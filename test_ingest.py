import json
from pathlib import Path

import pytest

from ingest import DEFAULT_PDF, ingest, sheet_id

pytestmark = pytest.mark.skipif(not Path(DEFAULT_PDF).exists(), reason="drawings not downloaded")


def test_ingest_three_sheets(tmp_path):
    sheets = ingest({"dwg": (DEFAULT_PDF, 0)}, tmp_path, pages={1, 2, 3}, dpi=72)
    assert [s["sheet"] for s in sheets] == ["dwg-p001", "dwg-p002", "dwg-p003"]

    index = json.loads((tmp_path / "sheets.json").read_text())
    assert len(index["sheets"]) == 3
    assert index["sources"][0]["sheets"] == 3
    for s in index["sheets"]:
        assert set(s) >= {"sheet", "source", "page", "text_length", "has_text_layer",
                          "words_file", "tiles"}
        assert s["has_text_layer"] == (s["text_length"] > 0)
        assert s["tiles"], "every sheet must have at least one tile"
        for t in s["tiles"]:
            assert (tmp_path / t["file"]).stat().st_size > 0
        # tiles cover the whole page
        assert max(t["bbox_pt"][2] for t in s["tiles"]) == pytest.approx(s["width"], abs=0.01)
        assert max(t["bbox_pt"][3] for t in s["tiles"]) == pytest.approx(s["height"], abs=0.01)

        wf = json.loads((tmp_path / s["words_file"]).read_text())
        assert wf["sheet"] == s["sheet"]
        assert len(wf["words"]) == s["word_count"]
        for w in wf["words"]:
            x0, y0, x1, y1 = w["bbox"]
            assert 0 <= x0 <= x1 <= wf["width"] + 1 and 0 <= y0 <= y1 <= wf["height"] + 1
    # at least one sheet must carry vector text, else the words path is untested
    assert any(s["has_text_layer"] for s in index["sheets"])


def test_rotated_sheet_words_are_in_rendered_space(tmp_path):
    """Page 4 (civil C1.10) has /Rotate 270. Word boxes must be in the same space as the tiles."""
    (s,) = ingest({"dwg": (DEFAULT_PDF, 0)}, tmp_path, pages={4}, dpi=36)
    assert s["rotation"] == 270
    # tiles must cover the rotated (rendered) page, not the unrotated mediabox
    assert max(t["bbox_pt"][2] for t in s["tiles"]) == pytest.approx(s["width"], abs=0.01)
    assert max(t["bbox_pt"][3] for t in s["tiles"]) == pytest.approx(s["height"], abs=0.01)
    wf = json.loads((tmp_path / s["words_file"]).read_text())
    for w in wf["words"]:
        x0, y0, x1, y1 = w["bbox"]
        assert 0 <= x0 <= x1 <= wf["width"] + 1 and 0 <= y0 <= y1 <= wf["height"] + 1, w
    # the title-block sheet number reads as horizontal text in the bottom-right after rotation
    from analyze_sheets import find_sheet_number
    assert find_sheet_number(wf["words"], wf["width"], wf["height"]) == "C1.10"


@pytest.mark.parametrize("dpi", [143, 72])  # 143 dpi used to leave a 0.5 pt strip uncovered
def test_tiles_reach_page_edge_at_awkward_dpi(tmp_path, dpi):
    (s,) = ingest({"dwg": (DEFAULT_PDF, 0)}, tmp_path, pages={1}, dpi=dpi)
    assert max(t["bbox_pt"][2] for t in s["tiles"]) == pytest.approx(s["width"], abs=1e-6)
    assert max(t["bbox_pt"][3] for t in s["tiles"]) == pytest.approx(s["height"], abs=1e-6)


def test_missing_source_is_reported_not_dropped(tmp_path):
    ingest({"nope": (str(tmp_path / "absent.pdf"), 0)}, tmp_path)
    index = json.loads((tmp_path / "sheets.json").read_text())
    assert index["sources"] == [{"source": "nope", "path": str(tmp_path / "absent.pdf"),
                                 "error": "missing"}]


def test_sheet_id_is_unique_per_source():
    assert sheet_id("dwg", 0) != sheet_id("add1", 0)
