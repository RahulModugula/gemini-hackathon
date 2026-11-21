import json
import time
from pathlib import Path

import pytest

import search as S

HERE = Path(__file__).parent
have_build = (HERE / "build" / "sheet_index.json").exists() and (HERE / "build" / "words").exists()
needs_build = pytest.mark.skipif(not have_build, reason="build/ missing (run ingest.py + analyze_sheets.py)")


def W(text, bbox, block, line, word):
    return {"text": text, "bbox": bbox, "block": block, "line": line, "word": word}


def test_tokenize_normalises_marks_and_codes():
    assert "3ft" in S.tokenize("3'-0\" AFF") and "0in" in S.tokenize("3'-0\" AFF")
    assert "36in" in S.tokenize('GRAB BAR - 36"')
    t = S.tokenize("WC-1")
    assert {"wc", "1", "wc1"} <= set(t)
    assert "105a" in S.tokenize("ROOM 105A") and "a201" in S.tokenize("see A201")
    assert "the" not in S.tokenize("the door")


def test_merge_lines_and_windows():
    words = [W("BAR", [30, 0, 50, 10], 0, 0, 1), W("GRAB", [0, 0, 28, 10], 0, 0, 0),   # out of order
             W("MOUNT", [0, 12, 40, 22], 0, 1, 0), W("OTHER", [0, 100, 40, 110], 1, 0, 0)]
    lines = S.merge_lines(words)
    assert [l["text"] for l in lines] == ["GRAB BAR", "MOUNT", "OTHER"]
    assert lines[0]["bbox"] == [0, 0, 50, 10]
    docs = S.make_docs(lines)
    texts = [d["text"] for d in docs]
    assert "GRAB BAR MOUNT" in texts                      # same block: window
    assert not any("MOUNT OTHER" in t for t in texts)    # different block: no window
    win = next(d for d in docs if d["text"] == "GRAB BAR MOUNT")
    assert win["bbox"] == [0, 0, 50, 22]


@pytest.fixture(scope="module")
def idx():
    return S.Index()


@needs_build
def test_hit_rate_top3(idx):
    ev = json.load(open(HERE / "search_eval.json"))["queries"]
    assert len(ev) == 10
    hits = 0
    for q in ev:
        want = set(q["expected"]) | set(q.get("also_acceptable", []))
        got = [r["sheet_number"] for r in idx.search_sheets(q["query"], k=3)]
        ok = bool(want & set(got))
        hits += ok
        print(f'{"HIT " if ok else "MISS"} {q["query"]!r} want={q["expected"]} got={got}')
    assert hits >= 9, f"top-3 hit rate {hits}/10"


@needs_build
def test_current_ranks_above_superseded(idx):
    res = idx.search("A201 plan keynotes", k=10)
    cur = [i for i, r in enumerate(res) if r["sheet_number"] == "A201" and r["current"]]
    old = [i for i, r in enumerate(res) if r["sheet_number"] == "A201" and r["current"] is False]
    assert cur and old, res
    assert cur[0] < old[0]
    assert res[old[0]]["superseded_by"] == res[cur[0]]["sheet"]
    # P101 was revised in addendum 1 too
    res = idx.search("water closet WC-1 P101", k=10)
    p = [r for r in res if r["sheet_number"] == "P101"]
    assert p[0]["current"] is True


@needs_build
def test_sheet_number_in_query_boosts(idx):
    assert idx.search("A201", k=1)[0]["sheet_number"] == "A201"


@needs_build
def test_latency_under_200ms(idx):
    qs = ["grab bar mounting height", "fire alarm devices in warehouse 151", "A201", "WC-1 toilet 105A", "cubby alternates"]
    for q in qs:
        idx.search_sheets(q)
        t = time.perf_counter()
        idx.search_sheets(q)
        idx.search(q, 10)
        ms = (time.perf_counter() - t) * 1000
        print(f"{q!r}: {ms:.1f} ms")
        assert ms < 200


@needs_build
def test_boxes_inside_page(idx):
    sheets = {s["sheet"]: s for s in json.load(open(HERE / "build" / "sheets.json"))["sheets"]}
    for r in idx.search("grab bar", k=10):
        s = sheets[r["sheet"]]
        x0, y0, x1, y1 = r["bbox"]
        assert 0 <= x0 < x1 <= s["width"] + 1 and 0 <= y0 < y1 <= s["height"] + 1
