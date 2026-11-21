"""Naive single-call baseline, scored like eval/run_eval.py on the same frozen held-out sets.

For each claim: render the WHOLE sheet image, ONE gemini-4-argon call (thinking LOW), verdict taken as-is.
No crop, no superseded twin, no words list, no second judge, no code verification.
  baseline         : the claim's own current sheet (sheet given)
  baseline_search  : the top search.search_sheets hit (sheet NOT given; a superseded hit is swapped for its current sheet)

Usage: python tools/baseline_eval.py eval/heldout_v2.json eval/heldout_v3.json --out build/metrics_baseline_v2v3.json
"""
import argparse
import datetime as dt
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import pymupdf  # noqa: E402

import check  # noqa: E402
import llm  # noqa: E402
import pdflock  # noqa: E402
import search  # noqa: E402
from roles import ROLES  # noqa: E402
from run_eval import frozen_ok, wilson  # noqa: E402

MAX_PX = 3072
PROMPT = ("Here is a construction drawing sheet. Claim: {claim}. "
          "Answer with JSON {{verdict: conflict|consistent|unclear, reason}}.")
SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["conflict", "consistent", "unclear"]},
                                           "reason": {"type": "string"}}, "required": ["verdict", "reason"]}


def render_whole(sheet, sheets, src):
    s = sheets[sheet]
    scale = MAX_PX / max(s["width"], s["height"])
    with pdflock.LOCK:
        page = pymupdf.open(check._pdf_path(src[s["source"]]))[s["page"] - 1]
        return page.get_displaylist().get_pixmap(matrix=pymupdf.Matrix(scale, scale)).tobytes("png")


def ask(sheet, claim, sheets, src):
    png = render_whole(sheet, sheets, src)
    r = llm.call(ROLES["judge"], [llm.image_part(png, "image/png"), llm.text_part(PROMPT.format(claim=claim))],
                 schema=SCHEMA, purpose="baseline", thinking="LOW")
    return r.json["verdict"], r.json["reason"], r.cost_usd


def run_one(c, variant, sheets, src, rows):
    out = {"id": c["id"], "expected": c["expected"], "variant": variant}
    try:
        if variant == "baseline":
            sheet = c["current_sheet"]
        else:
            h = search.search_sheets(check.positive_query(c["claim"]), k=1)[0]
            sheet = h["sheet"]
            if rows.get(sheet, {}).get("current") is False and h.get("superseded_by") in sheets:
                sheet = h["superseded_by"]
        v, why, cost = ask(sheet, c["claim"], sheets, src)
        out.update(sheet=sheet, verdict=v, correct=v == c["expected"], reason=why[:300], cost_usd=cost)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:160]}"
    return out


def summarise(rows):
    ok = [r for r in rows if "error" not in r]
    n, k = len(rows), sum(r.get("correct", False) for r in rows)
    pred = [r for r in ok if r["verdict"] == "conflict"]
    dec = [r for r in ok if r["verdict"] != "unclear"]
    rw = sum(r["correct"] for r in dec)
    return {"n": n, "errors": n - len(ok), "correct": k, "accuracy": round(k / n, 3), "accuracy_ci95": wilson(k, n),
            "false_alarms": sum(r["expected"] != "conflict" for r in pred), "answered": len(dec),
            "right_when_answered": f"{rw}/{len(dec)}", "cost_usd": round(sum(r.get("cost_usd", 0) for r in ok), 4)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("claims", nargs="+")
    ap.add_argument("--out", default="build/metrics_baseline_v2v3.json")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    claims = []
    for p in a.claims:
        good, why = frozen_ok(p)
        if not good:
            sys.exit(f"refusing: {p} changed since freeze ({why})")
        claims += json.loads(Path(p).read_text())
    sheets, src, rows, _ = check._load()
    res = {"date": dt.date.today().isoformat(), "claims_files": a.claims, "model": ROLES["judge"], "thinking": "LOW",
           "method": "one whole-sheet call per claim, verdict as-is; scored like eval/run_eval.py (Wilson 95%)",
           "variants": {}}
    allrows = {}
    for variant in ("baseline", "baseline_search"):
        with ThreadPoolExecutor(a.workers) as pool:
            rs = list(pool.map(lambda c: run_one(c, variant, sheets, src, rows), claims))
        allrows[variant] = rs
        res["variants"][variant] = summarise(rs)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    Path(str(out).replace(".json", ".rows.json")).write_text(json.dumps(allrows, indent=1))
    out.write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
