"""Deterministic DEV claim set from the addenda (dev only; the held-out set is human-written).

For every superseded -> current sheet pair: spatial word diff (global offset estimated from unique tokens),
cluster changed words (<=150 pt), keep clusters with tag-like tokens, then build claims whose labels hold by
construction:
  * stale   : asserts the OLD value (N x token in a small region) -> conflict
  * current : asserts the NEW value                                 -> consistent
  * unclear : asks for a fact (a manufacturer) that is not on the sheet at all -> unclear
Counts are measured on the words, never guessed, and capped at 3.

Usage (repo root): python studio/devset.py [--out studio/devset] [--packs studio/packs] [--dpi 200] [--no-packs]
"""
import argparse
import importlib.util
import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN_DATA = Path("/Users/rmodugula/projects/gemini-hackathon/data")  # PDFs are not tracked in git
CLUSTER_PT = 150.0
TOL_PT = 4.0            # position tolerance after the global offset is removed
WINDOW_HALF = 220.0     # claim window = the crop the judge sees: square of this half-size around the change (pt)
ANCHOR_MAX_PT = 150.0   # a room-number anchor must be this close to the change
MAX_CHANGES_PER_SHEET = 3   # a stale+current pair per change => at most 6 claims per sheet
TARGET_CHANGES = 22
TITLE_BLOCK = (2700.0, 1800.0)  # words right/below this point are title block / revision stamp, not content
WORD_NUMS = {1: "one", 2: "two", 3: "three"}

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


def nearest_anchor(bbox, words, exclude=()):
    """Nearest room-number word (###/###A) to the bbox centre within 400 pt, else nearest tag word, else None.
    Words whose text is one of the changed tokens (`exclude`) are never anchors."""
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    freq = Counter(w["text"] for w in words)
    area_lines = {(w.get("block"), w.get("line")) for w in words if w["text"] in {"SF", "SF/Occ", "Occs", "AREA", "Occ."}}
    words = [w for w in words if w["text"] not in exclude and freq[w["text"]] <= 3
             and (w.get("block"), w.get("line")) not in area_lines]  # repeats / area-table cells are not room tags
    for pool in ([w for w in words if is_room(w["text"]) and not in_title_block(w)],):  # room numbers only: claims read naturally
        best, bd = None, ANCHOR_MAX_PT
        for w in pool:
            x, y = center(w)
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if d < bd:
                best, bd = w, d
        if best is not None:
            return best["text"], ("room" if is_room(best["text"]) else "tag")
    return None, None


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
        if anchor is None:
            continue
        changes.append({
            "sheet_number": new_row["sheet_number"], "old_sheet": old_row["sheet"], "new_sheet": new_row["sheet"],
            "old_tokens": [t for t in otok if o_only[t] > 0], "new_tokens": [t for t in ntok if n_only[t] > 0],
            "bbox_pt": [round(v, 1) for v in bbox], "anchor": anchor, "anchor_kind": akind,
            "offset_pt": [round(off[0], 2), round(off[1], 2)],
            "_okeys": okeys, "_nkeys": nkeys,
        })
    return changes, off


def pairs(index):
    by = {r["sheet"]: r for r in index["rows"]}
    return [(r, by[r["superseded_by"]]) for r in index["rows"] if not r["current"] and r["superseded_by"]]


# ---------------------------------------------------------------- claims
def phrase(tok, n, where):
    times = "" if n == 1 else f" {WORD_NUMS[n]} times"
    return f'The current sheet shows "{tok}"{times} {where}.'


def build_pair(ch, old_words, new_words, seq):
    """Turn one verified change into (stale, current) claim dicts, or None when counts are not clean (n>3 / ambiguous)."""
    off = ch["offset_pt"]
    okeys, nkeys = ch["_okeys"], ch["_nkeys"]
    old_it = okeys[0] if okeys else None
    ref = old_it[2] if old_it else None
    if nkeys:   # pick the added tag nearest the removed one (or the first one for pure additions)
        nk = min(nkeys, key=lambda i: gap(i[2], ref) if ref else reading_order(i))
    else:
        nk = None
    boxes = [b for b in ([old_it[2]] if old_it else []) + ([nk[2]] if nk else [])]
    region = window(union(boxes))
    old_region = [region[0] - off[0], region[1] - off[1], region[2] - off[0], region[3] - off[1]]
    otok = old_it[1]["text"] if old_it else None
    ntok = nk[1]["text"] if nk else None
    if otok == ntok:
        return None
    where = (f"next to room {ch['anchor']}" if ch["anchor_kind"] == "room" else f"near {ch['anchor']}") if ch["anchor"] else "in this area"
    base = {"sheet_number": ch["sheet_number"], "current_sheet": ch["new_sheet"], "superseded_sheet": ch["old_sheet"],
            "anchor": ch["anchor"], "bbox_pt": [round(v, 1) for v in region], "offset_pt": off}
    stale = current = None
    # stale: claim asserts the OLD token count seen on the superseded sheet; false now because it dropped
    if otok:
        n_old, n_cur = count_in(old_words, old_region, otok), count_in(new_words, region, otok)
        if 1 <= n_old <= 3 and n_cur < n_old:
            stale = {**base, "id": f"dev_{seq:02d}s", "claim": phrase(otok, n_old, where), "expected": "conflict",
                     "evidence_tokens": [otok] * n_old, "kind": "stale"}
    # current: claim asserts the NEW token count on the current sheet; it was lower on the superseded sheet
    if ntok:
        n_new, n_was = count_in(new_words, region, ntok), count_in(old_words, old_region, ntok)
        if 1 <= n_new <= 3 and n_was < n_new:
            current = {**base, "id": f"dev_{seq:02d}c", "claim": phrase(ntok, n_new, where), "expected": "consistent",
                       "evidence_tokens": [ntok] * n_new, "kind": "current"}
    elif otok and stale:   # pure removal: "no X here" is true now
        current = {**base, "id": f"dev_{seq:02d}c", "claim": f'The current sheet shows no "{otok}" {where}.',
                   "expected": "consistent", "evidence_tokens": [], "absent_tokens": [otok], "kind": "current"}
    if stale and not current and ntok is None:
        return None
    if not (stale and current):
        # additions: the stale claim is "sheet shows no <new token>", false because the new token is there
        if current and not stale and ntok and count_in(old_words, old_region, ntok) == 0:
            stale = {**base, "id": f"dev_{seq:02d}s", "claim": f'The current sheet shows no "{ntok}" {where}.', "expected": "conflict",
                     "evidence_tokens": [ntok] * current["evidence_tokens"].__len__(), "kind": "stale_absence"}
        else:
            return None
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
    how = {"stale": f"label conflict: the superseded sheet has {len(claim['evidence_tokens'])}x \"{claim['evidence_tokens'][0]}\" in the region, the current sheet has fewer",
           "stale_absence": f"label conflict: \"{claim['evidence_tokens'][0] if claim['evidence_tokens'] else ''}\" is absent on the superseded sheet region but present on the current sheet, so 'shows no' is false",
           "current": f"label consistent: the current sheet region has exactly {len(claim['evidence_tokens'])}x the asserted token and the superseded sheet had fewer" if claim['evidence_tokens'] else "label consistent: the token was removed, none remain in the region"}[claim["kind"]]
    return f"{how}. {diff}{bullet}."


MANUFACTURERS = ["Kohler", "Sloan Valve", "Lutron", "Trane", "Armstrong", "Zurn", "Cooper Lighting", "Schneider Electric", "Carrier", "Mohawk"]


def unclear_claims(sheets, n=5):
    """`n` claims about a manufacturer that appears nowhere on the (current or superseded) sheet => expected unclear."""
    out, used = [], 0
    for ch in sheets:   # ch: representative change per sheet, deterministic order
        if len(out) >= n:
            break
        cur, old = load_words(ch["new_sheet"]), load_words(ch["old_sheet"])
        text = " ".join(w["text"] for w in cur + old).lower()
        for k in range(len(MANUFACTURERS)):
            m = MANUFACTURERS[(used + k) % len(MANUFACTURERS)]
            if m.split()[0].lower() not in text:
                break
        else:
            continue
        used += k + 1
        where = f"room {ch['anchor']}" if ch["anchor_kind"] == "room" else (ch["anchor"] or "this area")
        region = window(ch["bbox_pt"])
        out.append({"id": f"dev_u{len(out) + 1:02d}", "sheet_number": ch["sheet_number"], "current_sheet": ch["new_sheet"],
                    "superseded_sheet": ch["old_sheet"], "anchor": ch["anchor"], "bbox_pt": [round(v, 1) for v in region],
                    "offset_pt": ch["offset_pt"], "expected": "unclear", "kind": "unclear",
                    "claim": f"The product at {where} on sheet {ch['sheet_number']} is manufactured by {m}.",
                    "evidence_tokens": [], "absent_tokens": [m.split()[0]],
                    "provenance": f"label unclear: the word '{m.split()[0]}' occurs nowhere on {ch['new_sheet']} or {ch['old_sheet']} "
                                  f"(checked all {len(cur)}+{len(old)} words, case-insensitive), so no crop can support or refute a manufacturer."})
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
        for c in changes:
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
                return (sn, s0["claim"]) in seen_claims or pair_n[(sn, s0["evidence_tokens"][0], c0["evidence_tokens"][:1].__repr__())] >= 1 \
                    or pair_n[(s0["evidence_tokens"][0], c0["evidence_tokens"][:1].__repr__())] >= 2
            while q and dup(q[0]):
                q.pop(0)
            if q and taken[sn] < MAX_CHANGES_PER_SHEET and len(chosen) < TARGET_CHANGES:
                item = q.pop(0)
                s0, c0 = build_pair(item[0], ow, nw, 0)
                seen_claims.add((sn, s0["claim"]))
                pair_n[(sn, s0["evidence_tokens"][0], c0["evidence_tokens"][:1].__repr__())] += 1
                pair_n[(s0["evidence_tokens"][0], c0["evidence_tokens"][:1].__repr__())] += 1
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
    data = ROOT / "data"
    pk.SRC = {k: str(data / Path(v).name if (data / Path(v).name).exists() else MAIN_DATA / Path(v).name) for k, v in pk.SRC.items()}
    index = json.loads((ROOT / "build/sheet_index.json").read_text())
    rows = {r["sheet"]: r for r in index["rows"]}
    packs_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = packs_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    else:   # regenerate the 3 original examples (pack.py works from cwd-relative paths; we gave it absolute SRC)
        import os
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
