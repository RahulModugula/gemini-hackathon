"""Claim -> card (ticket T7). Search finds candidate sheets, a crop around the best hit (plus the superseded
version at the same rect) goes to two judges (verify.py), code checks ground the answer, and the card is built.

    python check.py "All the water closets in Toilet 105A are type WC-2."
"""
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

import pymupdf

import llm
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
    page = pymupdf.open(_pdf_path(src[crop["source"]]))[crop["page"] - 1]
    page.get_displaylist().get_pixmap(matrix=pymupdf.Matrix(DPI / 72, DPI / 72), clip=pymupdf.Rect(*crop["rect"])).save(str(out))
    crop["png"], crop["path"] = out, f"build/crops/{key}.png"
    return crop


def pick(claim, hits, sheets, src, rows):
    """Prefer current sheets; among the top candidates take the one whose crop holds the most of the claim's
    identifiers (105A, WC-2, ...). Ties keep search order. Returns (crop dict of the current sheet, superseded id)."""
    want, best = _id_tokens(claim), None
    for i, h in enumerate(hits):
        sid, bbox = h["sheet"], h["bbox"]
        if rows.get(sid, {}).get("current") is False and h.get("superseded_by") in sheets:
            sid = h["superseded_by"]           # judge the current version, at the hit's rect
        s = sheets[sid]
        rect = crop_rect(bbox, s["width"], s["height"])
        c = _crop(sid, rect, sheets, src, rows)
        have = set().union(*(_norm_tokens(w["text"]) for w in c["words"])) if c["words"] else set()
        score = (len(want & have), -i)
        if best is None or score > best[0]:
            best = (score, c)
    return best[1] if best else None


def superseded_twin(cur, sheets, src, rows):
    num = rows.get(cur["sheet"], {}).get("sheet_number")
    olds = [r for r in rows.values() if r.get("sheet_number") == num and r["sheet"] != cur["sheet"]
            and r.get("current") is False and r["sheet"] in sheets]
    if not olds:
        return None
    old = max(olds, key=lambda r: r["issue_order"])
    return _crop(old["sheet"], cur["rect"], sheets, src, rows)


def build_parts(claim, crops):
    system, _, template = PROMPT.read_text().partition("\n---\n")
    lines = []
    parts = [llm.text_part(system.strip())]
    for label, key in (("IMAGE 1 = CURRENT sheet", "current"), ("IMAGE 2 = SUPERSEDED sheet", "superseded")):
        c = crops.get(key)
        if c:
            parts += [llm.text_part(f"{label} {c['sheet_number']} ({c['sheet']})"), llm.image_part(c["png"], "image/png")]
            lines += [f"[{key} sheet {c['sheet_number']} = {c['sheet']}] words in crop:", " ".join(w["text"] for w in c["words"])]
    parts.append(llm.text_part(template.strip().replace("{{claim}}", claim).replace("{{words}}", "\n".join(lines))))
    return parts


def check(claim, *, judge=ROLES["judge"], second=ROLES["second"], thinking=THINKING):
    sheets, src, rows, narr = _load()
    hits = search.search_sheets(claim, k=K)
    cur = pick(claim, hits, sheets, src, rows)
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    card = {"claim": claim, "speaker": None, "timestamp": now, "verdict": "unclear", "confidence": 0.0,
            "evidence": [], "spec_refs": [], "checks": [], "reason": "", "rfi_draft": None}
    if cur is None:
        card["reason"] = "No sheet matched the claim."
        card["checks"].append({"by": "code", "check": "search", "pass": False, "detail": "no search hits"})
        return card
    render(cur, src)
    crops = {"current": cur}
    old = superseded_twin(cur, sheets, src, rows)
    if old:
        crops["superseded"] = render(old, src)
    r = rows.get(cur["sheet"], {})
    narrative = [{"action": ch["action"], "text": ch["text"]} for n in narr
                 if n["source"] == cur["source"] and n["sheet_number"] == r.get("sheet_number") for ch in n["changes"]]
    ctx = {"judge": judge, "second": second, "thinking": thinking, "schema": json.loads(SCHEMA.read_text()),
           "parts": build_parts(claim, crops), "crops": crops, "rows": rows, "narrative": narrative}
    res = verify.verify(claim, ctx)
    card.update(verdict=res["verdict"], confidence=res["confidence"], reason=res["reason"], checks=res["checks"])
    # Only boxes on the current sheet are evidence; the superseded image only explains what changed.
    card["evidence"] = [{"sheet": cur["sheet"], "revision": cur["revision"], "bbox": [round(v, 2) for v in e["bbox"]],
                         "crop": cur["path"], "crop_rect_pt": [round(v, 1) for v in cur["rect"]], "label": e["label"]} for e in res["evidence"] if e["image"] == "current"]
    return card


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit('usage: python check.py "claim"')
    print(json.dumps(check(" ".join(sys.argv[1:])), indent=1))
