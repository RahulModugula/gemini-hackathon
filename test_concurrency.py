"""PyMuPDF is not thread-safe: one shared lock (pdflock.LOCK) wraps every document open/render/get_drawings."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pymupdf

import check
import llm
import pdflock
import rooms
from roles import ROLES
from test_check import CLAIM, fake, judge_json, world  # noqa: F401  (world is a fixture)


def _held_by_another_thread(lock):
    got = []

    def probe():
        ok = lock.acquire(blocking=False)
        got.append(ok)
        if ok:
            lock.release()
    t = threading.Thread(target=probe)
    t.start()
    t.join()
    return not got[0]


def test_render_holds_the_pdf_lock(monkeypatch, world):  # noqa: F811
    seen = []
    real_open = pymupdf.open
    monkeypatch.setattr(pymupdf, "open", lambda *a, **k: seen.append(_held_by_another_thread(pdflock.LOCK)) or real_open(*a, **k))
    check.render(dict(world["cur"]), world["src"])
    assert seen == [True]


def test_wall_mask_holds_the_pdf_lock():
    seen = []

    class Page:
        rect = pymupdf.Rect(0, 0, 10, 10)
        rotation = 0

        def get_drawings(self):
            seen.append(_held_by_another_thread(pdflock.LOCK))
            return []
    rooms.wall_mask(Page())
    assert seen == [True]


def test_sheet_rooms_holds_the_pdf_lock(monkeypatch, world):  # noqa: F811
    seen = []
    real_open = pymupdf.open
    monkeypatch.setattr(pymupdf, "open", lambda *a, **k: seen.append(_held_by_another_thread(pdflock.LOCK)) or real_open(*a, **k))
    rooms.sheet_rooms(world["cur"]["sheet"], use_cache=False)
    assert seen and all(seen)


def test_concurrent_checks_match_serial(monkeypatch, world):  # noqa: F811
    monkeypatch.setattr(llm, "call", fake({ROLES["judge"]: judge_json(world), ROLES["second"]: judge_json(world, conf=0.8)}))

    def key(card):
        return json.dumps([card["verdict"], card["confidence"],
                           [(e["crop"], e["bbox"], e["crop_rect_pt"]) for e in card["evidence"]],
                           [c.get("check") for c in card["checks"]]], sort_keys=True)
    serial = key(check.check(CLAIM))
    with ThreadPoolExecutor(8) as ex:
        keys = list(ex.map(lambda _: key(check.check(CLAIM)), range(8)))
    assert set(keys) == {serial}
