"""Adversarial tests for search.py and analyze_sheets.py (tests only; no product code changes).

Run from anywhere: `python -m pytest tests/test_adv_search.py`. Paths are anchored to this file, not the cwd.
Known defects are kept as strict xfails with a one-line BUG reason; when one is fixed the xfail turns into a
failure and must be unmarked.
"""
import json
import math
import random
import re
import time
from pathlib import Path

import pytest

import analyze_sheets
import search as S

HERE = Path(__file__).resolve().parent.parent
BUILD = HERE / "build"
DATA = HERE / "data"
LATENCY_MS = 200.0

have_build = all((BUILD / f).exists() for f in ("sheets.json", "sheet_index.json", "words"))
needs_build = pytest.mark.skipif(not have_build, reason="build/ missing (run ingest.py + analyze_sheets.py)")


def _sheet_index():
    return json.loads((BUILD / "sheet_index.json").read_text())


@pytest.fixture(scope="module")
def ix():
    return S.Index(BUILD)


@pytest.fixture(scope="module")
def page_size():
    return {s["sheet"]: (s["width"], s["height"]) for s in json.loads((BUILD / "sheets.json").read_text())["sheets"]}


@pytest.fixture(scope="module")
def vocab():
    words = set()
    for f in (BUILD / "words").glob("*.json"):
        for w in json.loads(f.read_text())["words"]:
            t = w["text"].strip().lower()
            if len(t) > 3 and t.isalpha() and t.isascii():
                words.add(t)
    return sorted(words)


def _superseded_numbers():
    return _sheet_index()["summary"]["superseded"] if have_build else []


def _check_hit_shape(hit):
    assert set(hit) >= {"sheet", "sheet_number", "current", "superseded_by", "bbox", "score", "snippet", "kind"}
    assert isinstance(hit["bbox"], list) and len(hit["bbox"]) == 4


# ---------- blank, punctuation-only and oversized queries ----------

@needs_build
@pytest.mark.parametrize("q", ["", " ", "   \t\n  "])
def test_blank_query_returns_nothing(ix, q):
    assert ix.search(q, k=10) == []
    assert ix.search_sheets(q, k=3) == []


@needs_build
@pytest.mark.parametrize("q", ["...", "%%%", "-", "--", "''", "()", '""', "/", "*", "?!"])
def test_punctuation_only_query_returns_nothing(ix, q):
    assert ix.search(q, k=10) == []
    assert ix.search_sheets(q, k=3) == []


@needs_build
def test_ten_thousand_char_query_is_bounded_and_fast(ix):
    q = ("grab bar mounting height " * 500)[:10000]
    assert len(q) == 10000
    t = time.perf_counter()
    res, sheets = ix.search(q, k=10), ix.search_sheets(q, k=3)
    ms = (time.perf_counter() - t) * 1000
    assert len(res) <= 10 and len(sheets) <= 3
    assert ms < LATENCY_MS, f"{ms:.1f} ms"


@needs_build
def test_ten_thousand_char_single_token_query(ix):
    t = time.perf_counter()
    assert ix.search("x" * 10000, k=10) == []
    assert ix.search_sheets("x" * 10000, k=3) == []
    assert (time.perf_counter() - t) * 1000 < LATENCY_MS


# ---------- unicode and emoji ----------

@needs_build
@pytest.mark.parametrize("q", ["\U0001F4A5", "\U0001F9F1 wall", "ñandú", "中文图纸", "Ø", "café au lait",
                               "​", " ", "Ａ２０１", "A201 grab bar"])
def test_unicode_and_emoji_queries_return_well_formed_hits(ix, q):
    res = ix.search(q, k=5)
    sheets = ix.search_sheets(q, k=3)
    assert isinstance(res, list) and isinstance(sheets, list)
    assert len(res) <= 5 and len(sheets) <= 3
    for h in res + sheets:
        _check_hit_shape(h)


@needs_build
def test_nbsp_after_sheet_number_still_finds_the_sheet(ix):
    # Non-breaking space and line separator are whitespace to str.split; "A201 " must still be A201.
    assert ix.search_sheets("A201 ", k=1)[0]["sheet_number"] == "A201"
    assert ix.search_sheets("A201 ", k=1)[0]["sheet_number"] == "A201"


# ---------- regex, SQL and other metacharacters ----------

@needs_build
@pytest.mark.parametrize("q", ["105A.*", "[(", "\\", "(?i)grab", "grab|bar", "^$", "\\d+", "{}", "%_",
                               "SELECT * FROM sheets", "x' OR '1'='1", "'; DROP TABLE sheets;--",
                               "a[b-", "*", "+", "?"])
def test_metacharacter_queries_are_treated_as_plain_text(ix, q):
    res = ix.search(q, k=10)
    sheets = ix.search_sheets(q, k=3)
    for h in res + sheets:
        _check_hit_shape(h)
    assert len(res) <= 10 and len(sheets) <= 3


@needs_build
@pytest.mark.parametrize("plain,noisy", [("105A", "105A.*"), ("WC-1", "WC-1[(\\"), ("A201", "A201.*+?")])
def test_regex_noise_does_not_change_ranking(ix, plain, noisy):
    # Metacharacters are not interpreted: the noisy query must rank exactly like the plain tag.
    assert S.tokenize(noisy) == S.tokenize(plain)
    assert ix.search(noisy, k=10) == ix.search(plain, k=10)
    assert ix.search_sheets(noisy, k=3) == ix.search_sheets(plain, k=3)


# ---------- feet-inch and inch variants (pure tokenizer) ----------

@pytest.mark.parametrize("text,expected", [
    ("3'-0\"", ["3ft", "0in"]),
    ("3' 0\"", ["3ft", "0in"]),
    ("3'-0 1/2\"", ["3ft", "0.5in"]),
    ("3′-0″", ["3ft", "0in"]),      # typographic prime and double prime
    ("36\"", ["36in"]),
    ("1/2\"", ["0.5in"]),
    ("1 1/2\"", ["1.5in"]),
    ("3'-10 1/2\"", ["3ft", "10.5in"]),
    ("3/16”", ["0.1875in"]),              # curly close quote as inch mark
])
def test_feet_inch_variants_tokenize_to_canonical_units(text, expected):
    assert S.tokenize(text) == expected


@needs_build
@pytest.mark.parametrize("q", ['36"', '1/2"', '3\'-0"', '1 1/2"', "3'-0 1/2\""])
def test_feet_inch_query_hits_share_a_token_with_the_query(ix, q):
    qt = set(S.tokenize(q))
    for h in ix.search(q, k=10):
        if h["kind"] in ("line", "window"):
            assert set(S.tokenize(h["snippet"])) & qt, (q, h)


def test_double_apostrophe_inch_mark_is_normalised_like_double_quote():
    assert S.tokenize("12''") == S.tokenize('12"')


# ---------- sheet numbers in every form ----------

@needs_build
@pytest.mark.parametrize("q,expected", [
    ("A201", "A201"), ("a201", "A201"), ("A-201", "A201"), ("a-201", "A201"),
    ("C1.10", "C1.10"), ("c110", "C1.10"), ("c1.10", "C1.10"),
    ("L1.00", "L1.00"), ("l100", "L1.00"),
    ("A1100", "A1100"), ("ED200", "ED200"), ("ad201", "AD201"), ("S900", "S900"),
])
def test_sheet_number_in_any_form_is_top_sheet(ix, q, expected):
    assert ix.search_sheets(q, k=1)[0]["sheet_number"] == expected
    assert ix.search(q, k=1)[0]["sheet_number"] == expected


# ---------- superseded sheets ----------

@needs_build
@pytest.mark.parametrize("num", _superseded_numbers())
def test_superseded_number_returns_current_sheet_first(ix, num):
    top = ix.search_sheets(num, k=1)[0]
    assert top["sheet_number"] == num and top["current"] is True
    first = ix.search(num, k=10)[0]
    assert first["sheet_number"] == num and first["current"] is True


@needs_build
@pytest.mark.parametrize("q", ["A201 first floor plan", "A201 plan keynotes", "ED200 electrical"])
def test_superseded_number_with_words_still_ranks_current_first(ix, q):
    sheets = ix.search_sheets(q, k=10)
    num = q.split()[0]
    same = [i for i, h in enumerate(sheets) if h["sheet_number"] == num]
    assert same, sheets
    assert sheets[same[0]]["current"] is True


# ---------- bboxes ----------

@needs_build
@pytest.mark.parametrize("q", ["grab bar", "fire alarm", "A201", "3'-0\"", "cubby", "WC-1 105A", "1/2\""])
def test_result_bboxes_are_finite_and_inside_page(ix, page_size, q):
    for h in ix.search(q, k=20) + ix.search_sheets(q, k=10):
        w, ht = page_size[h["sheet"]]
        x0, y0, x1, y1 = h["bbox"]
        assert all(math.isfinite(v) for v in h["bbox"]), h
        assert 0 <= x0 < x1 <= w + 1e-6 and 0 <= y0 < y1 <= ht + 1e-6, (q, h, w, ht)


# ---------- determinism ----------

@needs_build
@pytest.mark.parametrize("q", ["grab bar mounting height", "A201", "water closet WC-1 105A", "1/2\""])
def test_same_query_twice_gives_identical_results(ix, q):
    assert ix.search(q, k=10) == ix.search(q, k=10)
    assert ix.search_sheets(q, k=3) == ix.search_sheets(q, k=3)


@needs_build
def test_fresh_index_gives_identical_results(ix):
    fresh = S.Index(BUILD)
    for q in ("grab bar", "A201", "3'-0\""):
        assert fresh.search(q, k=10) == ix.search(q, k=10)
        assert fresh.search_sheets(q, k=3) == ix.search_sheets(q, k=3)


# ---------- top-k bounds ----------

@needs_build
def test_k_one_returns_exactly_the_best_hit(ix):
    one = ix.search("grab bar", k=1)
    assert len(one) == 1 and one[0] == ix.search("grab bar", k=10)[0]
    sheet_one = ix.search_sheets("grab bar", k=1)
    assert len(sheet_one) == 1 and sheet_one[0] == ix.search_sheets("grab bar", k=3)[0]


@needs_build
def test_k_larger_than_result_count_is_bounded_and_sheets_unique(ix):
    res = ix.search("A201", k=10000)
    assert 0 < len(res) < 10000
    sheets = ix.search_sheets("A201", k=10000)
    nums = [h["sheet_number"] for h in sheets]
    assert len(nums) == len(set(nums))


@needs_build
def test_search_hits_do_not_repeat_the_same_box(ix):
    keys = [(h["sheet"], tuple(h["bbox"])) for h in ix.search("A201", k=10000)]
    assert len(keys) == len(set(keys))


@needs_build
@pytest.mark.parametrize("q", ["grab bar", "A201", "fire alarm devices", "cubby"])
def test_smaller_k_is_a_prefix_of_larger_k(ix, q):
    assert ix.search(q, k=3) == ix.search(q, k=10)[:3]
    assert ix.search_sheets(q, k=1) == ix.search_sheets(q, k=3)[:1]


@needs_build
@pytest.mark.parametrize("q", ["grab bar", "fire alarm", "A201", "water closet"])
def test_search_sheets_never_repeats_a_sheet_number(ix, q):
    nums = [h["sheet_number"] for h in ix.search_sheets(q, k=50)]
    assert len(nums) == len(set(nums))


@needs_build
def test_k_zero_returns_no_hits(ix):
    assert ix.search("grab bar", k=0) == []
    assert ix.search_sheets("grab bar", k=0) == []


@needs_build
def test_negative_k_returns_no_hits(ix):
    assert ix.search("grab bar", k=-1) == []
    assert ix.search_sheets("grab bar", k=-1) == []


# ---------- latency over real vocabulary ----------

@needs_build
def test_fifty_random_vocabulary_queries_under_200ms(ix, vocab):
    rng = random.Random(20261121)
    worst = 0.0
    for _ in range(50):
        q = " ".join(rng.sample(vocab, rng.randint(1, 4)))
        t = time.perf_counter()
        ix.search(q, 10)
        ix.search_sheets(q, 3)
        worst = max(worst, (time.perf_counter() - t) * 1000)
    print(f"worst of 50 random queries: {worst:.1f} ms")
    assert worst < LATENCY_MS, f"worst {worst:.1f} ms"


# ---------- analyze_sheets invariants over all rows ----------

@pytest.fixture(scope="module")
def analysis(tmp_path_factory):
    """Run analyze_sheets into a scratch dir so the test never rewrites build/ outputs."""
    t = tmp_path_factory.mktemp("adv_analyze")
    (t / "sheets.json").symlink_to((BUILD / "sheets.json").resolve())
    (t / "words").symlink_to((BUILD / "words").resolve())
    analyze_sheets.main(str(t), str(DATA))
    idx = json.loads((t / "sheet_index.json").read_text())
    return idx


@needs_build
def test_all_rows_match_sheets_json_order(analysis):
    sheets = json.loads((BUILD / "sheets.json").read_text())["sheets"]
    assert [r["sheet"] for r in analysis["rows"]] == [s["sheet"] for s in sheets]
    assert len(analysis["rows"]) == 76


@needs_build
def test_every_superseded_by_exists_and_is_current(analysis):
    rows = {r["sheet"]: r for r in analysis["rows"]}
    sup = [r for r in analysis["rows"] if r["superseded_by"] is not None]
    assert sup, "expected superseded rows"
    for r in sup:
        target = rows.get(r["superseded_by"])
        assert target is not None, r["sheet"]
        assert target["current"] is True, (r["sheet"], target["sheet"])
        assert target["sheet_number"] == r["sheet_number"], r["sheet"]
        assert target["issue_order"] > r["issue_order"], r["sheet"]


@needs_build
def test_no_sheet_supersedes_itself(analysis):
    assert all(r["superseded_by"] != r["sheet"] for r in analysis["rows"])


@needs_build
def test_issue_order_is_monotonic_and_one_per_source(analysis):
    orders = [r["issue_order"] for r in analysis["rows"]]
    assert orders == sorted(orders)
    per_source = {}
    for r in analysis["rows"]:
        per_source.setdefault(r["source"], set()).add(r["issue_order"])
    assert all(len(v) == 1 for v in per_source.values()), per_source


@needs_build
def test_titles_null_only_where_listed(analysis):
    null = {r["sheet"] for r in analysis["rows"] if r["title"] is None}
    assert null == set(analysis["summary"]["unmatched_titles"]) == {"dwg-p001"}
    for r in analysis["rows"]:
        if r["title"] is not None:
            assert r["title"].strip() == r["title"] and r["title"], r["sheet"]


@needs_build
def test_current_flag_agrees_with_superseded_by(analysis):
    for r in analysis["rows"]:
        if r["current"] is None:
            continue
        assert (r["current"] is False) == (r["superseded_by"] is not None), r["sheet"]


@needs_build
def test_summary_superseded_list_matches_rows(analysis):
    from_rows = sorted({r["sheet_number"] for r in analysis["rows"] if r["superseded_by"]})
    assert analysis["summary"]["superseded"] == from_rows


@needs_build
def test_every_row_has_a_valid_sheet_number_and_revision_no(analysis):
    pat = re.compile(r"^[A-Z]{1,3}-?\d{1,4}(\.\d{1,2})?[A-Z]?$")
    for r in analysis["rows"]:
        assert pat.match(r["sheet_number"]), r
        revs = r["revisions"]
        assert r["revision_no"] == (max(x["no"] for x in revs) if revs else 0), r["sheet"]
