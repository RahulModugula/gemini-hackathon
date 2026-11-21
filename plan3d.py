"""3D plan data for one sheet, built only from the PDF's vector drawing (no invented model).

    python plan3d.py add1-p018        # writes build/plan3d/add1-p018.json + .png and prints a summary

Walls: the wall primitives rooms.wall_mask picks (grey fills, grey strokes >= 1 pt) rasterised at 1 px per pt, closed
by WALL_CLOSE pt so the double outline of a wall becomes one solid band, then traced into polygons (outer ring + holes)
along pixel edges and simplified (Douglas-Peucker, SIMPLIFY pt). Rooms: rooms.sheet_rooms (flood fill from the room tags),
each mask traced to its outer ring. Scale: the sheet's own 'n/d" = 1'-0"' text, else null (callers then use a fixed
consistent factor). Doors: none are drawn as swings on the plumbing plans, so openings are simply the gaps the drawing
leaves in the walls (no door objects are invented)."""
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
import pymupdf

import pdflock
import rooms

HERE = Path(__file__).parent
BUILD = HERE / "build"
VERSION = 2                       # bump when the output changes so stale caches are rebuilt
WALL_CLOSE = 4                    # pt; closing radius: fills wall cavities up to 2r thick (door gaps are ~27 pt)
MIN_WALL_AREA = 12                # pt^2; specks smaller than this are dropped
SIMPLIFY = 0.9                    # pt; Douglas-Peucker tolerance for wall rings
ROOM_SIMPLIFY = 1.5
TEX_PX = 4096                     # longest side of the whole-sheet texture
DETAIL_PX = 4096                  # longest side of the building-extent texture
SHEET_ID = re.compile(r"^[a-z0-9]{2,8}-p\d{3}$")
SCALE_RE = re.compile(r'^(\d+)/(\d+)"$')


class UnknownSheet(KeyError):
    pass


def sheet_entry(sheet, build=None):
    """-> (sheet row, pdf rel path). UnknownSheet unless the id is well formed AND listed in build/sheets.json."""
    build = Path(build) if build else BUILD
    if not isinstance(sheet, str) or not SHEET_ID.match(sheet):
        raise UnknownSheet(sheet)
    idx = json.loads((build / "sheets.json").read_text())
    s = next((x for x in idx["sheets"] if x["sheet"] == sheet), None)
    if s is None:
        raise UnknownSheet(sheet)
    return s, next(x["path"] for x in idx["sources"] if x["source"] == s["source"])


# ---- raster -> polygons ----------------------------------------------------------------------------
def trace(mask):
    """Closed rings along the pixel edges of a bool mask (x right, y down, vertices on the pixel grid).
    Outer rings have positive shoelace area, holes negative. Diagonal pinches are split (right turn first)."""
    m = np.pad(mask.astype(bool), 1)
    out = {}
    # top edges: solid with empty above, run left->right; bottom: right->left; left: bottom->top; right: top->bottom
    ys, xs = np.nonzero(m[1:-1, 1:-1] & ~m[:-2, 1:-1])
    for y, x in zip(ys.tolist(), xs.tolist()):
        out.setdefault((x, y), []).append((x + 1, y))
    ys, xs = np.nonzero(m[1:-1, 1:-1] & ~m[2:, 1:-1])
    for y, x in zip(ys.tolist(), xs.tolist()):
        out.setdefault((x + 1, y + 1), []).append((x, y + 1))
    ys, xs = np.nonzero(m[1:-1, 1:-1] & ~m[1:-1, :-2])
    for y, x in zip(ys.tolist(), xs.tolist()):
        out.setdefault((x, y + 1), []).append((x, y))
    ys, xs = np.nonzero(m[1:-1, 1:-1] & ~m[1:-1, 2:])
    for y, x in zip(ys.tolist(), xs.tolist()):
        out.setdefault((x + 1, y), []).append((x + 1, y + 1))
    rings = []
    while out:
        start = next(iter(out))
        ring, cur, d = [start], start, None
        while True:
            nxt = out[cur]
            if len(nxt) == 1 or d is None:
                n = nxt.pop(0)
            else:                                   # pinch: take the right turn (screen coords, y down)
                right = (-d[1], d[0])
                k = next((i for i, v in enumerate(nxt) if (v[0] - cur[0], v[1] - cur[1]) == right), 0)
                n = nxt.pop(k)
            if not nxt:
                del out[cur]
            d = (n[0] - cur[0], n[1] - cur[1])
            cur = n
            if cur == start:
                break
            ring.append(cur)
        rings.append(ring)
    return rings


def area(ring):
    s = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
        s += x0 * y1 - x1 * y0
    return s / 2


def _dp(pts, tol):
    """Douglas-Peucker on an open polyline (list of (x, y)), endpoints kept."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    a = np.asarray(pts, float)
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        p, q = a[i], a[j]
        seg = a[i + 1:j]
        d = q - p
        L = math.hypot(*d)
        if L == 0:
            dist = np.hypot(*(seg - p).T)
        else:
            dist = np.abs(d[0] * (seg[:, 1] - p[1]) - d[1] * (seg[:, 0] - p[0])) / L
        k = int(np.argmax(dist))
        if dist[k] > tol:
            keep[i + 1 + k] = True
            stack += [(i, i + 1 + k), (i + 1 + k, j)]
    return [pt for pt, k in zip(pts, keep) if k]


def simplify(ring, tol):
    """Closed ring -> simplified closed ring (split at the vertex farthest from the first)."""
    if len(ring) < 5:
        return ring
    a = np.asarray(ring, float)
    far = int(np.argmax(np.hypot(*(a - a[0]).T)))
    r = _dp(ring[:far + 1], tol)[:-1] + _dp(ring[far:] + ring[:1], tol)[:-1]
    return r if len(r) >= 3 else ring


def _inside(pt, ring):
    x, y = pt
    c = False
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            c = not c
    return c


def polygons(mask, tol, min_area, ox=0, oy=0):
    """bool mask -> [{"outer": [[x, y]...], "holes": [[[x, y]...]]}] in pt (offset by ox, oy)."""
    rings = [r for r in trace(mask) if abs(area(r)) >= min_area]
    outers = [r for r in rings if area(r) > 0]
    holes = [r for r in rings if area(r) < 0]
    out = [{"ring": o, "a": area(o), "holes": []} for o in outers]
    for h in holes:
        # midpoint of the hole's first edge, nudged off the grid: inside the ring that encloses the hole
        probe = ((h[0][0] + h[1][0]) / 2 + 0.01, (h[0][1] + h[1][1]) / 2 + 0.01)
        owner = [p for p in out if _inside(probe, p["ring"])]
        if owner:
            min(owner, key=lambda p: p["a"])["holes"].append(h)

    def fmt(r):
        return [[x + ox, y + oy] for x, y in simplify(r, tol)]
    return [{"outer": fmt(p["ring"]), "holes": [fmt(h) for h in p["holes"]]} for p in out]


# ---- sheet data ------------------------------------------------------------------------------------
def scale_of(words):
    """'1/8" = 1'-0"' in the text layer -> {"text", "pt_per_m"}; the most frequent scale wins. None if absent."""
    seen = {}
    for i in range(len(words) - 2):
        m = SCALE_RE.match(words[i]["text"])
        if m and words[i + 1]["text"] == "=" and words[i + 2]["text"] in ("1'-0\"", "1'-0”", "1’-0\""):
            n, d = int(m.group(1)), int(m.group(2))
            if d:
                k = (n, d)
                seen[k] = seen.get(k, 0) + 1
    if not seen:
        return None
    n, d = max(seen, key=seen.get)
    pt_per_ft = 72.0 * n / d
    return {"text": f'{n}/{d}" = 1\'-0"', "pt_per_m": round(pt_per_ft / 0.3048, 4)}


def _ring_of(room):
    ps = polygons(room.mask, ROOM_SIMPLIFY, 4, room.x0, room.y0)
    return max(ps, key=lambda p: abs(area([tuple(v) for v in p["outer"]])))["outer"] if ps else []


def extract(sheet, build=None):
    """Extract the plan data for `sheet` (no cache) and render its textures into build/plan3d/."""
    build = Path(build) if build else BUILD
    s, rel = sheet_entry(sheet, build)
    words = json.loads((build / "words" / f"{sheet}.json").read_text())["words"]
    out_dir = build / "plan3d"
    out_dir.mkdir(parents=True, exist_ok=True)
    with pdflock.LOCK:
        page = pymupdf.open(rooms._pdf(rel))[s["page"] - 1]
        W, H = float(page.rect.width), float(page.rect.height)
        if page.rotation % 180:
            W, H = H, W
        wall, n = rooms.wall_mask(page)
        z = TEX_PX / max(W, H)
        page.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False).save(str(out_dir / f"{sheet}.png"))
    walls, bounds, detail = [], None, None
    if n:
        solid = rooms._close(wall, WALL_CLOSE) | wall
        ys, xs = np.nonzero(solid)
        x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
        walls = polygons(solid[y0:y1, x0:x1], SIMPLIFY, MIN_WALL_AREA, x0, y0)
        bounds = [x0, y0, x1, y1]
        pad = 40
        rect = [max(0, x0 - pad), max(0, y0 - pad), min(W, x1 + pad), min(H, y1 + pad)]
        dz = DETAIL_PX / max(rect[2] - rect[0], rect[3] - rect[1])
        with pdflock.LOCK:
            page = pymupdf.open(rooms._pdf(rel))[s["page"] - 1]
            page.get_pixmap(matrix=pymupdf.Matrix(dz, dz), clip=pymupdf.Rect(*rect), alpha=False).save(
                str(out_dir / f"{sheet}.detail.png"))
        detail = {"url": f"/api/plan3d/{sheet}/detail.png", "rect_pt": rect}
    geo = rooms.sheet_rooms(sheet, build)
    rms = [{"number": r.number, "name": r.name, "polygon_pt": _ring_of(r), "bbox_pt": r.bbox,
            "label_pt": [(r.label_bbox[0] + r.label_bbox[2]) / 2, (r.label_bbox[1] + r.label_bbox[3]) / 2],
            "tag_count": int(sum(r.tags.values())), "tags": r.tags} for r in geo["rooms"]]
    return {"v": VERSION, "sheet": sheet, "size_pt": [W, H], "bounds_pt": bounds, "scale": scale_of(words),
            "walls": walls, "rooms": rms, "doors": [],
            "doors_note": "no door swings are drawn on this sheet; openings are the gaps in the drawn walls",
            "texture_url": f"/api/plan3d/{sheet}/texture.png", "detail": detail,
            "method": "walls: rooms.wall_mask primitives closed by %d pt, traced and simplified at %.1f pt" % (
                WALL_CLOSE, SIMPLIFY)}


def get(sheet, build=None, use_cache=True):
    """Cached plan data (build/plan3d/<sheet>.json). Raises UnknownSheet for ids not in the index."""
    build = Path(build) if build else BUILD
    sheet_entry(sheet, build)
    cache = build / "plan3d" / f"{sheet}.json"
    if use_cache and cache.exists() and (build / "plan3d" / f"{sheet}.png").exists():
        try:
            d = json.loads(cache.read_text())
            if d.get("v") == VERSION:
                return d
        except ValueError:
            pass
    d = extract(sheet, build)
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".json.part")
    tmp.write_text(json.dumps(d, separators=(",", ":")))
    tmp.replace(cache)
    return d



def texture_path(sheet, kind, build=None):
    """Path of a rendered texture ('texture' or 'detail'); UnknownSheet / FileNotFoundError when absent."""
    build = Path(build) if build else BUILD
    sheet_entry(sheet, build)
    p = build / "plan3d" / (f"{sheet}.png" if kind == "texture" else f"{sheet}.detail.png")
    if not p.is_file():
        get(sheet, build)
    if not p.is_file():
        raise FileNotFoundError(p)
    return p


if __name__ == "__main__":
    sh = sys.argv[1] if len(sys.argv) > 1 else "add1-p018"
    d = get(sh, use_cache=False)
    nv = sum(len(w["outer"]) + sum(len(h) for h in w["holes"]) for w in d["walls"])
    print(f"{sh}: {len(d['walls'])} wall polygons, {nv} vertices, {len(d['rooms'])} rooms, scale={d['scale']}, "
          f"bounds={d['bounds_pt']}")
