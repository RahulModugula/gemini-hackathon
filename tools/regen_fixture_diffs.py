"""Recompute the addendum_diff check of every fixture card in ui/cards.json from the real code path, no model calls.

    python tools/regen_fixture_diffs.py          # rewrites ui/cards.json in place, prints what changed

For each card with an addendum_diff check: the current crop is the rect of its first current evidence box
(crop_rect_pt), the superseded twin comes from check.superseded_twin (same page-offset alignment a live run uses), and
verify.addendum_diff produces only_current / only_superseded / diff_boxes. The fixture crop file names are kept.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import check  # noqa: E402
import verify  # noqa: E402

CARDS = HERE / "ui" / "cards.json"


def regen(cards):
    sheets, src, rows, narr = check._load()
    changed = []
    for c in cards:
        d = next((k for k in c.get("checks") or [] if k.get("check") == "addendum_diff"), None)
        cur_ev = next((e for e in c.get("evidence") or [] if e.get("revision") != "dwg" and e.get("crop_rect_pt")), None)
        if d is None or cur_ev is None:
            continue
        cur = check._crop(cur_ev["sheet"], list(cur_ev["crop_rect_pt"]), sheets, src, rows)
        twin = check.superseded_twin(cur, sheets, src, rows)
        if twin is None:
            continue
        new = verify.addendum_diff({"current": cur, "superseded": twin}, d.get("narrative") or ())
        for k in ("only_superseded", "only_current", "diff_boxes", "current_sheet", "superseded_sheet", "detail"):
            d[k] = new[k]
        d["superseded_rect_pt"] = [round(v, 1) for v in twin["rect"]]
        changed.append((c["id"], new["detail"]))
    return changed


if __name__ == "__main__":
    data = json.loads(CARDS.read_text())
    for cid, detail in regen(data):
        print(cid, detail)
    CARDS.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
