"""T13 eval harness: run claims through check() and report honest metrics with confidence intervals.

Usage (from a checkout that has check.py):
  python eval/run_eval.py eval/heldout_v1.json --out build/metrics.json [--workers 4] [--label heldout_v1]
Refuses to run if the claims file's sha256 doesn't match its .sha256 sidecar (frozen sets stay frozen).
Writes per-claim rows to <out>.rows.json and a metrics summary to <out>.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))


def wilson(k, n, z=1.96):
    if n == 0:
        return [0.0, 0.0]
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)]


def kappa(pairs):
    """Cohen's kappa for two raters over the same items (labels: any hashable)."""
    n = len(pairs)
    if n == 0:
        return None
    labels = {x for p in pairs for x in p}
    po = sum(a == b for a, b in pairs) / n
    pe = sum((sum(a == lab for a, _ in pairs) / n) * (sum(b == lab for _, b in pairs) / n) for lab in labels)
    return round((po - pe) / (1 - pe), 3) if pe < 1 else 1.0


def frozen_ok(path):
    side = Path(str(path).replace(".json", ".sha256"))
    if not side.exists():
        return True, "no sidecar (dev set)"
    want = side.read_text().split()[0]
    got = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return want == got, f"sha256 {got[:12]} vs frozen {want[:12]}"


def run_one(check, c):
    t = time.time()
    try:
        card = check.check(c["claim"])
    except Exception as e:  # counted, never hidden
        return {"id": c["id"], "expected": c["expected"], "error": f"{type(e).__name__}: {str(e)[:160]}",
                "latency_s": round(time.time() - t, 2)}
    judges = [x for x in card.get("checks", []) if x.get("by", "").startswith("gemini")]
    codes = [x for x in card.get("checks", []) if x.get("by") == "code"]
    ev = card.get("evidence") or []
    return {
        "id": c["id"], "expected": c["expected"], "verdict": card.get("verdict"),
        "correct": card.get("verdict") == c["expected"], "confidence": card.get("confidence"),
        "expected_sheet": c.get("sheet_number"), "evidence_sheets": sorted({e.get("sheet") for e in ev if e.get("sheet")}),
        "judges": {j["by"]: j.get("verdict") for j in judges},
        "code_failed": [x.get("check") for x in codes if x.get("pass") is False],
        "cost_usd": card.get("cost_usd"), "latency_s": round(time.time() - t, 2),
    }


def summarise(rows, label, claims_path):
    ok = [r for r in rows if "error" not in r]
    n, k = len(rows), sum(r.get("correct", False) for r in rows)
    conf_pred = [r for r in ok if r["verdict"] == "conflict"]
    conf_true = [r for r in rows if r["expected"] == "conflict"]
    tp = sum(r["expected"] == "conflict" for r in conf_pred)
    decided = [r for r in ok if r["verdict"] != "unclear"]
    lat = sorted(r["latency_s"] for r in ok)
    pairs = [tuple(r["judges"].values())[:2] for r in ok if len(r["judges"]) >= 2]
    costs = [r["cost_usd"] for r in ok if isinstance(r.get("cost_usd"), (int, float))]
    return {
        "label": label, "claims_file": str(claims_path), "date": dt.date.today().isoformat(), "n": n,
        "errors": n - len(ok),
        "accuracy": round(k / n, 3) if n else None, "accuracy_ci95": wilson(k, n), "correct": k,
        "conflict_precision": round(tp / len(conf_pred), 3) if conf_pred else None,
        "conflict_precision_ci95": wilson(tp, len(conf_pred)), "false_alarms": len(conf_pred) - tp,
        "conflict_recall": round(tp / len(conf_true), 3) if conf_true else None,
        "coverage": round(len(decided) / n, 3) if n else None,
        "selective_accuracy": round(sum(r["correct"] for r in decided) / len(decided), 3) if decided else None,
        "judge_agreement": round(sum(a == b for a, b in pairs) / len(pairs), 3) if pairs else None,
        "judge_kappa": kappa(pairs),
        "latency_p50_s": statistics.median(lat) if lat else None,
        "latency_p95_s": lat[min(len(lat) - 1, math.ceil(0.95 * len(lat)) - 1)] if lat else None,
        "cost_per_card_usd": round(sum(costs) / len(costs), 4) if costs else None,
        "by_label": {lab: f"{sum(r.get('correct', False) for r in rows if r['expected'] == lab)}/"
                          f"{sum(r['expected'] == lab for r in rows)}" for lab in ("conflict", "consistent", "unclear")},
        "method": "check() end to end: search -> crop -> Argon+Carbon (thinking LOW) -> code verifier; "
                  "verdict compared to frozen label; Wilson 95% intervals",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("claims")
    ap.add_argument("--out", default="build/metrics.json")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--label", default=None)
    a = ap.parse_args()
    good, why = frozen_ok(a.claims)
    if not good:
        sys.exit(f"refusing: claims file changed since freeze ({why})")
    import check  # noqa: E402  (imported late so --help works without the pipeline)
    claims = json.loads(Path(a.claims).read_text())
    with ThreadPoolExecutor(a.workers) as pool:
        rows = list(pool.map(lambda c: run_one(check, c), claims))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    Path(str(out).replace(".json", ".rows.json")).write_text(json.dumps(rows, indent=1))
    m = summarise(rows, a.label or Path(a.claims).stem, a.claims)
    out.write_text(json.dumps(m, indent=1))
    print(json.dumps(m, indent=1))


if __name__ == "__main__":
    main()
