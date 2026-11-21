"""GET /api/plan3d/<sheet>: 3D plan data from the real P101 vector drawing (add1-p018). No network."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as appmod
import check
import plan3d

SHEET = "add1-p018"
REAL = Path(check.BUILD).resolve()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A fresh build dir: the real sheet index, words and room cache, but an empty build/plan3d/."""
    for name in ("sheets.json", "words", "rooms"):
        (tmp_path / name).symlink_to(REAL / name)
    monkeypatch.setattr(appmod, "BUILD", tmp_path)
    return TestClient(appmod.app, raise_server_exceptions=False), tmp_path


def _pt_in(pt, ring):
    x, y = pt
    c = False
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            c = not c
    return c


def test_schema_and_real_geometry(env):
    c, tmp = env
    r = c.get(f"/api/plan3d/{SHEET}")
    assert r.status_code == 200
    d = r.json()
    assert d["sheet"] == SHEET and d["size_pt"] == [3024.0, 2160.0]
    assert {"walls", "rooms", "doors", "texture_url", "scale", "bounds_pt", "detail"} <= set(d)
    assert d["scale"]["text"] == '1/8" = 1\'-0"' and abs(d["scale"]["pt_per_m"] - 9 / 0.3048) < 0.01
    for w in d["walls"]:
        assert len(w["outer"]) >= 3 and all(len(p) == 2 for p in w["outer"])
        for h in w["holes"]:
            assert len(h) >= 3
    nverts = sum(len(w["outer"]) + sum(map(len, w["holes"])) for w in d["walls"])
    assert 10 <= len(d["walls"]) <= 3000 and 100 <= nverts <= 5000      # simplified, not a pixel dump
    bx0, by0, bx1, by1 = d["bounds_pt"]
    assert 0 <= bx0 < bx1 <= 3024 and 0 <= by0 < by1 <= 2160
    for w in d["walls"]:
        for x, y in w["outer"]:
            assert bx0 - 1 <= x <= bx1 + 1 and by0 - 1 <= y <= by1 + 1
    # the textures exist and are served
    t = c.get(d["texture_url"])
    assert t.status_code == 200 and t.headers["content-type"] == "image/png" and t.content[:4] == b"\x89PNG"
    assert c.get(d["detail"]["url"]).status_code == 200
    assert (tmp / "plan3d" / f"{SHEET}.json").exists()


def test_room_105a_present_with_tags(env):
    c, _ = env
    d = c.get(f"/api/plan3d/{SHEET}").json()
    rooms = {r["number"]: r for r in d["rooms"]}
    assert len(rooms) >= 15
    r = rooms["105A"]
    assert r["name"] == "TOILET" and r["tag_count"] == sum(r["tags"].values()) and r["tags"]["WC-3"] == 2
    # the evidence boxes of the 105A fixture card fall inside the room polygon
    card = next(x for x in json.loads((appmod.UI / "cards.json").read_text()) if x["id"] == "ex1_wc_105a_all_wc2")
    for e in card["evidence"]:
        if e["sheet"] == SHEET:
            b = e["bbox"]
            assert _pt_in(((b[0] + b[2]) / 2, (b[1] + b[3]) / 2), r["polygon_pt"])


def test_walls_come_from_the_wall_primitives(env):
    """Every wall vertex lies on (or within a few pt of) a wall primitive in the drawing: nothing invented."""
    import numpy as np
    import pymupdf
    import pdflock
    import rooms as R
    c, _ = env
    d = c.get(f"/api/plan3d/{SHEET}").json()
    with pdflock.LOCK:
        page = pymupdf.open(R._pdf("data/addendum-1.pdf"))[17]
        mask, _ = R.wall_mask(page)
    near = R._dilate_box(mask, plan3d.WALL_CLOSE + 2)
    pts = np.array([p for w in d["walls"] for p in w["outer"]], int)
    pts = np.clip(pts, 0, [mask.shape[1] - 1, mask.shape[0] - 1])
    assert near[pts[:, 1], pts[:, 0]].mean() > 0.97


@pytest.mark.parametrize("bad", ["nope", "add1-p999", "..%2Fsheets", "%2E%2E", "add1-p018.json", "ADD1-P018"])
def test_unknown_or_traversal_is_404(env, bad):
    c, _ = env
    assert c.get(f"/api/plan3d/{bad}").status_code == 404
    assert c.get(f"/api/plan3d/{bad}/texture.png").status_code == 404


def test_bad_texture_kind_is_404(env):
    c, _ = env
    assert c.get(f"/api/plan3d/{SHEET}/secret.png").status_code == 404


def test_cache_hit_does_not_rebuild(env, monkeypatch):
    c, _ = env
    first = c.get(f"/api/plan3d/{SHEET}").json()

    def boom(*a, **k):
        raise AssertionError("rebuilt despite a cache")
    monkeypatch.setattr(plan3d, "extract", boom)
    r = c.get(f"/api/plan3d/{SHEET}")
    assert r.status_code == 200 and r.json() == first


def test_stale_cache_version_rebuilds(env):
    c, tmp = env
    c.get(f"/api/plan3d/{SHEET}")
    p = tmp / "plan3d" / f"{SHEET}.json"
    p.write_text(json.dumps({"v": -1}))
    assert c.get(f"/api/plan3d/{SHEET}").json()["v"] == plan3d.VERSION


def test_trace_rings_and_holes():
    import numpy as np
    m = np.zeros((10, 10), bool)
    m[2:8, 2:8] = True
    m[4:6, 4:6] = False
    ps = plan3d.polygons(m, 0.5, 1)
    assert len(ps) == 1 and len(ps[0]["holes"]) == 1
    assert sorted(map(tuple, ps[0]["outer"])) == [(2, 2), (2, 8), (8, 2), (8, 8)]
    assert sorted(map(tuple, ps[0]["holes"][0])) == [(4, 4), (4, 6), (6, 4), (6, 6)]


def test_demo_page_served(env):
    c, _ = env
    r = c.get("/plan3d.html")
    assert r.status_code == 200 and "/assets/plan3d.js" in r.text


def test_concurrent_cold_requests_all_succeed(env, monkeypatch):
    """Several first requests at once (empty build/plan3d/): each gets the data, none a spurious 404. The extractions
    are held at a barrier so they overlap (a per-sheet lock lets the barrier time out and the rest hit the cache)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    c, _ = env
    gate, real = threading.Barrier(4, timeout=2), plan3d.extract

    def slow(*a, **k):
        d = real(*a, **k)
        try:
            gate.wait()
        except threading.BrokenBarrierError:
            pass
        return d
    monkeypatch.setattr(plan3d, "extract", slow)
    with ThreadPoolExecutor(4) as ex:
        codes = list(ex.map(lambda _: c.get(f"/api/plan3d/{SHEET}").status_code, range(4)))
    assert codes == [200] * 4
