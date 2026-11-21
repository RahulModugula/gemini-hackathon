"""Turn judge results on the studio packs into UI fixtures (CONTRACT.md card shape) for AI Studio Build.

Replays from the llm.py cache by default (LLM_CACHE=replay → no network, no cost).
Usage (repo root): LLM_CACHE=replay python studio/make_fixtures.py [--model gemini-4-hydrogen]
Writes studio/fixtures/cards.sample.json and copies crops to studio/fixtures/crops/.
"""
import argparse
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import llm  # noqa: E402


def to_pt(box, rect):
    """Gemini [ymin,xmin,ymax,xmax] 0-1000 on a crop -> PDF pt [x0,y0,x1,y1] on the sheet."""
    y0, x0, y1, x1 = box
    rx0, ry0, rx1, ry1 = rect
    w, h = rx1 - rx0, ry1 - ry0
    return [round(rx0 + x0 / 1000 * w, 1), round(ry0 + y0 / 1000 * h, 1),
            round(rx0 + x1 / 1000 * w, 1), round(ry0 + y1 / 1000 * h, 1)]


def main(model):
    packs = ROOT / "studio/packs"
    out = ROOT / "studio/fixtures"
    (out / "crops").mkdir(parents=True, exist_ok=True)
    prompt = (ROOT / "studio/prompts/judge_v0.md").read_text()
    schema = json.loads((ROOT / "studio/prompts/judge.schema.json").read_text())
    cards = []
    for ex in json.loads((packs / "manifest.json").read_text()):
        d = packs / ex["id"]
        text = prompt.replace("{{claim}}", ex["claim"]).replace("{{words}}", (d / "words.txt").read_text())
        parts = [llm.text_part("IMAGE 1 = CURRENT sheet"), llm.image_part(str(d / "crop_current.png"), "image/png")]
        if (d / "crop_superseded.png").exists():
            parts += [llm.text_part("IMAGE 2 = SUPERSEDED sheet"),
                      llm.image_part(str(d / "crop_superseded.png"), "image/png")]
        parts.append(llm.text_part(text))
        r = llm.call(model, parts, schema=schema, purpose=f"fixture:{ex['id']}")
        j = r.json
        evidence = []
        for e in j["evidence"]:
            f = ex["files"][e["image"]] if e["image"] in ex["files"] else ex["files"]["current"]
            crop_name = f"{ex['id']}_{e['image']}.png"
            shutil.copy(d / f["png"], out / "crops" / crop_name)
            evidence.append({"sheet": f["sheet"], "revision": f["sheet"].split("-")[0],
                             "bbox": to_pt(e["box_2d"], f["rect_pt"]), "box_2d_on_crop": e["box_2d"],
                             "crop": f"crops/{crop_name}", "crop_rect_pt": f["rect_pt"], "label": e["label"]})
        cards.append({
            "id": ex["id"], "claim": ex["claim"], "speaker": None,
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "verdict": j["verdict"], "confidence": j["confidence"], "reason": j["reason"],
            "evidence": evidence, "spec_refs": [],
            "checks": [{"by": model, "verdict": j["verdict"], "confidence": j["confidence"], "note": j["reason"]},
                       {"by": "code", "verdict": "info", "note": f"word diff vs superseded: {ex['files'].get('word_diff')}"}],
            "changed_by_addendum": j["changed_by_addendum"], "expected_label": ex["label"],
            "rfi_draft": None, "cost_usd": r.cost_usd, "latency_s": r.latency_s,
        })
    (out / "cards.sample.json").write_text(json.dumps(cards, indent=1))
    print(f"{len(cards)} cards -> {out / 'cards.sample.json'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemini-4-hydrogen")
    main(ap.parse_args().model)
