"""Sheet-stage endpoints: /api/sheet/<id> (page size + room outlines) and /sheets/<id>.jpg (full-sheet render)."""
import json

import pymupdf
import pytest
from fastapi.testclient import TestClient

import app as appmod


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "BUILD", tmp_path)
    pdf = tmp_path / "set.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((50, 50), "TOILET 105A")
    doc.save(pdf)
    (tmp_path / "sheets.json").write_text(json.dumps({
        "sources": [{"source": "add1", "path": str(pdf)}],
        "sheets": [{"sheet": "add1-p001", "source": "add1", "page": 1, "width": 400.0, "height": 300.0}]}))
    return TestClient(appmod.app, raise_server_exceptions=False), tmp_path


def test_room_edges_trace_the_mask_exactly():
    # an L shape: rows 10..11 cover x 20..30, row 12 covers x 20..25
    edges = appmod._room_edges([[10, 20, 30], [11, 20, 30], [12, 20, 25]])
    assert sorted(edges) == sorted([
        [20, 10, 30, 10],          # top
        [25, 12, 30, 12],          # step
        [20, 13, 25, 13],          # bottom
        [20, 10, 20, 13],          # left
        [25, 12, 25, 13],          # inner vertical of the step
        [30, 10, 30, 12],          # right
    ])
    assert appmod._room_edges([]) == []


def test_sheet_api_returns_size_and_rooms(env):
    c, tmp = env
    (tmp / "rooms").mkdir()
    (tmp / "rooms" / "add1-p001.json").write_text(json.dumps({"v": 1, "sheet": "add1-p001", "unresolved": [], "rooms": [
        {"number": "105A", "name": "TOILET", "label_bbox": [1, 1, 2, 2], "bbox": [20, 10, 30, 12],
         "area": 20, "gap": 8, "tags": {"WC-3": 2}, "runs": [[10, 20, 30], [11, 20, 30]]}]}))
    r = c.get("/api/sheet/add1-p001")
    assert r.status_code == 200
    j = r.json()
    assert (j["width"], j["height"], j["image"]) == (400.0, 300.0, "/sheets/add1-p001.jpg")
    room = j["rooms"][0]
    assert room["number"] == "105A" and room["tags"] == {"WC-3": 2} and "runs" not in room
    assert [20, 10, 30, 10] in room["edges"] and [20, 12, 30, 12] in room["edges"]


def test_sheet_api_without_rooms_is_empty_list(env):
    c, _ = env
    assert c.get("/api/sheet/add1-p001").json()["rooms"] == []


@pytest.mark.parametrize("sheet", ["add1-p999", "../etc", "add1-p001.json", "x", "dwg-p01"])
def test_unknown_or_malformed_sheet_is_404(env, sheet):
    c, _ = env
    assert c.get(f"/api/sheet/{sheet}").status_code == 404
    assert c.get(f"/sheets/{sheet}.jpg").status_code == 404


def test_sheet_render_is_a_cached_jpeg(env):
    c, tmp = env
    r = c.get("/sheets/add1-p001.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.content[:3] == b"\xff\xd8\xff"
    out = tmp / "sheet_renders" / f"add1-p001_{appmod.SHEET_PX}.jpg"
    assert out.is_file()
    pix = pymupdf.Pixmap(str(out))
    assert max(pix.width, pix.height) == appmod.SHEET_PX
    assert c.get("/sheets/add1-p001.png").status_code == 404
    t = c.get("/sheets/add1-p001_t.jpg")
    assert t.status_code == 200 and t.content[:3] == b"\xff\xd8\xff"
    assert max(pymupdf.Pixmap(t.content).width, pymupdf.Pixmap(t.content).height) == appmod.THUMB_PX


def test_card_by_id_reads_fixtures_and_rejects_unknown(env):
    c, _ = env
    fx = json.loads((appmod.UI / "cards.json").read_text())[0]
    assert c.get(f"/api/card/{fx['id']}").json()["id"] == fx["id"]
    assert c.get("/api/card/nope").status_code == 404
    assert c.get("/api/card/..%2Fx").status_code == 404


def test_heldout_totals_sum_the_frozen_runs(env):
    c, tmp = env
    assert c.get("/api/heldout").json() is None
    (tmp / "metrics_heldout_v1_final.json").write_text(json.dumps({"label": "a", "n": 17, "false_alarms": 0, "coverage": 0.471, "selective_accuracy": 1.0}))
    (tmp / "metrics_heldout_v2.json").write_text(json.dumps({"label": "b", "n": 43, "false_alarms": 0, "coverage": 0.326, "selective_accuracy": 1.0}))
    j = c.get("/api/heldout").json()
    assert (j["n"], j["false_alarms"], j["answered"], j["answered_right"]) == (60, 0, 22, 22)


def test_sheetset_lists_rows_and_supersession(env):
    c, tmp = env
    assert c.get("/api/sheetset").status_code == 404
    (tmp / "sheet_index.json").write_text(json.dumps({"summary": {"by_source": {"dwg": 1, "add1": 1}, "superseded": ["P101"]},
        "rows": [{"sheet": "dwg-p034", "source": "dwg", "sheet_number": "P101", "current": False, "superseded_by": "add1-p018",
                  "word_count": 9}, {"sheet": "add1-p018", "source": "add1", "sheet_number": "P101", "current": True}]}))
    j = c.get("/api/sheetset").json()
    assert j["by_source"] == {"dwg": 1, "add1": 1} and j["superseded"] == ["P101"]
    assert j["rows"][0]["superseded_by"] == "add1-p018" and "word_count" not in j["rows"][0]
