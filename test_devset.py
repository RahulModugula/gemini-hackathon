"""Dev claim set checks. Needs build/ (symlink to the main checkout's build dir); claims_dev.json is committed."""
import json
from collections import Counter
from pathlib import Path

import pytest

from studio import devset as D

CLAIMS = json.loads((Path(__file__).parent / "studio/devset/claims_dev.json").read_text())
pytestmark = pytest.mark.skipif(not (Path(__file__).parent / "build/sheet_index.json").exists(),
                                reason="build/ missing (run ingest or symlink it)")
FIELDS = {"id", "claim", "expected", "sheet_number", "current_sheet", "superseded_sheet", "anchor", "bbox_pt",
          "evidence_tokens", "provenance"}


def region_words(sheet, bbox, off=(0.0, 0.0)):
    b = [bbox[0] + off[0], bbox[1] + off[1], bbox[2] + off[0], bbox[3] + off[1]]
    return Counter(w["text"] for w in D.load_words(sheet)
                   if b[0] <= D.center(w)[0] <= b[2] and b[1] <= D.center(w)[1] <= b[3])


def test_schema_and_unique_ids():
    assert all(FIELDS <= set(c) for c in CLAIMS)
    assert len({c["id"] for c in CLAIMS}) == len(CLAIMS)
    assert all(c["expected"] in {"conflict", "consistent", "unclear"} for c in CLAIMS)
    assert all(c["provenance"] and c["claim"] for c in CLAIMS)


def test_p101_toilet_105a_wc2_to_wc3_found_with_both_claims():
    rows = [c for c in CLAIMS if c["sheet_number"] == "P101" and c["anchor"] == "105A"]
    stale = [c for c in rows if c["expected"] == "conflict" and "WC-2" in c["claim"]]
    cur = [c for c in rows if c["expected"] == "consistent" and "WC-3" in c["claim"]]
    assert len(stale) == 1 and len(cur) == 1
    # and the diff itself, independent of the claim file
    idx = json.loads((D.ROOT / "build/sheet_index.json").read_text())
    o, n = next(p for p in D.pairs(idx) if p[1]["sheet_number"] == "P101")
    ch = next(c for c in D.diff_pair(o, n)[0] if c["anchor"] == "105A")
    # the revision delta marker "1" also appears as a new word next to the cloud; it is not a tag
    assert ch["old_tokens"] == ["WC-2", "WC-2"] and [t for t in ch["new_tokens"] if t != "1"] == ["WC-3", "WC-3"]


def test_evidence_tokens_exist_in_stated_sheet_inside_bbox():
    for c in CLAIMS:
        if not c["evidence_tokens"]:
            continue
        # stale: the asserted (old) value lives on the superseded sheet; every other claim cites the current sheet
        sheet, off = (c["superseded_sheet"], [-v for v in c["offset_pt"]]) if c["kind"] == "stale" else (c["current_sheet"], (0, 0))
        have = region_words(sheet, c["bbox_pt"], off)
        for tok, n in Counter(c["evidence_tokens"]).items():
            assert have[tok] == n, (c["id"], tok, n, have[tok])


def test_stale_tokens_exist_only_in_superseded_region():
    stale = [c for c in CLAIMS if c["kind"] == "stale"]
    assert stale
    for c in stale:
        tok = c["evidence_tokens"][0]
        old = region_words(c["superseded_sheet"], c["bbox_pt"], [-v for v in c["offset_pt"]])[tok]
        cur = region_words(c["current_sheet"], c["bbox_pt"])[tok]
        assert old == len(c["evidence_tokens"]) and cur < old, (c["id"], tok, old, cur)


def test_absence_claims_are_false_or_true_as_labelled():
    for c in CLAIMS:
        if c["kind"] == "stale_absence":   # "shows no X": X really is on the current sheet, was not on the old
            tok = c["evidence_tokens"][0]
            assert region_words(c["current_sheet"], c["bbox_pt"])[tok] >= 1
            assert region_words(c["superseded_sheet"], c["bbox_pt"], [-v for v in c["offset_pt"]])[tok] == 0
        if c["kind"] == "current" and c.get("absent_tokens"):
            assert region_words(c["current_sheet"], c["bbox_pt"])[c["absent_tokens"][0]] == 0


def test_unclear_subjects_are_not_on_either_sheet():
    unclear = [c for c in CLAIMS if c["expected"] == "unclear"]
    assert len(unclear) >= 4
    for c in unclear:
        text = " ".join(w["text"] for s in (c["current_sheet"], c["superseded_sheet"]) for w in D.load_words(s)).lower()
        assert all(t.lower() not in text for t in c["absent_tokens"]), c["id"]


def test_labels_balanced_and_spread(capsys):
    lab = Counter(c["expected"] for c in CLAIMS)
    sheets = Counter(c["sheet_number"] for c in CLAIMS)
    with capsys.disabled():
        print(f"\nlabels: {dict(lab)}  sheets: {len(sheets)}  per-sheet max: {max(sheets.values())}")
    assert 30 <= len(CLAIMS) <= 50
    assert lab["conflict"] == lab["consistent"]
    assert 4 <= lab["unclear"] <= 6
    assert len(sheets) >= 6 and max(sheets.values()) <= 8
    assert all(len([t for t in c["evidence_tokens"]]) <= 3 for c in CLAIMS)   # no count > 3


def test_committed_file_matches_regeneration():
    claims, _ = D.build()
    assert [c["id"] for c in claims] == [c["id"] for c in CLAIMS]
    assert [c["claim"] for c in claims] == [c["claim"] for c in CLAIMS]
    assert [c["bbox_pt"] for c in claims] == [c["bbox_pt"] for c in CLAIMS]


def test_offset_estimate_recovers_known_shift():
    old = [{"text": f"TOK{i}", "bbox": [10.0 * i, 5.0 * i, 10.0 * i + 8, 5.0 * i + 6]} for i in range(20)]
    new = [{"text": w["text"], "bbox": [w["bbox"][0] + 3, w["bbox"][1] - 2, w["bbox"][2] + 3, w["bbox"][3] - 2]} for w in old]
    assert D.estimate_offset(old, new) == (3.0, -2.0)
    new[4]["text"] = "CHANGED"
    removed, added = D.align(old, new, (3.0, -2.0))
    assert [w["text"] for w in removed] == ["TOK4"] and [w["text"] for w in added] == ["CHANGED"]
