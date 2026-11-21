"""Fixture cards (ui/cards.json): every addendum diff box lies inside its crop and sits on a PDF word whose text is in
only_current / only_superseded. Needs the built word layer (build/words); skipped when it is absent."""
import json
from pathlib import Path

import pytest

import verify

HERE = Path(__file__).parent
WORDS = HERE / "build" / "words"
CARDS = json.loads((HERE / "ui" / "cards.json").read_text())


def _words(sheet):
    p = WORDS / f"{sheet}.json"
    if not p.exists():
        pytest.skip("build/words not available")
    return json.loads(p.read_text())["words"]


def _cases():
    for c in CARDS:
        d = next((k for k in c.get("checks") or [] if k.get("check") == "addendum_diff"), None)
        cur = next((e for e in c.get("evidence") or [] if e.get("revision") != "dwg" and e.get("crop_rect_pt")), None)
        if d and cur:
            yield pytest.param(c, d, cur, id=c["id"])


@pytest.mark.parametrize("card,diff,cur", list(_cases()))
def test_fixture_diff_boxes_sit_on_changed_words(card, diff, cur):
    sides = [("current", diff["current_sheet"], cur["crop_rect_pt"], diff["only_current"])]
    if diff.get("superseded_rect_pt"):
        sides.append(("superseded", diff["superseded_sheet"], diff["superseded_rect_pt"], diff["only_superseded"]))
    else:
        pytest.fail("superseded_rect_pt missing: run tools/regen_fixture_diffs.py")
    for side, sheet, rect, only in sides:
        boxes = diff["diff_boxes"][side]
        assert len(boxes) == sum(only.values()) or len(boxes) == verify.MAX_DIFF_BOXES, side
        words = [(w["text"], verify.pt_to_box(w["bbox"], rect)) for w in _words(sheet)]
        for b in boxes:
            assert all(0 <= v <= 1000 for v in b), (side, b)
            hit = [t for t, wb in words if wb == b]
            assert hit and any(t in only for t in hit), (side, b, hit)
