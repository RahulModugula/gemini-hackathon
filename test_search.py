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
    assert S.tokenize("3'-0\" AFF")[:2] == ["3ft", "0in"]
    assert "36in" in S.tokenize('GRAB BAR - 36"')
    assert {"wc", "1", "wc1"} <= set(S.tokenize("WC-1"))
    assert "105a" in S.tokenize("ROOM 105A") and "a201" in S.tokenize("see A201")
    assert "the" not in S.tokenize("the door")


def test_tokenize_fractions():
    assert S.tokenize("3'-0 1/2\"") == ["3ft", "0.5in"]
    assert S.tokenize("1/2\"") == ["0.5in"]
    assert S.tokenize("1 1/2\" AFF") == ["1.5in", "aff"]
    assert S.tokenize("4'-6\"") == ["4ft", "6in"]
    assert S.tokenize("10'") == ["10ft"]
    assert "wc1" in S.tokenize("WC-1 36\"") and "36in" in S.tokenize("WC-1 36\"")


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


def test_cells_join_nearby_words_on_different_lines():
    words = [W("105A", [100, 100, 130, 110], 0, 0, 0), W("WC-3", [120, 130, 150, 140], 1, 0, 0),
             W("TOILET", [110, 90, 150, 98], 2, 0, 0), W("FAR", [900, 900, 930, 910], 3, 0, 0)]
    cells = S.make_cells(words)
    assert any({"105A", "WC-3", "TOILET"} <= set(c["text"].split()) for c in cells)
    assert not any("FAR" in c["text"] and "105A" in c["text"] for c in cells)


def test_current_none_is_not_penalised():
    ix = S.Index.__new__(S.Index)
    ix.meta = {"a": {"current": None}, "b": {"current": True}, "c": {"current": False}}
    assert ix._penalty("a") == 1.0 and ix._penalty("b") == 1.0 and ix._penalty("c") == S.SUPERSEDED_PENALTY


@pytest.fixture(scope="module")
def idx():
    return S.Index()


def evaluate(idx):
    """Per query: rank (1-based, by sheet_number, None if absent from the top 50) of each expected sheet."""
    out = []
    for q in json.load(open(HERE / "search_eval.json"))["queries"]:
        want = list(dict.fromkeys(q["expected"] + q.get("also_acceptable", [])))
        got = [r["sheet_number"] for r in idx.search_sheets(q["query"], k=50)]
        ranks = {w: (got.index(w) + 1 if w in got else None) for w in want}
        best = min((r for r in ranks.values() if r), default=None)
        out.append({"query": q["query"], "expected": q["expected"], "ranks": ranks, "best": best, "top3": got[:3]})
    return out


@needs_build
def test_eval_metrics(idx):
    ev = evaluate(idx)
    assert len(ev) == 10
    top1 = sum(e["best"] == 1 for e in ev)
    top3 = sum(bool(e["best"]) and e["best"] <= 3 for e in ev)
    mrr = sum(1 / e["best"] for e in ev if e["best"]) / len(ev)
    for e in ev:
        print(f'{"HIT " if e["best"] and e["best"] <= 3 else "MISS"} {e["query"]!r} expected-ranks={e["ranks"]} top3={e["top3"]}')
    print(f"top-1 {top1}/10  top-3 {top3}/10  MRR {mrr:.3f}")
    assert top3 >= 9, f"top-3 hit rate {top3}/10"
    assert top1 >= 6, f"top-1 {top1}/10"


@needs_build
def test_room_tag_and_fixture_tags_p101_top1(idx):
    top = idx.search_sheets("water closet tags in toilet 105A", k=1)[0]
    assert top["sheet_number"] == "P101" and top["current"] is True and top["sheet"] == "add1-p018"


@needs_build
def test_current_ranks_above_superseded(idx):
    res = idx.search("A201 plan keynotes", k=10)
    cur = [i for i, r in enumerate(res) if r["sheet_number"] == "A201" and r["current"]]
    old = [i for i, r in enumerate(res) if r["sheet_number"] == "A201" and r["current"] is False]
    assert cur and old, res
    assert cur[0] < old[0]
    assert res[old[0]]["superseded_by"] == res[cur[0]]["sheet"]
    res = idx.search("water closet WC-1 P101", k=10)
    p = [r for r in res if r["sheet_number"] == "P101"]
    assert p[0]["current"] is True


@needs_build
@pytest.mark.parametrize("num", ["A201", "C1.10", "L1.00", "ED200", "S900"])
def test_sheet_number_in_query_is_top1(idx, num):
    assert idx.search_sheets(num, k=1)[0]["sheet_number"] == num
    assert idx.search(num, k=1)[0]["sheet_number"] == num


@needs_build
def test_sheet_index_entries_are_not_content(idx):
    # G000's sheet index lists every sheet number; a number query must not return G000 for them.
    assert idx.search_sheets("A1002", k=1)[0]["sheet_number"] == "A1002"
    assert all(d["text"].strip().lower() != "a1002" for d in idx.lw.docs if d["sheet"] == "dwg-p001")


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
def test_boxes_inside_page_all_eval_queries(idx):
    sheets = {s["sheet"]: s for s in json.load(open(HERE / "build" / "sheets.json"))["sheets"]}
    for q in json.load(open(HERE / "search_eval.json"))["queries"]:
        for r in idx.search(q["query"], k=10) + idx.search_sheets(q["query"], k=5):
            s = sheets[r["sheet"]]
            x0, y0, x1, y1 = r["bbox"]
            assert 0 <= x0 < x1 <= s["width"] + 1 and 0 <= y0 < y1 <= s["height"] + 1, (q["query"], r)
