"""Compare Gemini 4 models as the claim judge on the studio packs: verdict, grounding, latency, cost.

Grounding = each evidence box (model [ymin,xmin,ymax,xmax] 0-1000 on the crop) converted to sheet pt must overlap
PDF words, and every quoted word must appear inside at least one evidence box. Pure code, no model.
Usage (repo root): python studio/compare_models.py [--thinking LOW] [--models a,b,...]
Writes studio/results/compare_<date>.json and prints a table.
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import llm  # noqa: E402

MODELS = ["gemini-4-hydrogen", "gemini-4-carbon", "gemini-4-neon", "gemini-4-argon"]


def to_pt(box, rect):
    y0, x0, y1, x1 = box
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    return [rect[0] + x0 / 1000 * w, rect[1] + y0 / 1000 * h, rect[0] + x1 / 1000 * w, rect[1] + y1 / 1000 * h]


def overlaps(a, b):
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def grounding(ex, j):
    """Return (boxes_on_words, boxes_total, quoted_found, quoted_total)."""
    inside_words, ok_boxes = [], 0
    for e in j["evidence"]:
        f = ex["files"].get(e["image"]) or ex["files"]["current"]
        words = json.loads((ROOT / f"build/words/{f['sheet']}.json").read_text())["words"]
        bb = to_pt(e["box_2d"], f["rect_pt"])
        hit = [w["text"] for w in words if overlaps(w["bbox"], bb)]
        ok_boxes += bool(hit)
        inside_words += hit
    joined = " ".join(inside_words).upper()
    quoted = [q for q in j["quoted_words"] if q.strip()]
    found = sum(all(tok in joined for tok in q.upper().split()) for q in quoted)
    return ok_boxes, len(j["evidence"]), found, len(quoted)


def main(models, thinking):
    packs = ROOT / "studio/packs"
    prompt = (ROOT / "studio/prompts/judge_v0.md").read_text()
    schema = json.loads((ROOT / "studio/prompts/judge.schema.json").read_text())
    rows = []
    for m in models:
        for ex in json.loads((packs / "manifest.json").read_text()):
            d = packs / ex["id"]
            text = prompt.replace("{{claim}}", ex["claim"]).replace("{{words}}", (d / "words.txt").read_text())
            parts = [llm.text_part("IMAGE 1 = CURRENT sheet"), llm.image_part(str(d / "crop_current.png"), "image/png")]
            if (d / "crop_superseded.png").exists():
                parts += [llm.text_part("IMAGE 2 = SUPERSEDED sheet"),
                          llm.image_part(str(d / "crop_superseded.png"), "image/png")]
            parts.append(llm.text_part(text))
            try:
                r = llm.call(m, parts, schema=schema, purpose=f"compare:{ex['id']}", thinking=thinking)
                j = r.json
                g = grounding(ex, j)
                expected = ex["label"] or "conflict"  # ex3: code word-diff says WC-1, so "WC-3" is a conflict
                rows.append({"model": m, "example": ex["id"], "expected": expected, "verdict": j["verdict"],
                             "correct": j["verdict"] == expected, "confidence": j["confidence"],
                             "boxes_on_words": g[0], "boxes": g[1], "quoted_found": g[2], "quoted": g[3],
                             "latency_s": round(r.latency_s, 1), "cost_usd": round(r.cost_usd, 4),
                             "reason": j["reason"]})
            except Exception as e:  # report, don't hide
                rows.append({"model": m, "example": ex["id"], "error": f"{type(e).__name__}: {str(e)[:120]}"})
    out = ROOT / f"studio/results/compare_{dt.date.today()}_{thinking or 'default'}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rows, indent=1))
    print(f"{'model':18} {'example':24} {'exp':10} {'got':10} ok  conf  boxes  quoted  sec   $")
    for r in rows:
        if "error" in r:
            print(f"{r['model']:18} {r['example']:24} ERROR {r['error']}")
            continue
        print(f"{r['model']:18} {r['example']:24} {r['expected']:10} {r['verdict']:10} {'Y' if r['correct'] else 'N'}  "
              f"{r['confidence']:.2f}  {r['boxes_on_words']}/{r['boxes']}    {r['quoted_found']}/{r['quoted']}    "
              f"{r['latency_s']:4}  {r['cost_usd']}")
    for m in models:
        mr = [r for r in rows if r["model"] == m and "error" not in r]
        if mr:
            print(f"SUMMARY {m:18} correct {sum(r['correct'] for r in mr)}/{len(mr)}  "
                  f"grounded boxes {sum(r['boxes_on_words'] for r in mr)}/{sum(r['boxes'] for r in mr)}  "
                  f"quotes {sum(r['quoted_found'] for r in mr)}/{sum(r['quoted'] for r in mr)}  "
                  f"mean {sum(r['latency_s'] for r in mr) / len(mr):.1f}s  ${sum(r['cost_usd'] for r in mr):.3f}")
    print("->", out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--thinking", default="LOW")
    ap.add_argument("--models", default=",".join(MODELS))
    a = ap.parse_args()
    main(a.models.split(","), a.thinking)
