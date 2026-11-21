"""Deterministic DEV claim set from the addenda (dev only; the held-out set is human-written).

For every superseded -> current sheet pair: spatial word diff (global offset estimated from unique tokens),
cluster changed words (<=150 pt), keep clusters with tag-like tokens, then build claims whose labels hold by
construction:
  * stale   : "On P101, the water closets in Toilet 105A are tagged WC-2." (OLD value)        -> conflict
              (pure additions: a same-shaped decoy value verified absent from both crops)
  * current : the same sentence with the NEW value                                         -> consistent
  * unclear : asks for a fact (a manufacturer) that is not on the sheet at all             -> unclear
Every claim names its sheet; no counts, no negations. Values are checked against the words in the crop window.

Usage (repo root): python studio/devset.py [--out studio/devset] [--packs studio/packs] [--dpi 200] [--no-packs]
"""
import argparse
import importlib.util
import json
import re
import os
import statistics
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ATTRIB_PT = 125.0       # an asserted value must be this close to its room-number anchor (and checked within this radius)
ROW_TOL_PT = 3.0        # same text row (schedule line) if centres differ by <= this in y
MARGIN = 1.3            # off-row anchors must be this much nearer than the next room tag
CLUSTER_PT = 150.0
TOL_PT = 4.0            # position tolerance after the global offset is removed
WINDOW_HALF = 220.0     # claim window = the crop the judge sees: square of this half-size around the change (pt)
ANCHOR_MAX_PT = 150.0   # cluster-level anchor (only used for the unclear claims)
MAX_CHANGES_PER_SHEET = 5   # a stale+current pair per change => at most 8 claims per sheet
TARGET_CHANGES = 15
TITLE_BLOCK = (2700.0, 1800.0)  # words right/below this point are title block / revision stamp, not content

TAG_RES = [
    re.compile(r"^[A-Z]{1,4}-?\d{1,3}[A-Z]?$"),          # WC-3, BL06, P1, DF-2, T09
    re.compile(r"^\d{2,4}[A-Z]?$"),                       # room / keynote / dimension-ish numbers
    re.compile(r"^\d+['’]-\d+[\"”]?$"),         # 3'-0" (bare 3' / 11' are split-dimension fragments)
    re.compile(r"^(\d{2,}(\.\d+)?|\d+/\d+)[\"”%]$"),  # 12"  7/8"  (single digits like 4" / 0" are dimension fragments)
    re.compile(r"^\d+(\.\d+)?$"),                         # 55.8
    re.compile(r"^\d+/[A-Z]\d{2,4}$"),                    # 1/A1001
    re.compile(r"^[A-Z]\d{3,4}$"),                        # sheet refs
]
STOP = {"AND", "THE", "OF", "TO", "FOR", "IN", "AT", "ON", "A", "AN", "OR", "NO", "NOT", "SEE", "ALL", "DWG", "TYP."}


def is_tag(t):
    t = t.strip()
    return bool(t) and t.upper() not in STOP and any(r.match(t) for r in TAG_RES)


def is_room(t):
    return bool(re.match(r"^\d{3}[A-Z]?$", t))


def is_delta_digit(t):
    return bool(re.match(r"^\d$", t))   # lone 1-9: revision delta markers / bubbles, too ambiguous to claim on


def load_words(sheet):
    return json.loads((ROOT / f"build/words/{sheet}.json").read_text())["words"]


def center(w):
    b = w["bbox"]
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def in_title_block(w):
    cx, cy = center(w)
    return cx >= TITLE_BLOCK[0] and cy >= TITLE_BLOCK[1]


def estimate_offset(old, new):
    """Median (dx, dy) over tokens that occur exactly once in both sheets (rounded mode, then median of members)."""
    co, cn = Counter(w["text"] for w in old), Counter(w["text"] for w in new)
    uo = {w["text"]: center(w) for w in old if co[w["text"]] == 1 and cn[w["text"]] == 1 and len(w["text"]) >= 3}
    un = {w["text"]: center(w) for w in new if co[w["text"]] == 1 and cn[w["text"]] == 1 and len(w["text"]) >= 3}
    ds = [(un[t][0] - uo[t][0], un[t][1] - uo[t][1]) for t in uo if t in un]
    if not ds:
        return 0.0, 0.0
    mode = Counter((round(dx), round(dy)) for dx, dy in ds).most_common(1)[0][0]
    mem = [(dx, dy) for dx, dy in ds if abs(dx - mode[0]) <= 2 and abs(dy - mode[1]) <= 2]
    return statistics.median(x for x, _ in mem), statistics.median(y for _, y in mem)


def align(old, new, off, tol=TOL_PT):
    """Greedy nearest same-text matching within tol after shifting old by off. Returns (removed, added) word lists."""
    bucket = defaultdict(list)
    for j, w in enumerate(new):
        bucket[w["text"]].append(j)
    used, removed = set(), []
    for w in old:
        cx, cy = center(w)
        cx, cy = cx + off[0], cy + off[1]
        best, bd = None, tol
        for j in bucket.get(w["text"], []):
            if j in used:
                continue
            nx, ny = center(new[j])
            d = max(abs(nx - cx), abs(ny - cy))
            if d <= bd:
                best, bd = j, d
        if best is None:
            removed.append(w)
        else:
            used.add(best)
    return removed, [w for j, w in enumerate(new) if j not in used]


def shift(b, off):
    return [b[0] + off[0], b[1] + off[1], b[2] + off[0], b[3] + off[1]]


def gap(a, b):
    dx, dy = max(a[0] - b[2], b[0] - a[2], 0), max(a[1] - b[3], b[1] - a[3], 0)
    return (dx * dx + dy * dy) ** 0.5


def cluster(removed, added, off):
    """Single-linkage clusters of changed words in the CURRENT frame, gap <= CLUSTER_PT."""
    items = [("old", w, shift(w["bbox"], off)) for w in removed] + [("new", w, list(w["bbox"])) for w in added]
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if gap(items[i][2], items[j][2]) <= CLUSTER_PT:
                parent[find(i)] = find(j)
    groups = defaultdict(list)
    for i, it in enumerate(items):
        groups[find(i)].append(it)
    return list(groups.values())


def has_label(w, words):
    """A real room tag carries a room name (>= 4 capital letters) just above it or on the same row to its left."""
    ax, ay = center(w)
    for o in words:
        if o is w or not re.match(r"^[A-Z][A-Z/.&-]{3,}$", o["text"]):
            continue
        ox, oy = center(o)
        if (0 < ay - oy <= 60 and abs(ox - ax) <= 80) or (abs(oy - ay) <= ROW_TOL_PT and 0 < ax - ox <= 150):
            return True
    return False


def is_value_token(t):
    """Tokens a claim may assert: tags / refs / dimensions, not bare numbers (areas, loads) or room-like numbers."""
    return not is_room(t) and not (re.match(r"^\d+(\.\d+)?$", t) and not t.startswith("0"))


def rooms_by_distance(box, words, exclude=(), loose=False):
    """Room-number words (###/###A) sorted by distance to the centre of `box`. Words whose text is a changed token,
    repeated numbers (> 3 on the sheet) and area-table cells (SF / Occs lines) are not room tags."""
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    if loose:    # every room-looking word, no filters: used to catch a nearer tag the strict filters would hide
        return sorted(((((center(w)[0] - cx) ** 2 + (center(w)[1] - cy) ** 2) ** 0.5, w) for w in words
                       if is_room(w["text"]) and w["text"] not in exclude and not in_title_block(w)), key=lambda t: (t[0], t[1]["text"]))
    freq = Counter(w["text"] for w in words)
    area_lines = {(w.get("block"), w.get("line")) for w in words if w["text"] in {"SF", "SF/Occ", "Occs", "AREA", "Occ."}}
    out = []
    for w in words:
        if (is_room(w["text"]) and w["text"] not in exclude and freq[w["text"]] <= 3 and not in_title_block(w)
                and (w.get("block"), w.get("line")) not in area_lines and has_label(w, words)):
            x, y = center(w)
            out.append((((x - cx) ** 2 + (y - cy) ** 2) ** 0.5, w))
    return sorted(out, key=lambda t: (t[0], t[1]["text"]))


def nearest_anchor(bbox, words, exclude=()):
    """Cluster-level anchor: nearest room tag within ANCHOR_MAX_PT, else (None, None)."""
    rs = rooms_by_distance(bbox, words, exclude)
    return (rs[0][1]["text"], "room") if rs and rs[0][0] <= ANCHOR_MAX_PT else (None, None)


def union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def pad(b, p, w=3024.0, h=2160.0):
    return [max(0.0, b[0] - p), max(0.0, b[1] - p), min(w, b[2] + p), min(h, b[3] + p)]


def window(b, half=WINDOW_HALF, w=3024.0, h=2160.0):
    """Square crop window centred on box b, slid to stay on the page so it keeps its full size."""
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    x0, y0 = min(max(0.0, cx - half), w - 2 * half), min(max(0.0, cy - half), h - 2 * half)
    return [x0, y0, x0 + 2 * half, y0 + 2 * half]


def count_in(words, region, token):
    """Number of words with exact text `token` whose centre lies inside region."""
    return sum(1 for w in words if w["text"] == token and region[0] <= center(w)[0] <= region[2]
               and region[1] <= center(w)[1] <= region[3])


def reading_order(it):
    return (round(it[2][1] / 25), it[2][0])


def diff_pair(old_row, new_row):
    """All verified changes between a superseded sheet and the sheet that replaced it."""
    old, new = load_words(old_row["sheet"]), load_words(new_row["sheet"])
    off = estimate_offset(old, new)
    removed, added = align(old, new, off)
    changes = []
    for g in cluster([w for w in removed if not in_title_block(w)], [w for w in added if not in_title_block(w)], off):
        olds, news = sorted((i for i in g if i[0] == "old"), key=reading_order), sorted((i for i in g if i[0] == "new"), key=reading_order)
        otok, ntok = [i[1]["text"] for i in olds], [i[1]["text"] for i in news]
        if not any(is_tag(t) for t in otok + ntok) or len(otok) + len(ntok) > 60:
            continue
        # multiset cancel: tokens present on both sides of the cluster were only moved/re-flowed, not changed
        co, cn = Counter(otok), Counter(ntok)
        o_only, n_only = co - cn, cn - co
        okeys = [i for i in olds if o_only[i[1]["text"]] > 0 and is_tag(i[1]["text"]) and not is_delta_digit(i[1]["text"])]
        nkeys = [i for i in news if n_only[i[1]["text"]] > 0 and is_tag(i[1]["text"]) and not is_delta_digit(i[1]["text"])]
        if not okeys and not nkeys:
            continue
        boxes = [i[2] for i in g]
        bbox = union(boxes)
        anchor, akind = nearest_anchor(bbox, new, exclude=set(otok) | set(ntok))
        changes.append({
            "sheet_number": new_row["sheet_number"], "old_sheet": old_row["sheet"], "new_sheet": new_row["sheet"],
            "old_tokens": [t for t in otok if o_only[t] > 0], "new_tokens": [t for t in ntok if n_only[t] > 0],
            "bbox_pt": [round(v, 1) for v in bbox], "anchor": anchor, "anchor_kind": akind,
            "offset_pt": [round(off[0], 2), round(off[1], 2)],
            "_okeys": okeys, "_nkeys": nkeys,
        })
    return changes, off


def expand(changes):
    """One candidate per added tag: pair it with the nearest removed tag within 100 pt (a replacement), else it is an addition."""
    out = []
    for c in changes:
        for nk in c["_nkeys"]:
            olds = sorted((o for o in c["_okeys"] if gap(o[2], nk[2]) <= 100), key=lambda o: gap(o[2], nk[2]))
            out.append({**c, "_nkeys": [nk], "_okeys": olds[:1], "new_tokens": [nk[1]["text"]],
                        "old_tokens": [olds[0][1]["text"]] if olds else []})
    return out


def pairs(index):
    by = {r["sheet"]: r for r in index["rows"]}
    return [(r, by[r["superseded_by"]]) for r in index["rows"] if not r["current"] and r["superseded_by"]]


# ---------------------------------------------------------------- claims
def loc_label(anchor, words):
    """'Toilet 105A' when a TOILET label sits just above the room number, else 'room 105A'."""
    for a in (w for w in words if w["text"] == anchor):
        ax, ay = center(a)
        if any(w["text"].upper() == "TOILET" and abs(center(w)[0] - ax) <= 80 and 0 <= ay - center(w)[1] <= 60 for w in words):
            return f"Toilet {anchor}"
    return f"room {anchor}"


def claim_text(sheet, tok, loc, row_aligned=False):
    """One plain sentence naming the sheet and stating a value at a location (no counts, no negations)."""
    if row_aligned:   # value sits on the same text row as the room number (a schedule / list line)
        return f"On {sheet}, the list entry for {loc} is {tok}."
    if re.match(r"^WC-\d+$", tok):
        return f"On {sheet}, the water closets in {loc} are tagged {tok}."
    if re.match(r"^A\d{3,4}$", tok):
        return f"On {sheet}, {loc} references detail {tok}."
    if re.match(r"^[A-Z]{1,3}-?\d+[A-Z]?$", tok):
        return f"On {sheet}, the tag at {loc} reads {tok}."
    return f"On {sheet}, the callout at {loc} reads {tok}."


def decoy_for(tok, *windows_words):
    """A same-shaped wrong value (numeric part bumped) that appears in none of the given word lists."""
    m = re.search(r"\d+", tok)
    if not m:
        return None
    present = {w["text"] for ws in windows_words for w in ws}
    for k in range(3, 40):
        cand = tok[:m.start()] + str(int(m.group()) + k).zfill(len(m.group())) + tok[m.end():]
        if cand not in present:
            return cand
    return None


def in_window(words, region):
    return [w for w in words if region[0] <= center(w)[0] <= region[2] and region[1] <= center(w)[1] <= region[3]]


def count_near(words, pt, tok, radius=ATTRIB_PT):
    """Words with text `tok` whose centre is within `radius` of point pt."""
    return sum(1 for w in words if w["text"] == tok and ((center(w)[0] - pt[0]) ** 2 + (center(w)[1] - pt[1]) ** 2) ** 0.5 <= radius)


def _no(trace, why):
    if trace is not None:
        trace.append(why)
    return None


ROOM_NAMES = ["STORAGE", "JANITOR", "KITCHEN", "OFFICE", "CLASSROOM", "MECHANICAL", "LOBBY", "LAUNDRY"]


def room_name(w, words):
    """Name printed above a room-number tag (uppercase words on the nearest line above, within 60 pt), or None."""
    ax, ay = center(w)
    above = [o for o in words if re.match(r"^[A-Z][A-Z/.&-]{2,}$", o["text"]) and 0 < ay - center(o)[1] <= 60 and abs(center(o)[0] - ax) <= 90]
    if not above:
        return None
    line_y = max(center(o)[1] for o in above)     # the line closest above the tag
    run = sorted((o for o in above if abs(center(o)[1] - line_y) <= ROW_TOL_PT), key=lambda o: o["bbox"][0])
    name = " ".join(o["text"] for o in run)
    return (name, run) if sum(c.isalpha() for c in name) >= 4 else None


def room_name_pair(ch, old_words, new_words, seq, ntok, nk, trace=None):
    """A room tag that the addendum ADDED (e.g. ED200): current claim names the room, conflict claim names a different one."""
    off = ch["offset_pt"]
    if sum(1 for w in new_words if w["text"] == ntok) != 1:
        return _no(trace, "room number not unique on the sheet")
    w = next(w for w in new_words if w["text"] == ntok)
    rn = room_name(w, new_words)
    if not rn:
        return _no(trace, "no room name above the tag")
    name, run = rn
    ax, ay = center(w)
    region = window(union([list(w["bbox"])] + [o["bbox"] for o in run]))
    reg_old = [r - o for r, o in zip(region, off + off)]
    if count_near(old_words, (ax - off[0], ay - off[1]), ntok, 20):
        return _no(trace, "room tag already there")
    present = {o["text"] for ws in (in_window(new_words, region), in_window(old_words, reg_old)) for o in ws}
    decoy = next((n for n in ROOM_NAMES if n not in present), None)
    if decoy is None:
        return _no(trace, "no decoy")
    S = ch["sheet_number"]
    base = {"sheet_number": S, "current_sheet": ch["new_sheet"], "superseded_sheet": ch["old_sheet"], "anchor": ntok,
            "bbox_pt": [round(v, 1) for v in region], "offset_pt": off}
    current = {**base, "id": f"dev_{seq:02d}c", "claim": f"On {S}, room {ntok} is {name}.", "expected": "consistent",
               "evidence_tokens": [o["text"] for o in run] + [ntok], "kind": "current", "name": name}
    stale = {**base, "id": f"dev_{seq:02d}s", "claim": f"On {S}, room {ntok} is {decoy}.", "expected": "conflict",
             "evidence_tokens": [o["text"] for o in run] + [ntok], "absent_tokens": [decoy], "kind": "stale_decoy", "name": name}
    return stale, current


def count_row(words, pt, tok, reach=150.0):
    """Words with text `tok` on the same text row as point pt (|dy| <= ROW_TOL_PT), within `reach` horizontally."""
    return sum(1 for w in words if w["text"] == tok and abs(center(w)[1] - pt[1]) <= ROW_TOL_PT and abs(center(w)[0] - pt[0]) <= reach)


def build_pair(ch, old_words, new_words, seq, trace=None):
    """One verified change -> (conflict claim, consistent claim), or None when it cannot be stated attributably.
    The location is a labelled room tag within ATTRIB_PT of the asserted (new) token, on the same text row (|dy| <=
    ROW_TOL_PT) or MARGIN x nearer than the next room tag, and the changed word must be the nearest copy of that token
    to the tag. (Table row labels were tried as a second anchor type and dropped: they read wrongly on annotations.)"""
    off = ch["offset_pt"]
    okeys, nkeys = ch["_okeys"], ch["_nkeys"]
    old_it = okeys[0] if okeys else None
    if not nkeys:
        return _no(trace, "no new tag")
    nk = min(nkeys, key=lambda i: gap(i[2], old_it[2]) if old_it else reading_order(i))
    otok = old_it[1]["text"] if old_it else None
    ntok = nk[1]["text"]
    if otok == ntok:
        return _no(trace, "same token")
    S = ch["sheet_number"]
    if is_room(ntok) and not otok:
        return room_name_pair(ch, old_words, new_words, seq, ntok, nk, trace)
    tx, ty = (nk[2][0] + nk[2][2]) / 2, (nk[2][1] + nk[2][3]) / 2
    mode = None
    oc = ((old_it[2][0] + old_it[2][2]) / 2, (old_it[2][1] + old_it[2][3]) / 2) if old_it else None
    allr = rooms_by_distance(nk[2], new_words, exclude={otok, ntok}, loose=True)   # every room-looking word, unfiltered
    # (1) same text row: exactly one labelled room number to the left of the token on its row (a list / schedule line,
    #     e.g. "TODDLERS 102 - 18" TO RIM"), and the token is the only copy of itself on that row
    row = [w for d, w in allr if abs(center(w)[1] - ty) <= ROW_TOL_PT and center(w)[0] < tx and d <= ATTRIB_PT and has_label(w, new_words)]
    same_row = [w for w in new_words if w["text"] == ntok and abs(center(w)[1] - ty) <= ROW_TOL_PT and abs(center(w)[0] - tx) <= 150]
    if is_value_token(ntok) and not (otok and not is_value_token(otok)) and len(row) == 1 and len(same_row) == 1:
        aw, aligned = row[0], True
        d1 = ((center(aw)[0] - tx) ** 2 + (center(aw)[1] - ty) ** 2) ** 0.5
        ax, ay = center(aw)
        if oc is None or abs(oc[1] - ty) <= ROW_TOL_PT:
            mode = "room"
    # (2) otherwise the nearest strictly-filtered room tag, clearly nearer than any other room-looking word
    rooms = rooms_by_distance(nk[2], new_words, exclude={otok, ntok})
    if mode is None and is_value_token(ntok) and not (otok and not is_value_token(otok)) and rooms and rooms[0][0] <= ATTRIB_PT:
        d1, aw = rooms[0]
        ax, ay = center(aw)
        aligned = abs(ay - ty) <= ROW_TOL_PT
        near = sorted((((center(w)[0] - ax) ** 2 + (center(w)[1] - ay) ** 2) ** 0.5, w["bbox"]) for w in new_words if w["text"] == ntok)
        if allr[0][1]["bbox"] == aw["bbox"] and (aligned or len(allr) == 1 or allr[1][0] >= max(MARGIN * d1, d1 + 20.0)) \
                and near[0][1] == nk[1]["bbox"] and (oc is None or ((oc[0] - ax) ** 2 + (oc[1] - ay) ** 2) ** 0.5 <= ATTRIB_PT):
            mode = "room"
    if mode == "room":
        region = window(union([nk[2], list(aw["bbox"])] + ([old_it[2]] if old_it else [])))
        a_old = (ax - off[0], ay - off[1])
        loc = loc_label(aw["text"], new_words)
        anchor = aw["text"]
        cnt = count_row if aligned else count_near        # list lines: count on that row only
        n_new, n_was = cnt(new_words, (ax, ay), ntok), cnt(old_words, a_old, ntok)
        n_old = cnt(old_words, a_old, otok) if otok else 0
        n_cur = cnt(new_words, (ax, ay), otok) if otok else 0
        txt = lambda v: claim_text(S, v, loc, aligned)   # noqa: E731
        kind_anchor = "room"
    else:
        return _no(trace, "no attributable room tag")
    if not (n_new >= 1 and n_was < n_new):
        return _no(trace, "new value not new at the location")
    base = {"sheet_number": S, "current_sheet": ch["new_sheet"], "superseded_sheet": ch["old_sheet"],
            "anchor": anchor, "anchor_kind": kind_anchor, "bbox_pt": [round(v, 1) for v in region], "offset_pt": off}
    current = {**base, "id": f"dev_{seq:02d}c", "claim": txt(ntok), "expected": "consistent",
               "evidence_tokens": [ntok], "kind": "current"}
    if otok:     # replacement: the stale claim asserts the OLD value, which dropped out at the location
        if not (n_old >= 1 and n_cur < n_old):
            return _no(trace, "old value not dropped at the location")
        stale = {**base, "id": f"dev_{seq:02d}s", "claim": txt(otok), "expected": "conflict",
                 "evidence_tokens": [otok], "kind": "stale"}
    else:        # pure addition: nothing was there, so assert a plausible wrong value absent from both crops
        d = decoy_for(ntok, in_window(old_words, [r - o for r, o in zip(region, off + off)]), in_window(new_words, region))
        if not d:
            return _no(trace, "no decoy")
        stale = {**base, "id": f"dev_{seq:02d}s", "claim": txt(d), "expected": "conflict",
                 "evidence_tokens": [ntok], "absent_tokens": [d], "kind": "stale_decoy"}
    return stale, current


def provenance(ch, claim, bullets, new_words, region):
    """Explain exactly how the label was derived, plus the best-matching addendum bullet (word overlap) if any."""
    near = " ".join(w["text"] for w in new_words if region[0] - 100 <= center(w)[0] <= region[2] + 100
                    and region[1] - 100 <= center(w)[1] <= region[3] + 100).lower()
    keys = {t.lower() for t in ch["old_tokens"] + ch["new_tokens"]}
    best, bs = None, 0
    for b in bullets:
        bt = b["text"].lower()
        words = {x for x in re.findall(r"[a-z]{4,}", bt) if x not in {"sheet", "reference", "references", "revise"}}
        score = sum(1 for x in words if re.search(r"\b" + re.escape(x) + r"\b", near)) + 3 * sum(1 for k in keys if k in bt)
        if score > bs:
            best, bs = b, score
    diff = f"word diff {ch['old_sheet']} -> {ch['new_sheet']} (global offset {ch['offset_pt']} pt): removed {ch['old_tokens'][:6]}, added {ch['new_tokens'][:6]}"
    bullet = (f"; addendum bullet most similar (overlap score {bs}): {best['action']}: {best['text']}" if best and bs >= 2
              else f"; no addendum bullet matches by word overlap (bullets: {[b['text'][:50] for b in bullets]})")
    t = claim["evidence_tokens"][0] if claim["evidence_tokens"] else ""
    how = {"stale": f'label conflict: the claim asserts the OLD value "{t}", which is in this window on the superseded sheet but not (or less) on the current sheet',
           "stale_decoy": f'label conflict: pure addition, so nothing was there before; the claim asserts a plausible wrong value "{claim.get("absent_tokens", ["?"])[0]}" '
                          f'(same shape as the real "{t}"), verified absent from the words of both crops; the real value is "{t}"',
           "current": f'label consistent: "{t}" is in this window on the current sheet and was not (or less) on the superseded sheet'}[claim["kind"]]
    return f"{how}. {diff}{bullet}."


# (template, absent words): each asks for a fact no drawing sheet shows, so no crop can settle it.
UNCLEAR_TEMPLATES = [
    ("On {S}, the product at {loc} is manufactured by Kohler.", ["Kohler"]),
    ("On {S}, the work at {loc} is scheduled for completion in March 2027.", ["March", "2027"]),
    ("On {S}, the finish at {loc} is Sherwin-Williams SW 7005.", ["Sherwin", "7005"]),
    ("On {S}, the unit cost for the work at {loc} is $4,500.", ["$4,500"]),
    ("On {S}, the contractor responsible for {loc} is Kellerman Construction.", ["Kellerman"]),
    ("On {S}, the product at {loc} carries a 10-year manufacturer warranty.", ["warranty"]),
    ("On {S}, the product at {loc} is manufactured by Lutron.", ["Lutron"]),
]


def unclear_claims(sheets, n=5):
    """`n` varied claims about facts that appear nowhere on the (current or superseded) sheet => expected unclear."""
    out = []
    for ch in sheets:   # ch: representative change per sheet, deterministic order
        if len(out) >= n:
            break
        cur, old = load_words(ch["new_sheet"]), load_words(ch["old_sheet"])
        text = " ".join(w["text"] for w in cur + old).lower()
        for k in range(len(UNCLEAR_TEMPLATES)):
            tpl, absent = UNCLEAR_TEMPLATES[(len(out) + k) % len(UNCLEAR_TEMPLATES)]
            if all(a.lower() not in text for a in absent):
                break
        else:
            continue
        loc = loc_label(ch["anchor"], cur)
        region = window(ch["bbox_pt"])
        if not in_window(cur, region) or not any(w["text"] == ch["anchor"] for w in in_window(cur, region)):
            continue   # the named location must be inside the crop
        out.append({"id": f"dev_u{len(out) + 1:02d}", "sheet_number": ch["sheet_number"], "current_sheet": ch["new_sheet"],
                    "superseded_sheet": ch["old_sheet"], "anchor": ch["anchor"], "bbox_pt": [round(v, 1) for v in region],
                    "offset_pt": ch["offset_pt"], "expected": "unclear", "kind": "unclear",
                    "claim": tpl.format(S=ch["sheet_number"], loc=loc),
                    "evidence_tokens": [], "absent_tokens": absent,
                    "provenance": f"label unclear: {absent} occur nowhere on {ch['new_sheet']} or {ch['old_sheet']} "
                                  f"(checked all {len(cur)}+{len(old)} words, case-insensitive), so no crop can support or refute it."})
    return out


def build(verbose=True):
    index = json.loads((ROOT / "build/sheet_index.json").read_text())
    addenda = json.loads((ROOT / "build/addenda_changes.json").read_text())
    bullets_by = {(a["source"], a["sheet_number"]): a["changes"] for a in addenda}
    rows = {r["sheet"]: r for r in index["rows"]}
    per_sheet, summary = [], []
    for o, n in pairs(index):
        ow, nw = load_words(o["sheet"]), load_words(n["sheet"])
        changes, off = diff_pair(o, n)
        built = []
        for c in expand(changes):
            r = build_pair(c, ow, nw, 0)
            if r:
                built.append((c, r))
        per_sheet.append((o, n, ow, nw, off, changes, built))
        summary.append({"sheet": n["sheet_number"], "pair": f"{o['sheet']}->{n['sheet']}", "changes": len(changes), "candidates": len(built)})
    # round-robin across sheets (priority: replacements first) up to the per-sheet cap and TARGET_CHANGES
    def prio(item):
        c, (s, cu) = item
        return (0 if (c["old_tokens"] and c["new_tokens"]) else 1, c["bbox_pt"][1], c["bbox_pt"][0])
    queues = [(o, n, ow, nw, sorted(b, key=prio)) for o, n, ow, nw, off, ch, b in per_sheet if b]
    taken = defaultdict(int)
    chosen, seen_claims, pair_n = [], set(), Counter()
    while len(chosen) < TARGET_CHANGES:
        progressed = False
        # within a round, sheets whose next candidate is a replacement (old -> new) go first
        for o, n, ow, nw, q in sorted(queues, key=lambda t: (prio(t[4][0])[0] if t[4] else 9, t[1]["sheet_number"])):
            sn = n["sheet_number"]
            def dup(item):
                s0, c0 = build_pair(item[0], ow, nw, 0)
                # same change re-issued across two addenda, or the same token swap repeated (A5 -> A6): keep few
                k1 = (s0["kind"], s0["evidence_tokens"][0], c0["evidence_tokens"][0])
                return (sn, s0["claim"]) in seen_claims or pair_n[(sn,) + k1] >= 1 or pair_n[k1] >= 2
            while q and dup(q[0]):
                q.pop(0)
            if q and taken[sn] < MAX_CHANGES_PER_SHEET and len(chosen) < TARGET_CHANGES:
                item = q.pop(0)
                s0, c0 = build_pair(item[0], ow, nw, 0)
                seen_claims.add((sn, s0["claim"]))
                k1 = (s0["kind"], s0["evidence_tokens"][0], c0["evidence_tokens"][0])
                pair_n[(sn,) + k1] += 1
                pair_n[k1] += 1
                chosen.append((o, n, ow, nw, item))
                taken[sn] += 1
                progressed = True
        if not progressed:
            break
    claims, seq = [], 0
    for o, n, ow, nw, (c, _) in chosen:
        seq += 1
        s, cu = build_pair(c, ow, nw, seq)
        bullets = bullets_by.get((n["source"], n["sheet_number"]), [])
        for cl in (s, cu):
            cl["provenance"] = provenance(c, cl, bullets, nw, cl["bbox_pt"])
            cl["superseded_sheet"], cl["current_sheet"] = o["sheet"], n["sheet"]
            cl["change"] = {"old_tokens": c["old_tokens"], "new_tokens": c["new_tokens"], "cluster_bbox_pt": c["bbox_pt"]}
            claims += [cl]
    reps, seen = [], set()
    for o, n, ow, nw, (c, _) in chosen:
        if n["sheet_number"] not in seen and c["anchor"]:
            seen.add(n["sheet_number"])
            reps.append(c)
    claims += unclear_claims(reps[::2] + reps[1::2])
    by_sheet = Counter(c["sheet_number"] for c in claims)
    for s in summary:
        s["claims"] = by_sheet.get(s["sheet"], 0)
    return claims, summary


def load_pack_module():
    spec = importlib.util.spec_from_file_location("pack", Path(__file__).parent / "pack.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def make_packs(claims, packs_dir, dpi=200):
    """pack.py format per claim: crop_current.png, crop_superseded.png (same content window), words.txt, claim.txt, paste file."""
    import pymupdf
    pk = load_pack_module()
    data = Path(os.environ.get("DEVSET_DATA_DIR", ROOT / "data"))   # the bid-set PDFs are gitignored: point this at them
    pk.SRC = {k: str(data / Path(v).name) for k, v in pk.SRC.items()}
    missing = [v for v in pk.SRC.values() if not Path(v).exists()]
    if missing:
        raise SystemExit(f"PDFs not found: {missing}. Put them in {data} or set DEVSET_DATA_DIR to the folder that has them "
                         "(or run with --no-packs).")
    index = json.loads((ROOT / "build/sheet_index.json").read_text())
    rows = {r["sheet"]: r for r in index["rows"]}
    packs_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = packs_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    else:   # regenerate the 3 original examples (pack.py works from cwd-relative paths; we gave it absolute SRC)
        cwd = os.getcwd()
        os.chdir(ROOT)
        try:
            pk.main(str(packs_dir), dpi)
        finally:
            os.chdir(cwd)
        manifest = json.loads(manifest_path.read_text())
    manifest = [m for m in manifest if not m["id"].startswith("dev_")]
    system, _, template = (Path(__file__).parent / "prompts/judge_v0.md").read_text().partition("\n---\n")
    (packs_dir / "1_SYSTEM_INSTRUCTIONS.txt").write_text(system.strip() + "\n")
    for cl in claims:
        d = packs_dir / cl["id"]
        d.mkdir(parents=True, exist_ok=True)
        b, off = cl["bbox_pt"], cl["offset_pt"]
        lines, files = [], {}
        for tag, sheet, o in (("current", cl["current_sheet"], (0.0, 0.0)), ("superseded", cl["superseded_sheet"], (-off[0], -off[1]))):
            row = rows[sheet]
            page = pymupdf.open(pk.SRC[row["source"]])[int(row["sheet"].split("-p")[1]) - 1]
            rect = pymupdf.Rect(b[0] + o[0], b[1] + o[1], b[2] + o[0], b[3] + o[1]) & page.rect
            page.get_displaylist().get_pixmap(matrix=pymupdf.Matrix(dpi / 72, dpi / 72), clip=rect).save(d / f"crop_{tag}.png")
            words = [w for w in load_words(sheet) if rect.contains(pymupdf.Rect(w["bbox"]))]
            files[tag] = {"sheet": sheet, "png": f"crop_{tag}.png", "rect_pt": [round(v, 1) for v in rect]}
            lines += [f"[{tag} sheet {cl['sheet_number']} = {sheet}] words in crop:", " ".join(w["text"] for w in words)]
        (d / "words.txt").write_text("\n".join(lines) + "\n")
        (d / "claim.txt").write_text(cl["claim"] + "\n")
        (d / "2_PASTE_AS_MESSAGE.txt").write_text(
            "IMAGE 1 = CURRENT sheet, IMAGE 2 = SUPERSEDED sheet\n\n"
            + template.strip().replace("{{claim}}", cl["claim"]).replace("{{words}}", "\n".join(lines)) + "\n")
        manifest.append({"id": cl["id"], "claim": cl["claim"], "sheet_number": cl["sheet_number"], "anchor": cl["anchor"],
                         "label": cl["expected"], "why": cl["provenance"], "files": files})
    manifest_path.write_text(json.dumps(manifest, indent=1))
    return manifest


def table(summary, claims):
    lines = [f"{'sheet':8} {'pair':24} {'changes':>7} {'cand.':>5} {'claims':>6}"]
    for s in sorted(summary, key=lambda s: s["sheet"]):
        lines.append(f"{s['sheet']:8} {s['pair']:24} {s['changes']:7d} {s['candidates']:5d} {s['claims']:6d}")
    lab = Counter(c["expected"] for c in claims)
    lines.append(f"total claims {len(claims)}: " + ", ".join(f"{k}={v}" for k, v in sorted(lab.items())))
    return "\n".join(lines)


def main(out="studio/devset", packs="studio/packs", dpi=200, do_packs=True):
    claims, summary = build()
    out = ROOT / out
    out.mkdir(parents=True, exist_ok=True)
    fields = ["id", "claim", "expected", "sheet_number", "current_sheet", "superseded_sheet", "anchor", "bbox_pt",
              "evidence_tokens", "provenance"]
    extra = ["offset_pt", "kind", "absent_tokens", "change"]
    (out / "claims_dev.json").write_text(json.dumps([{k: c[k] for k in fields + extra if k in c} for c in claims], indent=1))
    print(table(summary, claims))
    if do_packs:
        make_packs(claims, ROOT / packs, dpi)
        print("packs ->", ROOT / packs)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="studio/devset")
    ap.add_argument("--packs", default="studio/packs")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--no-packs", action="store_true")
    a = ap.parse_args()
    main(a.out, a.packs, a.dpi, not a.no_packs)
