"""Claim -> card (ticket T7). Search finds candidate sheets, a crop around the best hit (plus the superseded
version at the same rect) goes to two judges (verify.py), code checks ground the answer, and the card is built.

    python check.py "All the water closets in Toilet 105A are type WC-2."
"""
import datetime as dt
import hashlib
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf

import llm
import pdflock
import rooms
import search
import verify
from roles import ROLES, THINKING

HERE = Path(__file__).parent
BUILD = HERE / "build"          # symlink to the shared build dir; tests may monkeypatch
PROMPT = HERE / "studio/prompts/judge_v0.md"
SCHEMA = HERE / "studio/prompts/judge.schema.json"
HALF_PT, DPI, K = 260, 200, 3


def _pdf_path(rel):
    for base in (HERE, (HERE / "build").resolve().parent):   # build symlink -> main checkout holds data/
        if (base / rel).exists():
            return base / rel
    raise FileNotFoundError(rel)


def _load():
    sheets = {s["sheet"]: s for s in json.loads((BUILD / "sheets.json").read_text())["sheets"]}
    src = {s["source"]: s["path"] for s in json.loads((BUILD / "sheets.json").read_text())["sources"] if "path" in s}
    rows = {r["sheet"]: r for r in json.loads((BUILD / "sheet_index.json").read_text())["rows"]}
    narr = json.loads((BUILD / "addenda_changes.json").read_text())
    return sheets, src, rows, narr


def _words(sheet):
    return json.loads((BUILD / "words" / f"{sheet}.json").read_text())["words"]


def _inside(words, rect):
    return [w for w in words if rect[0] <= w["bbox"][0] and rect[1] <= w["bbox"][1]
            and w["bbox"][2] <= rect[2] and w["bbox"][3] <= rect[3]]


def crop_rect(bbox, width, height, half=HALF_PT):
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return [max(0.0, cx - half), max(0.0, cy - half), min(float(width), cx + half), min(float(height), cy + half)]


def _norm_tokens(text):
    return {"".join(c for c in t.lower() if c.isalnum()) for t in text.split()}


def _id_tokens(claim):
    return {t for t in _norm_tokens(claim) if any(c.isdigit() for c in t)}


def _crop(sheet, rect, sheets, src, rows):
    s = sheets[sheet]
    words = _inside(_words(sheet), rect)
    return {"sheet": sheet, "source": s["source"], "revision": s["source"], "rect": rect, "words": words,
            "sheet_number": rows.get(sheet, {}).get("sheet_number"), "page": s["page"]}


def render(crop, src):
    """Render the crop at DPI into build/crops/<hash>.png; returns the repo-relative path."""
    key = hashlib.sha1(json.dumps([crop["sheet"], [round(v, 1) for v in crop["rect"]], DPI]).encode()).hexdigest()[:16]
    out = BUILD / "crops" / f"{key}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    with pdflock.LOCK:      # pymupdf is not thread-safe
        page = pymupdf.open(_pdf_path(src[crop["source"]]))[crop["page"] - 1]
        page.get_displaylist().get_pixmap(matrix=pymupdf.Matrix(DPI / 72, DPI / 72), clip=pymupdf.Rect(*crop["rect"])).save(str(out))
    crop["png"], crop["path"] = out, f"build/crops/{key}.png"
    return crop


NEGATIONS = {"no", "not", "never", "none", "without", "nor", "neither", "cannot", "isnt", "arent", "doesnt", "dont",
             "wont", "wasnt", "werent", "hasnt", "havent", "cant"}


def positive_query(claim):
    """Search query with negation words removed ("no grab bar in 105A" -> "grab bar in 105A"). Search only: the judge
    still sees the claim as written. Falls back to the claim if nothing is left."""
    kept = [t for t in claim.split() if "".join(c for c in t.lower() if c.isalpha()) not in NEGATIONS]
    return " ".join(kept) or claim


def named_sheets(claim, rows):
    """Sheet numbers named in the claim (any token equal to a sheet_number once punctuation/case is dropped, so
    C1.10 matches C110). -> {norm_number: sheet_number}."""
    nums = {search.norm_number(r["sheet_number"]): r["sheet_number"] for r in rows.values() if r.get("sheet_number")}
    return {n: nums[n] for n in dict.fromkeys(search.norm_number(t) for t in claim.split()) if n in nums}


TITLE_STRIP = 0.88       # analyze_sheets: the title block is the column at x >= 0.88 * page width (rendered space)
_TITLE_CLAIM = re.compile(r"\b(title|titled|revision|dated?|issue date|scale|sheet (?:number|name|title))\b", re.I)
_FILLER = {"sheet", "sheets", "on", "per", "see"}   # "On P101, ..." / "Sheet P101 shows ...": locators, not content


def is_title_claim(claim):
    """True if the claim is about title-block content (sheet title, revision, date, scale)."""
    return bool(_TITLE_CLAIM.search(claim))


def search_text(claim, named):
    """Claim -> search/identifier text once the sheet is resolved: negation words, the named sheet number(s) and
    locator filler ("On", "Sheet") removed. The judge still sees the claim as written."""
    kept = [t for t in positive_query(claim).split()
            if search.norm_number(t) not in named and "".join(c for c in t.lower() if c.isalpha()) not in _FILLER]
    return " ".join(kept) or positive_query(claim)


def in_title_strip(x, width):
    return x >= TITLE_STRIP * width


def hits_on(query, sheet, limit=15):
    """Search hits on one sheet in score order (the index's scored candidates, one box per distinct word set)."""
    idx = search.get_index()
    cand, terms = idx._candidates(query)
    out = []
    for sc, d in cand:
        if d["sheet"] == sheet:
            out.append(idx._hit(sc, d, terms))
            if len(out) >= limit:
                break
    return out


def _centre_x(rect):
    return (rect[0] + rect[2]) / 2


def best_crop_on(claim, query, sheet, sheets, src, rows, allow_title):
    """Best crop on one sheet: hits are ranked by the claim's identifiers (105A, WC-2) found in their crop, then by
    search order. A crop centred in the title-block strip is skipped unless the claim is about the title block."""
    s, best = sheets[sheet], None
    for i, h in enumerate(hits_on(query, sheet)):
        c = _crop(sheet, crop_rect(h["bbox"], s["width"], s["height"]), sheets, src, rows)
        if not allow_title and in_title_strip(_centre_x(c["rect"]), s["width"]):
            continue
        sc = _score(claim, c, i, allow_title, superseded_twin(c, sheets, src, rows))
        if best is None or sc > best[0]:
            best = (sc, c)
    return best[1] if best else None


def _tokens(crop):
    return set().union(*(_norm_tokens(w["text"]) for w in crop["words"])) if crop and crop["words"] else set()


def _score(claim, crop, i, title=False, twin=None):
    """(claim tokens found in the crop or its superseded twin, tokens found in the crop itself, -search rank).
    Content claims count identifiers (105A, WC-2); title-block claims count every word (the title text sits above
    the big sheet-number label, in the same strip). The twin counts because a stale claim quotes the OLD value
    ("room 103 is 19\""), which only the superseded sheet still shows: without it a lower-right "TWOS 103" label
    ties the real list entry on the room number alone and wins on search order."""
    want = {t for t in _norm_tokens(claim) if t} if title else _id_tokens(claim)
    have = _tokens(crop)
    return (len(want & (have | _tokens(twin))), len(want & have), -i)


def candidates(claim, sheets, src, rows, n=2, k=4):
    """-> (list of up to n crops, list of note strings).
    A claim naming a sheet number is judged on that sheet's CURRENT version only (noting any superseded version);
    the sheet number and locator words are dropped from the search and identifier scoring, so hits are ranked by
    the remaining tokens. Otherwise the top-k search hits are re-ranked by how many of the claim's identifiers their
    crop holds, superseded hits are swapped for their current sheet, and the best n distinct sheets are kept.
    Crops centred in the title-block strip are never chosen unless the claim is about the title block."""
    notes, out = [], []
    named = named_sheets(claim, rows)
    title = is_title_claim(claim)
    query = positive_query(claim) if title else search_text(claim, named)
    idclaim = positive_query(claim) if title else query     # identifier scoring text: no sheet number
    for num in named.values():
        vers = [r for r in rows.values() if r.get("sheet_number") == num and r["sheet"] in sheets]
        if not vers:
            continue            # numbered in sheet_index but its source is not loaded
        cur = max([r for r in vers if r.get("current") is not False] or vers, key=lambda r: r["issue_order"])
        crop = best_crop_on(idclaim, query, cur["sheet"], sheets, src, rows, title)
        if crop is None:
            continue
        old = [r["sheet"] for r in vers if r["sheet"] != cur["sheet"] and r.get("current") is False]
        notes.append(f"claim names sheet {num}: judging current {cur['sheet']}"
                     + (f" (superseded: {', '.join(old)})" if old else ""))
        out.append(crop)
    if out:
        return out[:n], notes
    scored = []
    for i, h in enumerate(search.search_sheets(query, k=k)):
        sid = h["sheet"]
        if rows.get(sid, {}).get("current") is False and h.get("superseded_by") in sheets:
            sid = h["superseded_by"]           # judge the current version, at the hit's rect
        s = sheets[sid]
        c = _crop(sid, crop_rect(h["bbox"], s["width"], s["height"]), sheets, src, rows)
        if not title and in_title_strip(_centre_x(c["rect"]), s["width"]):
            c = best_crop_on(idclaim, query, sid, sheets, src, rows, False)
            if c is None:
                continue
        scored.append((_score(idclaim, c, i, title, superseded_twin(c, sheets, src, rows)), c))
    seen, out = set(), []
    for _, c in sorted(scored, key=lambda t: t[0], reverse=True):
        if c["sheet"] not in seen:
            seen.add(c["sheet"])
            out.append(c)
    return out[:n], notes


def pick(claim, sheets, src, rows):
    """Best single candidate (for tests/tools)."""
    c, _ = candidates(claim, sheets, src, rows, n=1)
    return c[0] if c else None


def superseded_twin(cur, sheets, src, rows):
    num = rows.get(cur["sheet"], {}).get("sheet_number")
    olds = [r for r in rows.values() if r.get("sheet_number") == num and r["sheet"] != cur["sheet"]
            and r.get("current") is False and r["sheet"] in sheets]
    if not olds:
        return None
    old = max(olds, key=lambda r: r["issue_order"])
    dx, dy, n = page_offset(_words(old["sheet"]), _words(cur["sheet"]), rect=cur["rect"])
    o, c = sheets[old["sheet"]], cur["rect"]
    rect = [max(0.0, c[0] - dx), max(0.0, c[1] - dy), min(float(o["width"]), c[2] - dx), min(float(o["height"]), c[3] - dy)]
    twin = _crop(old["sheet"], rect, sheets, src, rows)
    twin["offset"], twin["offset_matches"] = (dx, dy), n
    return twin


def page_offset(old_words, cur_words, rect=None, k=8, min_len=3, min_matches=3):
    """(dx, dy, n): how far the content moved between two issues of a sheet (current minus old position), from words
    that occur exactly once on both sheets (len >= min_len). Reissues move regions by different amounts (ED200
    shifts 0, 50, 70 and 90 pt in y in different parts of one sheet), so with `rect` (the crop on the current
    sheet) only the k matched words closest to its centre vote, and the most common rounded (dx, dy) wins (ties:
    smaller shift). Without `rect` all matched words vote. (0, 0, n) when fewer than min_matches words match."""
    def once(ws):
        seen = {}
        for w in ws:
            seen.setdefault(w["text"], []).append(w["bbox"])
        return {t: b[0] for t, b in seen.items() if len(b) == 1 and len(t) >= min_len}
    A, B = once(old_words), once(cur_words)
    m = [(B[t], (round(B[t][0] - A[t][0]), round(B[t][1] - A[t][1]))) for t in A if t in B]
    if rect is not None:
        cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
        m.sort(key=lambda t: (t[0][0] - cx) ** 2 + (t[0][1] - cy) ** 2)
        m = m[:k]
    if len(m) < min_matches:
        return 0.0, 0.0, len(m)
    (dx, dy), n = max(Counter(v for _, v in m).items(), key=lambda kv: (kv[1], -abs(kv[0][0]) - abs(kv[0][1])))
    return float(dx), float(dy), n


EXTRA_NOTE = ("{label}. This is a FIELD PHOTO, not a drawing sheet. Compare what it shows with the drawing crop(s) when "
              "judging the claim. Your verdict is still about the CURRENT drawing sheet; evidence boxes and quoted "
              "words must come from the drawing images (IMAGE 1 / IMAGE 2) only, never from this photo.")


def build_parts(claim, crops, extra_images=None):
    system, _, template = PROMPT.read_text().partition("\n---\n")
    lines = []
    parts = [llm.text_part(system.strip())]
    for label, key in (("IMAGE 1 = CURRENT sheet", "current"), ("IMAGE 2 = SUPERSEDED sheet", "superseded")):
        c = crops.get(key)
        if c:
            parts += [llm.text_part(f"{label} {c['sheet_number']} ({c['sheet']})"), llm.image_part(c["png"], "image/png")]
            lines += [f"[{key} sheet {c['sheet_number']} = {c['sheet']}] words in crop:", " ".join(w["text"] for w in c["words"])]
    for x in extra_images or []:
        parts += [llm.text_part(EXTRA_NOTE.format(label=x.get("label") or "FIELD PHOTO")),
                  llm.image_part(x["path"], x.get("mime", "image/jpeg"))]
    parts.append(llm.text_part(template.strip().replace("{{claim}}", claim).replace("{{words}}", "\n".join(lines))))
    return parts


def _room_geometry(claim, crop):
    """Room geometry of the crop's sheet when the claim names a room number (105A); None otherwise or when the
    extraction fails, so the spatial check is simply skipped. Runs in the main thread (pymupdf is not thread-safe)."""
    if not rooms.claim_room_tokens(claim):
        return None
    try:
        return rooms.sheet_rooms(crop["sheet"], build=BUILD)
    except Exception:
        return None


def _run_candidate(claim, cur, judge, second, thinking, sheets, src, rows, narr, schema, progress=None, extra_images=None):
    crops = {"current": cur}
    if cur.get("twin"):
        crops["superseded"] = cur["twin"]
    r = rows.get(cur["sheet"], {})
    narrative = [{"action": ch["action"], "text": ch["text"]} for n in narr
                 if n["source"] == cur["source"] and n["sheet_number"] == r.get("sheet_number") for ch in n["changes"]]
    ctx = {"judge": judge, "second": second, "thinking": thinking, "schema": schema,
           "parts": build_parts(claim, crops, extra_images), "crops": crops, "rows": rows, "narrative": narrative,
           "progress": progress}
    return cur, verify.verify(claim, ctx)


def _rank(res):
    """Grounding must pass, then a real verdict beats unclear, then higher confidence. Ties keep search order."""
    g = next((c for c in res["checks"] if c.get("check") == "grounding"), {})
    return (bool(g.get("pass")), res["verdict"] != "unclear", res["confidence"])


def check(claim, *, judge=ROLES["judge"], second=ROLES["second"], thinking=THINKING, progress=None, extra_images=None):
    """progress(step, status, **info), optional. step: search | crop | judge_a | judge_b | grounding | revision;
    status: start | done. Judge/grounding/revision events carry cand=<index> and sheet=<id>, one set per candidate;
    they are called from worker threads. extra_images: optional [{"path", "mime", "label"}] field photos (e.g. a
    site-video frame crop) sent to both judges beside the drawing crops; verdict semantics are unchanged."""
    emit = progress or (lambda *a, **k: None)
    sheets, src, rows, narr = _load()
    emit("search", "start")
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    card = {"claim": claim, "speaker": None, "timestamp": now, "verdict": "unclear", "confidence": 0.0,
            "evidence": [], "spec_refs": [], "checks": [], "reason": "", "rfi_draft": None}
    cands, notes = candidates(claim, sheets, src, rows)
    emit("search", "done", hits=len(cands), sheet=cands[0]["sheet"] if cands else None)
    if not cands:
        card["reason"] = "No sheet matched the claim."
        card["checks"].append({"by": "code", "check": "search", "pass": False, "detail": "no search hits"})
        return card
    schema = json.loads(SCHEMA.read_text())
    emit("crop", "start")
    for c in cands:          # render here (pymupdf is not thread-safe), then judge every candidate at once
        render(c, src)
        twin = superseded_twin(c, sheets, src, rows)
        c["twin"] = render(twin, src) if twin else None
        c["rooms"] = _room_geometry(claim, c)
    emit("crop", "done", superseded=any(c["twin"] for c in cands))

    def per_cand(i, c):
        return None if progress is None else (lambda step, status, **info: progress(step, status, cand=i, sheet=c["sheet"], **info))
    # each candidate runs a judge pair concurrently: at most 2 x 2 = 4 model calls in flight
    with ThreadPoolExecutor(max_workers=len(cands)) as ex:
        results = list(ex.map(lambda ic: _run_candidate(claim, ic[1], judge, second, thinking, sheets, src, rows, narr,
                                                        schema, per_cand(*ic), extra_images), enumerate(cands)))
    best = max(range(len(results)), key=lambda i: (_rank(results[i][1]), -i))
    cur, res = results[best]
    card.update(verdict=res["verdict"], confidence=res["confidence"], reason=res["reason"], checks=list(res["checks"]))
    for n in notes:
        card["checks"].append({"by": "code", "check": "named_sheet", "info": True, "pass": True, "detail": n})
    for i, (c, r) in enumerate(results):
        if i != best:
            card["checks"].append({"by": "code", "check": "alternative_candidate", "info": True, "pass": True,
                                   "sheet": c["sheet"], "chosen_sheet": cur["sheet"], "verdict": r["verdict"],
                                   "confidence": r["confidence"], "grounding_pass": _rank(r)[0], "detail": r["reason"]})
    # Only boxes on the current sheet are evidence; the superseded image only explains what changed.
    card["evidence"] = [{"sheet": cur["sheet"], "revision": cur["revision"], "bbox": [round(v, 2) for v in e["bbox"]],
                         "sheet_number": cur["sheet_number"], "crop": cur["path"], "crop_rect_pt": [round(v, 1) for v in cur["rect"]], "label": e["label"]} for e in res["evidence"] if e["image"] == "current"]
    room = next((c.get("room") for c in card["checks"] if c.get("check") == "spatial"), None)
    if room:        # the room region in crop 0-1000 coordinates, so the UI can highlight it
        for e in card["evidence"]:
            e["room"] = {"number": room["number"], "name": room["name"], "room_box_2d": room["room_box_2d"]}
    return card


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit('usage: python check.py "claim"')
    print(json.dumps(check(" ".join(sys.argv[1:])), indent=1))
