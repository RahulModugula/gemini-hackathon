"""Build AI Studio packs: for each example claim, crops of the current (and superseded) sheet around an anchor
word, the PDF words inside each crop, and the claim text. Paste these into AI Studio with studio/prompts/judge_v0.md.

Usage (from the repo root): python studio/pack.py [--out studio/packs] [--dpi 200]
"""
import argparse
import json
from collections import Counter
from pathlib import Path

import pymupdf

SRC = {"dwg": "data/outagamie-ymca-childcare-bid-set-drawings.pdf",
       "add1": "data/addendum-1.pdf", "add2": "data/addendum-2.pdf"}

# Dev examples only (never held-out). label = expected verdict, with how we know it.
EXAMPLES = [
    {"id": "ex1_wc_105a_all_wc2", "claim": "All the water closets in Toilet 105A are type WC-2.",
     "sheet_number": "P101", "anchor": "105A", "label": "conflict",
     "why": "True on base P101 (superseded). Addendum 1 P101 retags two of them WC-3 inside a revision cloud with "
            "delta 1 (word diff WC-2 -> WC-3; Add 1 narrative 'REVISE: Water closet tags in Toilet 105A')."},
    {"id": "ex2_wc_105a_add1_change", "claim": "Addendum 1 changed two water closet tags in Toilet 105A from WC-2 to WC-3.",
     "sheet_number": "P101", "anchor": "105A", "label": "consistent",
     "why": "Word diff: exactly two WC-2 removed and two WC-3 added near 105A; revision cloud + delta 1 on the current sheet. "
            "Trap: the third water closet below stays WC-2, so 'all are WC-3' would be wrong."},
    {"id": "ex3_wc_109a", "claim": "Toilet 109A has a WC-3 water closet.",
     "sheet_number": "P101", "anchor": "109A", "label": None,
     "why": "Unlabelled on purpose: label it yourself in AI Studio by reading the crops, then compare with the word diff in manifest.json."},
]


def rows_for(index, num):
    rs = [r for r in index["rows"] if r["sheet_number"] == num]
    return next(r for r in rs if r["current"]), [r for r in rs if not r["current"]]


def crop(row, anchor, out_png, half=260, dpi=200):
    words = json.loads(Path(f"build/words/{row['sheet']}.json").read_text())["words"]
    hit = next(w for w in words if w["text"] == anchor)
    cx, cy = (hit["bbox"][0] + hit["bbox"][2]) / 2, (hit["bbox"][1] + hit["bbox"][3]) / 2
    page = pymupdf.open(SRC[row["source"]])[row["page"] - 1] if "page" in row else \
        pymupdf.open(SRC[row["source"]])[int(row["sheet"].split("-p")[1]) - 1]
    rect = pymupdf.Rect(cx - half, cy - half, cx + half, cy + half) & page.rect
    page.get_displaylist().get_pixmap(matrix=pymupdf.Matrix(dpi / 72, dpi / 72), clip=rect).save(out_png)
    inside = [w for w in words if rect.contains(pymupdf.Rect(w["bbox"]))]
    return rect, inside


def main(out="studio", dpi=200):
    index = json.loads(Path("build/sheet_index.json").read_text())
    out = Path(out)
    manifest = []
    for ex in EXAMPLES:
        d = out / ex["id"]
        d.mkdir(parents=True, exist_ok=True)
        cur, old = rows_for(index, ex["sheet_number"])
        r, w_cur = crop(cur, ex["anchor"], d / "crop_current.png", dpi=dpi)
        files = {"current": {"sheet": cur["sheet"], "png": "crop_current.png", "rect_pt": [round(v, 1) for v in r]}}
        lines = [f"[current sheet {ex['sheet_number']} = {cur['sheet']}] words in crop:", " ".join(w["text"] for w in w_cur)]
        if old:
            r2, w_old = crop(old[0], ex["anchor"], d / "crop_superseded.png", dpi=dpi)
            files["superseded"] = {"sheet": old[0]["sheet"], "png": "crop_superseded.png", "rect_pt": [round(v, 1) for v in r2]}
            lines += [f"[superseded sheet {ex['sheet_number']} = {old[0]['sheet']}] words in crop:", " ".join(w["text"] for w in w_old)]
            a, b = Counter(w["text"] for w in w_old), Counter(w["text"] for w in w_cur)
            files["word_diff"] = {"only_superseded": dict(a - b), "only_current": dict(b - a)}
        (d / "words.txt").write_text("\n".join(lines) + "\n")
        (d / "claim.txt").write_text(ex["claim"] + "\n")
        # Paste-ready files for AI Studio: no manual {{...}} replacement needed.
        system, _, template = (Path(__file__).parent / "prompts/judge_v0.md").read_text().partition("\n---\n")
        (out / "1_SYSTEM_INSTRUCTIONS.txt").write_text(system.strip() + "\n")
        images = "IMAGE 1 = CURRENT sheet" + (", IMAGE 2 = SUPERSEDED sheet" if old else "")
        (d / "2_PASTE_AS_MESSAGE.txt").write_text(
            images + "\n\n" + template.strip().replace("{{claim}}", ex["claim"]).replace("{{words}}", "\n".join(lines)) + "\n")
        manifest.append({**ex, "files": files})
        print(ex["id"], "->", d, files.get("word_diff", ""))
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="studio/packs")
    ap.add_argument("--dpi", type=int, default=200)
    a = ap.parse_args()
    main(a.out, a.dpi)
