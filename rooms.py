"""Deterministic room geometry from a sheet's vector drawing (no ML, no extra dependencies).

    python rooms.py add1-p018            # writes build/rooms/add1-p018.json and prints a summary

Walls are the grey drawing primitives: grey fills (the 0.55 filled polygons on the architectural sheets) and heavy
grey strokes (width >= WALL_STROKE_MIN, the 1.2 pt wall lines on the plumbing/mechanical overlays). Heavy BLACK strokes
are fire-rating lines or pipes and thin grey ones are grid lines / fixture outlines, so none of those count.

Method: rasterise the wall primitives at 1 px per pt, dilate them by r (so door openings up to ~2r pt close), flood
fill from each room-number tag in the text layer (105A, 109B), grow the fill back by r inside the original free space.
Several r are tried, largest first; a fill that is too big or reaches another room's tag has leaked, so that room is
reported as unresolved rather than guessed. Sheets with no such walls yield no rooms (callers treat that as "no
geometry", never as a failure).
"""
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
import pymupdf

import pdflock

HERE = Path(__file__).parent
BUILD = HERE / "build"
WALL_STROKE_MIN = 1.0            # pt; grey strokes at least this heavy are walls
WALL_LINE_PX = 2.0               # drawn at least this wide so diagonal walls stay 4-connected
GAPS = (20, 18, 16, 14, 12, 10, 8)              # closing radii tried, largest first (bridges wall gaps up to 2r pt)
MAX_AREA = 250_000               # pt^2; a bigger fill has leaked into the rest of the building
MIN_AREA = 150
TITLE_STRIP = 0.88
CACHE_VERSION = 1                # bump when extraction changes, so stale build/rooms/*.json is ignored
TAG = re.compile(r"^\d{3}[A-Z]?$")
FIXTURE_TAG = re.compile(r"^[A-Z]{1,3}-\d{1,2}[A-Z]?$")


def _grey(c):
    return bool(c) and len(c) == 3 and max(c) - min(c) < 0.02 and 0.4 <= c[0] <= 0.65


def _is_name(t):
    return len(t) >= 2 and t.isupper() and t.replace(".", "").replace("&", "").replace("/", "").isalpha()


def wall_mask(page):
    """Bool array [H, W] at 1 px per pt (page space as rendered): True on wall primitives."""
    with pdflock.LOCK:      # pymupdf is not thread-safe
        return _wall_mask(page)


def _wall_mask(page):
    W, H = math.ceil(page.rect.width), math.ceil(page.rect.height)
    doc = pymupdf.open()
    q = doc.new_page(width=page.rect.width, height=page.rect.height)
    sh = q.new_shape()
    n = 0
    for d in page.get_drawings():
        fill = d.get("fill")
        stroke = d.get("color") if d["type"] in ("s", "fs") else None
        wall_fill = _grey(fill)
        wall_stroke = _grey(stroke) and (d.get("width") or 0) >= WALL_STROKE_MIN
        if not (wall_fill or wall_stroke):
            continue
        for it in d["items"]:
            if it[0] == "l":
                sh.draw_line(it[1], it[2])
            elif it[0] == "re":
                sh.draw_rect(it[1])
            elif it[0] == "qu":
                sh.draw_quad(it[1])
            elif it[0] == "c":
                sh.draw_bezier(*it[1:])
        sh.finish(color=(0,), fill=(0,) if wall_fill else None, width=max(d.get("width") or 0, WALL_LINE_PX),
                  closePath=False)
        n += 1
    sh.commit()
    if n == 0:
        return np.zeros((H, W), bool), 0
    pm = q.get_pixmap(matrix=pymupdf.Matrix(1, 1), colorspace=pymupdf.csGRAY, alpha=False)
    a = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width) < 128
    out = np.zeros((H, W), bool)
    out[:a.shape[0], :a.shape[1]] = a[:H, :W]
    if page.rotation:                       # drawings are in unrotated space, words in rendered space
        out = np.rot90(out, -(page.rotation // 90))
    return out, n


def _dilate_box(m, r):
    """Square dilation by r (separable running window via cumulative sums)."""
    for axis in (0, 1):
        pad = [(0, 0), (0, 0)]
        pad[axis] = (r + 1, r)
        c = np.cumsum(np.pad(m.astype(np.int32), pad), axis=axis)
        n = m.shape[axis]
        hi = np.take(c, range(2 * r + 1, 2 * r + 1 + n), axis=axis)
        lo = np.take(c, range(0, n), axis=axis)
        m = (hi - lo) > 0
    return m


def _flood(free, seed, cap):
    """4-connected fill of `free` from seed -> (flat index list, leaked). Aborts (leaked) past `cap` cells."""
    H, W = free.shape
    flat = free.ravel()
    seen = bytearray(H * W)
    sy, sx = seed
    s = sy * W + sx
    seen[s] = 1
    stack, out = [s], []
    while stack:
        i = stack.pop()
        out.append(i)
        if len(out) > cap:
            return out, True
        y, x = divmod(i, W)
        for j, ok in ((i - W, y > 0), (i + W, y < H - 1), (i - 1, x > 0), (i + 1, x < W - 1)):
            if ok and flat[j] and not seen[j]:
                seen[j] = 1
                stack.append(j)
    return out, False


def _nearest_free(free, cx, cy, reach=3):
    H, W = free.shape
    cx, cy = int(cx), int(cy)
    best = None
    for dy in range(-reach, reach + 1):
        for dx in range(-reach, reach + 1):
            x, y = cx + dx, cy + dy
            if 0 <= x < W and 0 <= y < H and free[y, x]:
                d = dx * dx + dy * dy
                if best is None or d < best[0]:
                    best = (d, y, x)
    return None if best is None else (best[1], best[2])


def _close(wall, r):
    """Morphological closing with a (2r+1) square: bridges wall gaps up to 2r pt (door openings) and nothing else."""
    d = _dilate_box(wall, r)
    return ~_dilate_box(~d, r)


POCKET_THICK = 9             # pt; a pocket must be thicker than this somewhere
POCKET_MAX = 3000           # pt^2; a leftover enclosed pocket this small (a toilet stall) belongs to the room it opens on


def _absorb_pockets(m, x0, y0, free0, centres, own, pad=60):
    """Add pockets the closing sealed off (stalls and alcoves narrower than 2r): connected free cells outside the
    region that touch it, number at most POCKET_MAX cells and hold no other room's tag. -> (mask, x0, y0) of the crop."""
    H, W = free0.shape
    cy0, cy1, cx0, cx1 = max(0, y0 - pad), min(H, y0 + m.shape[0] + pad), max(0, x0 - pad), min(W, x0 + m.shape[1] + pad)
    reg = np.zeros((cy1 - cy0, cx1 - cx0), bool)
    reg[y0 - cy0:y0 - cy0 + m.shape[0], x0 - cx0:x0 - cx0 + m.shape[1]] = m
    rem = free0[cy0:cy1, cx0:cx1] & ~reg
    near = np.zeros_like(reg)               # free cells 4-adjacent to the region
    near[1:] |= reg[:-1]
    near[:-1] |= reg[1:]
    near[:, 1:] |= reg[:, :-1]
    near[:, :-1] |= reg[:, 1:]
    near &= rem
    others = [(int(oy) - cy0, int(ox) - cx0) for k, (ox, oy) in centres.items() if k != own
              and 0 <= int(oy) - cy0 < rem.shape[0] and 0 <= int(ox) - cx0 < rem.shape[1]]
    seen = np.zeros_like(rem)
    for sy, sx in zip(*np.nonzero(near)):
        if seen[sy, sx]:
            continue
        idx, big = _flood(rem, (sy, sx), POCKET_MAX)
        ys, xs = np.divmod(np.asarray(idx), rem.shape[1])
        seen[ys, xs] = True
        if big:
            continue
        pk = np.zeros_like(rem)
        pk[ys, xs] = True
        if any(pk[oy, ox] for oy, ox in others) or not (~_dilate_box(~pk, POCKET_THICK // 2)).any():
            continue                        # holds another tag, or is a sliver (wall cavity, chase) not a stall
        reg |= pk
    return reg, cx0, cy0


class Room:
    """One room: number, name, tag bbox, a pixel mask (1 px = 1 pt) and the fixture tags inside it."""

    def __init__(self, number, name, label_bbox, mask, x0, y0, tags=None, gap=None):
        self.number, self.name, self.label_bbox = number, name, list(label_bbox)
        ys, xs = np.nonzero(mask)
        mask = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]          # tight crop, whatever the caller passed
        x0, y0 = int(x0) + int(xs.min()), int(y0) + int(ys.min())
        self.mask, self.x0, self.y0, self.tags, self.gap = mask, x0, y0, dict(tags or {}), gap
        ys, xs = np.nonzero(mask)
        self.bbox = [float(self.x0 + xs.min()), float(self.y0 + ys.min()), float(self.x0 + xs.max() + 1),
                     float(self.y0 + ys.max() + 1)]
        self.area = int(mask.sum())

    def contains_pt(self, x, y):
        i, j = int(y) - self.y0, int(x) - self.x0
        return 0 <= i < self.mask.shape[0] and 0 <= j < self.mask.shape[1] and bool(self.mask[i, j])

    def share(self, box, margin=0):
        """Share (0-1) of the pt box [x0, y0, x1, y1] covered by the room, the room grown by `margin` pt."""
        m, ox, oy = self.mask, self.x0, self.y0
        if margin:
            m = np.pad(m, margin)
            ox, oy = ox - margin, oy - margin
            k = _dilate_box(m, margin)
            m = k
        bx0, by0, bx1, by1 = int(math.floor(box[0])), int(math.floor(box[1])), int(math.ceil(box[2])), int(math.ceil(box[3]))
        area = max((bx1 - bx0) * (by1 - by0), 1)
        ix0, iy0, ix1, iy1 = max(bx0, ox), max(by0, oy), min(bx1, ox + m.shape[1]), min(by1, oy + m.shape[0])
        if ix0 >= ix1 or iy0 >= iy1:
            return 0.0
        return float(m[iy0 - oy:iy1 - oy, ix0 - ox:ix1 - ox].sum()) / area

    def contains(self, box, margin=6.0, min_share=0.5):
        """True when the pt box lies in the room: at least `min_share` of its area is in the room grown by `margin`,
        or its centre is."""
        m = int(round(margin))
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        return self.share(box, m) >= min_share or self.share([cx - .5, cy - .5, cx + .5, cy + .5], m) >= 1.0

    def to_json(self):
        runs = []
        for i in range(self.mask.shape[0]):
            row, j = self.mask[i], 0
            while j < len(row):
                if row[j]:
                    k = j
                    while k < len(row) and row[k]:
                        k += 1
                    runs.append([self.y0 + i, self.x0 + j, self.x0 + k])
                    j = k
                else:
                    j += 1
        return {"number": self.number, "name": self.name, "label_bbox": self.label_bbox, "bbox": self.bbox,
                "area": self.area, "gap": self.gap, "tags": self.tags, "runs": runs}

    @classmethod
    def from_json(cls, d):
        runs = d["runs"]
        x0, y0 = min(r[1] for r in runs), min(r[0] for r in runs)
        x1, y1 = max(r[2] for r in runs), max(r[0] for r in runs) + 1
        mask = np.zeros((y1 - y0, x1 - x0), bool)
        for y, a, b in runs:
            mask[y - y0, a - x0:b - x0] = True
        return cls(d["number"], d["name"], d["label_bbox"], mask, x0, y0, d.get("tags"), d.get("gap"))


def _centre(b):
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def room_tags(words, width):
    """Room-number tags in the plan area: 105A, 109B."""
    return [w for w in words if TAG.match(w["text"]) and w["bbox"][0] < TITLE_STRIP * width]


def room_name(tag, words):
    """Name words stacked just above the tag ("TOILET" above 105A; up to two lines, ("STAFF", "LOUNGE") style)."""
    cx, _ = _centre(tag["bbox"])
    line_h = tag["bbox"][3] - tag["bbox"][1]
    got, top = [], tag["bbox"][1]
    for _ in range(2):
        row = [w for w in words if _is_name(w["text"]) and top - 2.2 * line_h <= w["bbox"][3] <= top + 0.3 * line_h
               and abs(_centre(w["bbox"])[0] - cx) <= 45 and w["bbox"][1] < top - 0.2 * line_h]
        if not row:
            break
        row.sort(key=lambda w: w["bbox"][0])
        got.insert(0, " ".join(w["text"] for w in row))
        top = min(w["bbox"][1] for w in row)
    return " ".join(got)


def _tags_inside(room_mask, x0, y0, words):
    out = {}
    for w in words:
        if FIXTURE_TAG.match(w["text"]):
            cx, cy = _centre(w["bbox"])
            i, j = int(cy) - y0, int(cx) - x0
            if 0 <= i < room_mask.shape[0] and 0 <= j < room_mask.shape[1] and room_mask[i, j]:
                out[w["text"]] = out.get(w["text"], 0) + 1
    return out


def extract(page, words, width=None):
    """-> (rooms, unresolved tag numbers). `page` is the pymupdf page, `words` the sheet's text-layer words."""
    width = width or page.rect.width
    tags = room_tags(words, width)
    wall, n = wall_mask(page)
    if n == 0 or not tags:
        return [], [t["text"] for t in tags]
    centres = {t["text"]: _centre(t["bbox"]) for t in tags}
    free = {}
    rooms, unresolved = [], []
    for t in tags:
        cx, cy = centres[t["text"]]
        done = None
        for r in GAPS:
            if r not in free:
                free[r] = ~_close(wall, r)
            seed = _nearest_free(free[r], cx, cy)
            if seed is None:
                continue
            idx, leaked = _flood(free[r], seed, MAX_AREA)
            if leaked:
                continue
            ys, xs = np.divmod(np.asarray(idx), wall.shape[1])
            H, W = wall.shape
            if xs.min() == 0 or ys.min() == 0 or xs.max() == W - 1 or ys.max() == H - 1:
                continue                    # reached the page edge: the fill escaped the building
            gx, gy = int(xs.min()), int(ys.min())
            m = np.zeros((int(ys.max()) - gy + 1, int(xs.max()) - gx + 1), bool)
            m[ys - gy, xs - gx] = True
            others = [k for k, (ox, oy) in centres.items() if k != t["text"]
                      and 0 <= int(oy) - gy < m.shape[0] and 0 <= int(ox) - gx < m.shape[1]
                      and m[int(oy) - gy, int(ox) - gx]]
            if others:
                break                       # filled into another room: smaller r only leaks more
            if m.sum() < MIN_AREA:
                continue
            m, gx, gy = _absorb_pockets(m, gx, gy, ~wall, centres, t["text"])
            done = (m, gx, gy, r)
            break
        if done is None:
            unresolved.append(t["text"])
            continue
        m, gx, gy, r = done
        rooms.append(Room(t["text"], room_name(t, words), t["bbox"], m, gx, gy, _tags_inside(m, gx, gy, words), r))
    return rooms, unresolved


# ---- sheet-level API + cache ---------------------------------------------------------------------
def _pdf(rel):
    for base in (HERE, (HERE / "build").resolve().parent):
        if (base / rel).exists():
            return base / rel
    raise FileNotFoundError(rel)


def sheet_rooms(sheet, build=None, use_cache=True):
    """-> {"sheet", "rooms": [Room], "unresolved": [...], "by_number": {norm: Room}} cached at build/rooms/<sheet>.json."""
    build = Path(build) if build else BUILD
    cache = build / "rooms" / f"{sheet}.json"
    d = json.loads(cache.read_text()) if use_cache and cache.exists() else None
    if d and d.get("v") == CACHE_VERSION:
        rooms = [Room.from_json(r) for r in d["rooms"]]
        unresolved = d["unresolved"]
    else:
        idx = json.loads((build / "sheets.json").read_text())
        s = next(x for x in idx["sheets"] if x["sheet"] == sheet)
        path = next(x["path"] for x in idx["sources"] if x["source"] == s["source"])
        words = json.loads((build / "words" / f"{sheet}.json").read_text())["words"]
        with pdflock.LOCK:
            page = pymupdf.open(_pdf(path))[s["page"] - 1]
            rooms, unresolved = extract(page, words, s["width"])
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"v": CACHE_VERSION, "sheet": sheet, "unresolved": unresolved, "rooms": [r.to_json() for r in rooms]}))
    return {"sheet": sheet, "rooms": rooms, "unresolved": unresolved,
            "by_number": {norm(r.number): r for r in rooms}}


def norm(s):
    return "".join(c for c in s.lower() if c.isalnum())


def named_rooms(claim, geo):
    """Rooms of `geo` (sheet_rooms result) whose number the claim names -> list of Room."""
    seen, out = set(), []
    for t in claim.split():
        k = norm(t)
        if k in geo["by_number"] and k not in seen:
            seen.add(k)
            out.append(geo["by_number"][k])
    return out


def claim_room_tokens(claim):
    """Room-number-shaped tokens in a claim (105A, 109B), whether or not geometry exists for them."""
    return list(dict.fromkeys(norm(t) for t in claim.split() if TAG.match(t.strip(".,;:()\"'").upper())
                              for t in [t.strip(".,;:()\"'").upper()]))


if __name__ == "__main__":
    sh = sys.argv[1] if len(sys.argv) > 1 else "add1-p018"
    g = sheet_rooms(sh, use_cache=False)
    for r in g["rooms"]:
        print(f"{r.number:6} {r.name:22} area={r.area:7} gap={r.gap} bbox={[round(v) for v in r.bbox]} tags={r.tags}")
    print(f"{len(g['rooms'])} rooms, unresolved: {g['unresolved']}")
