"""Adversarial tests for verify.py and check.py (tests only, no product changes).

No network: llm.call is monkeypatched with canned judge JSON, as in test_check.py. The data is the real build/.
Real defects are marked xfail(strict=True) with a "BUG:" reason, so they show up as XFAIL until fixed and turn into
XPASS failures (strict) once they are fixed and the mark must be removed.
"""
import itertools
import json
import math
import random

import pytest

import check
import llm
import verify
from roles import ROLES

CLAIM = "All the water closets in Toilet 105A are type WC-2."
PREFIXED = "On P101, the water closets in TOILET 105A are tagged WC-2."
NEGATED = "There is no WC-3 in Toilet 105A."
TITLE_CLAIM = "Sheet P101 is titled FIRST FLOOR PLUMBING NEW WORK PLAN"
NONSENSE = "The moon is cheese."
VERDICTS = ("conflict", "consistent", "unclear")
CARD_KEYS = {"claim", "speaker", "timestamp", "verdict", "confidence", "evidence", "spec_refs", "checks", "reason",
             "rfi_draft"}


# ---- fixtures and helpers ----------------------------------------------------------------------------
@pytest.fixture(scope="module")
def world():
    sheets, src, rows, narr = check._load()
    cur = check.pick(CLAIM, sheets, src, rows)
    return {"sheets": sheets, "src": src, "rows": rows, "cur": cur, "crops": {"current": cur}}


def box_for(world, texts):
    """box_2d (0-1000 on the current crop) around the words in `texts` near Toilet 105A (crop-specific region)."""
    cur = world["cur"]
    bbs = [w["bbox"] for w in cur["words"] if w["text"] in texts and 1360 < w["bbox"][0] < 1430 and 860 < w["bbox"][1] < 970]
    assert bbs, texts
    union = [min(b[0] for b in bbs), min(b[1] for b in bbs), max(b[2] for b in bbs), max(b[3] for b in bbs)]
    return verify.pt_to_box(union, cur["rect"])


def box_on(crop, texts):
    """box_2d (0-1000 on `crop`) around every word of the crop whose text is in `texts`."""
    bbs = [w["bbox"] for w in crop["words"] if w["text"] in texts]
    assert bbs, texts
    union = [min(b[0] for b in bbs), min(b[1] for b in bbs), max(b[2] for b in bbs), max(b[3] for b in bbs)]
    return verify.pt_to_box(union, crop["rect"])


def judge(verdict="conflict", conf=0.9, quoted=("105A", "WC-2"), box=None, image="current", **kw):
    return {"verdict": verdict, "confidence": conf, "reason": "canned", "quoted_words": list(quoted),
            "evidence": [{"image": image, "box_2d": box, "label": "closet"}] if box is not None else [],
            "changed_by_addendum": False, "needs_code": "", **kw}


def fake(by_model):
    def call(model, parts, *, schema=None, purpose, thinking=None):
        out = by_model[model]
        return llm.Result(text=json.dumps(out), json=out)
    return call


def verify_ctx(world, a_name="A", b_name="B", claim_rows=None):
    return {"judge": a_name, "second": b_name, "thinking": "LOW", "schema": {}, "parts": [],
            "rows": claim_rows or world["rows"], "crops": world["crops"], "narrative": []}


def run_check(monkeypatch, claim, out_a, out_b=None):
    monkeypatch.setattr(llm, "call", fake({ROLES["judge"]: out_a, ROLES["second"]: out_b or out_a}))
    return check.check(claim)


def assert_card_valid(card, claim, sheets):
    """Shape and invariants every card must satisfy, whatever the judges said."""
    assert set(card) == CARD_KEYS
    assert card["claim"] == claim and card["speaker"] is None and card["rfi_draft"] is None
    assert card["verdict"] in VERDICTS
    assert isinstance(card["confidence"], float) and math.isfinite(card["confidence"]) and 0.0 <= card["confidence"] <= 1.0
    assert card["timestamp"].endswith("Z")
    assert isinstance(card["reason"], str) and isinstance(card["checks"], list) and isinstance(card["evidence"], list)
    assert all(isinstance(c, dict) and "by" in c for c in card["checks"])
    for ev in card["evidence"]:
        assert ev["sheet"] in sheets and ev["revision"] == sheets[ev["sheet"]]["source"]
        assert len(ev["bbox"]) == 4 and all(isinstance(v, (int, float)) and math.isfinite(v) for v in ev["bbox"])
        assert ev["bbox"][0] < ev["bbox"][2] and ev["bbox"][1] < ev["bbox"][3]
        assert ev["crop"].startswith("build/crops/") and isinstance(ev["label"], str)
    if card["verdict"] != "unclear":
        # a real verdict must rest on grounded evidence
        g = [c for c in card["checks"] if c.get("check") == "grounding"]
        assert g and all(c["pass"] for c in g) and card["evidence"], card["checks"]


# ---- geometry ------------------------------------------------------------------------------------------
RECT = [1068.23, 640.68, 1588.23, 1160.68]


@pytest.mark.parametrize("box,expect", [
    ([0, 0, 0, 0], [RECT[0], RECT[1], RECT[0], RECT[1]]),                  # zero-area at the origin corner
    ([1000, 1000, 1000, 1000], [RECT[2], RECT[3], RECT[2], RECT[3]]),      # zero-area at the far corner
    ([0, 0, 1000, 1000], RECT),                                             # whole crop
])
def test_box_to_pt_edges(box, expect):
    assert verify.box_to_pt(box, RECT) == pytest.approx(expect)


def test_inverted_and_out_of_range_boxes_are_rejected_by_grounding(world):
    """Linear maths extrapolates; grounding must refuse these boxes instead of trusting them."""
    cur = world["cur"]
    assert verify.box_to_pt([-5, -5, 1200, 1200], cur["rect"])[0] < cur["rect"][0]    # extrapolates, by design
    for bad in ([500, 500, 400, 600], [-5, 100, 200, 300], [100, 100, 1200, 300], [0, 0, 1000], "0,0,1,1",
                [float("nan"), 0, 10, 10], [0, 0, float("inf"), 10]):
        probs = verify.grounding({"verdict": "consistent", "quoted_words": [], "evidence": [
            {"image": "current", "box_2d": bad}]}, world["crops"])
        assert probs and "bad box" in probs[0], (bad, probs)


@pytest.mark.xfail(strict=True, reason="BUG: _valid_box accepts booleans (isinstance(True, int)), so "
                   "[True, False, 10, 10] is a valid 0-1000 box and is read as coordinates 1, 0, 10, 10")
def test_boolean_box_coordinates_are_rejected(world):
    probs = verify.grounding({"verdict": "consistent", "quoted_words": [], "evidence": [
        {"image": "current", "box_2d": [True, False, 10, 10]}]}, world["crops"])
    assert probs and "bad box" in probs[0]


def test_float_boxes_convert_and_pt_to_box_returns_ints():
    pt = verify.box_to_pt([10.5, 20.25, 30.75, 40.0], [0.0, 0.0, 100.0, 200.0])
    assert pt == pytest.approx([2.025, 2.1, 4.0, 6.15])
    assert all(isinstance(v, int) for v in verify.pt_to_box(pt, [0.0, 0.0, 100.0, 200.0]))


@pytest.mark.parametrize("rect", [
    [0.0, 0.0, 2160.0, 3024.0],          # 270-degree civil sheet after /Rotate: portrait box for a landscape page
    [100.0, 200.0, 500.0, 1000.0],       # non-square, non-zero origin
    [0.0, 0.0, 3024.0, 2160.0],
])
def test_non_square_and_rotated_rects_round_trip_exactly(rect):
    for box in ([0, 0, 1000, 1000], [100, 250, 400, 900], [333, 17, 334, 18], [1, 2, 3, 4]):
        assert verify.pt_to_box(verify.box_to_pt(box, rect), rect) == box


def test_property_integer_boxes_round_trip_exactly():
    rng = random.Random(1234)
    for _ in range(1000):
        rect = [rng.uniform(0, 3000), rng.uniform(0, 3000)]
        rect += [rect[0] + rng.uniform(1, 3000), rect[1] + rng.uniform(1, 3000)]
        y0, y1 = sorted(rng.sample(range(0, 1001), 2))
        x0, x1 = sorted(rng.sample(range(0, 1001), 2))
        assert verify.pt_to_box(verify.box_to_pt([y0, x0, y1, x1], rect), rect) == [y0, x0, y1, x1]


def test_property_pt_boxes_round_trip_within_one_grid_cell():
    """pt_to_box snaps to the model's 0-1000 grid, so a pt box comes back within half a cell (w/2000, h/2000 pt).
    The 0.01 pt target is not reachable with integer 0-1000 coordinates (1 cell = 1.5 pt on a 3024 pt sheet).
    pt_to_box has no product callers (tests only), so this is a limit of the test helper, not a product defect."""
    rng = random.Random(99)
    rect = [0.0, 0.0, 3024.0, 2160.0]
    for _ in range(1000):
        x0, x1 = sorted(rng.uniform(0, 3024) for _ in range(2))
        y0, y1 = sorted(rng.uniform(0, 2160) for _ in range(2))
        back = verify.box_to_pt(verify.pt_to_box([x0, y0, x1, y1], rect), rect)
        assert abs(back[0] - x0) <= 3024 / 2000 + 1e-9 and abs(back[2] - x1) <= 3024 / 2000 + 1e-9
        assert abs(back[1] - y0) <= 2160 / 2000 + 1e-9 and abs(back[3] - y1) <= 2160 / 2000 + 1e-9


# ---- grounding with adversarial judge output ---------------------------------------------------------
def test_grounding_is_case_and_nbsp_insensitive(world):
    b = box_for(world, {"105A", "WC-2"})
    out = judge(quoted=("105a wc-2",), box=b)
    assert verify.grounding(out, world["crops"]) == []


def test_quote_split_across_lines_still_grounds(world):
    """A judge quote with a newline inside it: each token is checked on its own, so the line break is harmless."""
    out = judge(quoted=("TOILET\n105A", "WC-2\n"), box=box_for(world, {"105A", "WC-2", "TOILET"}))
    assert verify.grounding(out, world["crops"]) == []


UNICODE_DASH_BUG = ("BUG: _norm only strips whitespace and lowercases, so a judge quoting WC-2 with a non-breaking "
                    "hyphen or en dash is not grounded against the ASCII-hyphen word in the PDF (verdict downgraded)")


@pytest.mark.parametrize("dash", [
    pytest.param("‑", id="non-breaking-hyphen", marks=pytest.mark.xfail(strict=True, reason=UNICODE_DASH_BUG)),
    pytest.param("–", id="en-dash", marks=pytest.mark.xfail(strict=True, reason=UNICODE_DASH_BUG)),
    pytest.param("‐", id="unicode-hyphen", marks=pytest.mark.xfail(strict=True, reason=UNICODE_DASH_BUG)),
])
def test_unicode_dash_quote_grounds_like_ascii_hyphen(world, dash):
    """The PDF says WC-2 with an ASCII hyphen; a judge quoting WC-2 with another dash is the same word."""
    out = judge(quoted=(f"WC{dash}2",), box=box_for(world, {"WC-2"}))
    assert verify.grounding(out, world["crops"]) == []


@pytest.mark.xfail(strict=True, reason="BUG: a quote is grounded by any box that covers its word; a box covering the "
                   "whole crop ([0,0,1000,1000]) grounds every quoted word on the sheet, so a giant box passes trivially")
def test_giant_box_covering_the_whole_crop_does_not_ground_a_quote(world):
    out = judge(quoted=("WC-2",), box=[0, 0, 1000, 1000])
    assert verify.grounding(out, world["crops"]), "giant box grounded WC-2 without pointing at it"


def test_quote_on_page_but_outside_its_box_is_caught(world):
    out = judge(quoted=("WC-2",), box=box_for(world, {"105A", "WC-3"}))      # WC-2 exists, the box misses it
    probs = verify.grounding(out, world["crops"])
    assert probs and "WC-2" in probs[0]


def test_confident_verdict_with_empty_evidence_is_flagged(world):
    probs = verify.grounding(judge("conflict", quoted=("WC-2",), box=None), world["crops"])
    assert any("needs at least one quoted word and one evidence box" in p for p in probs)


def test_unclear_verdict_with_empty_evidence_is_not_flagged(world):
    assert verify.grounding(judge("unclear", quoted=(), box=None), world["crops"]) == []


@pytest.mark.xfail(strict=True, reason="BUG: judge evidence with a list or dict as `image` raises TypeError "
                   "(unhashable) inside grounding(), which is outside run_judge's try, so one bad judge output "
                   "crashes check() instead of degrading to unclear")
def test_unhashable_image_field_does_not_crash(world):
    out = judge("conflict", quoted=("WC-2",), box=box_for(world, {"WC-2"}), image=["current"])
    probs = verify.grounding(out, world["crops"])          # must return problems, not raise
    assert probs


def test_junk_evidence_entries_never_raise(world):
    """Strings, None, nested lists, numbers: _clean turns them into placeholder dicts; grounding reports them."""
    junk = ["x", None, 5, ["current", [1, 2]], {"image": None}, {"image": "nope", "box_2d": [0, 0, 10, 10]}]
    out = verify._clean({"verdict": "conflict", "confidence": 0.9, "reason": "r", "quoted_words": ["WC-2", 7, None],
                         "evidence": junk})
    probs = verify.grounding(out, world["crops"])
    assert probs and out["quoted_words"] == ["WC-2"]


@pytest.mark.parametrize("quoted", [None, "WC-2", 12, ["WC-2", None, ["nested"]]])
def test_malformed_quoted_words_do_not_raise(world, quoted):
    out = verify._clean({"verdict": "conflict", "confidence": 0.9, "reason": "r", "quoted_words": quoted, "evidence": []})
    assert all(isinstance(q, str) for q in out["quoted_words"])
    verify.grounding(out, world["crops"])


# ---- judge output coercion ---------------------------------------------------------------------------
@pytest.mark.parametrize("verdict", ["CONFLICT", "maybe", "", None, "consistent ", 0])
def test_unknown_verdict_strings_become_unclear(verdict):
    out = verify._clean({"verdict": verdict, "confidence": 0.9, "reason": "r"})
    assert out["verdict"] == "unclear" and out["confidence"] == 0.0


@pytest.mark.parametrize("junk", [None, [1, 2], "conflict", 3])
def test_non_dict_judge_output_is_unclear(junk):
    out = verify._clean(junk)
    assert out["verdict"] == "unclear" and out["confidence"] == 0.0 and out["evidence"] == []


def test_numeric_string_confidence_is_accepted():
    assert verify._clean({"verdict": "conflict", "confidence": "0.75"})["confidence"] == 0.75


@pytest.mark.parametrize("conf", [7, -3, True])
def test_finite_out_of_range_confidence_is_clamped(conf):
    assert 0.0 <= verify._clean({"verdict": "conflict", "confidence": conf})["confidence"] <= 1.0


@pytest.mark.xfail(strict=True, reason="BUG: NaN confidence is silently coerced to 0.0 and the verdict is kept, so the "
                   "card says conflict at 0.0 instead of treating the judge output as unusable (unclear)")
def test_nan_confidence_makes_the_output_unclear():
    out = verify._clean({"verdict": "conflict", "confidence": float("nan"), "reason": "r"})
    assert out["verdict"] == "unclear"


@pytest.mark.xfail(strict=True, reason="BUG: inf confidence is clamped to 1.0 and the verdict is kept, so a judge "
                   "reporting inf gets a full-confidence verdict; non-finite confidence should be unusable")
def test_inf_confidence_makes_the_output_unclear():
    out = verify._clean({"verdict": "conflict", "confidence": float("inf"), "reason": "r"})
    assert out["verdict"] == "unclear" and out["confidence"] < 1.0


# ---- final verdict rule over all judge pairs ---------------------------------------------------------
@pytest.mark.parametrize("va,vb", list(itertools.product(VERDICTS, VERDICTS)), ids=lambda v: str(v))
def test_disagreement_matrix_final_verdict(world, monkeypatch, va, vb):
    # a conflict must quote the contradicting tag (WC-3), not only the claim's own words
    q = {"conflict": ("105A", "WC-3"), "consistent": ("105A", "WC-2"), "unclear": ("105A", "WC-2")}
    box = box_for(world, {"105A", "WC-2", "WC-3"})
    a = judge(va, 0.9, quoted=q[va], box=box)
    b = judge(vb, 0.8, quoted=q[vb], box=box)
    monkeypatch.setattr(llm, "call", fake({"A": a, "B": b}))
    res = verify.verify(CLAIM, verify_ctx(world))
    if va == vb:
        assert res["verdict"] == va and res["confidence"] == pytest.approx(0.8)
    else:
        assert res["verdict"] == "unclear" and res["confidence"] <= 0.5
        assert "judges disagree" in res["reason"]


# ---- dimension parser and dimension check ------------------------------------------------------------
@pytest.mark.parametrize("text", ["WC-36\"", "Z-1910", "Z-1910\"", "WC-2's", "105's", "L-2", "S-3", "RC-DN", "FD-1",
                                  "A201", "C1.10", "P101", "Room 105's"])
def test_model_numbers_and_possessives_are_not_dimensions(text):
    assert verify.find_dims(text) == [], text


@pytest.mark.parametrize("text,inches", [
    ("3'-0\" x 7'-0\"", [36.0, 84.0]),
    ("Owner's 3' 4\"", [40.0]),
    ("2x4 @ 16\" o.c.", [16.0]),
    ("1-1/2\"", [1.5]),
    ("Type 2 - 36\" x 84\"", [36.0, 84.0]),
])
def test_known_dimension_forms(text, inches):
    assert [v for _, v in verify.find_dims(text)] == pytest.approx(inches)


@pytest.mark.xfail(strict=True, reason="BUG: a dimension pair with no space between the marks (3'-0\"x7'-0\") loses the "
                   "second dimension: the lookbehind (?<![\\w-]) rejects a digit after 'x'")
@pytest.mark.parametrize("text,inches", [("3'-0\"x7'-0\"", [36.0, 84.0]), ("36\"x84\"", [36.0, 84.0])])
def test_unspaced_x_dimension_pairs_keep_both_values(text, inches):
    assert [v for _, v in verify.find_dims(text)] == pytest.approx(inches)


@pytest.mark.xfail(strict=True, reason="BUG: feet and inches written with unit words (3ft 6in) are two separate "
                   "dimensions [36, 6] instead of one 42 in value")
def test_feet_and_inches_with_unit_words_are_one_dimension():
    assert [v for _, v in verify.find_dims("3ft 6in")] == pytest.approx([42.0])


@pytest.mark.xfail(strict=True, reason="BUG: decimal inches after feet (3'-6.5\") parse as 3'-6 plus a stray 5\" "
                   "giving [42, 5]; the correct value is 42.5")
def test_decimal_inches_after_feet_are_not_split():
    assert [v for _, v in verify.find_dims("3'-6.5\"")] == pytest.approx([42.5])


FUZZ = [
    "3'-0\"", "3'-0 1/2\"", "3' - 0\"", "3'-6\"", "2' 6\"", "2'-0\" x 6'-8\"", "3'-0\" x 7'-0\"", "3'-0\"x7'-0\"",
    "36\"x84\"", "36\" x 84\"", "1/2\"", "1 1/2\"", "1-1/2\"", "3/4\"", "12 in", "12 inches", "36 in.", "3 feet",
    "3 ft", "3.5 ft", "3ft 6in", "3 ft 6 in", "3'-6.5\"", "3'-6 1/2\"", "Owner's 3' 4\"", "105's", "WC-2's",
    "WC-36\"", "WC-2", "Z-1910", "Z-1910\"", "A201", "C1.10", "P101", "L-2", "S-3", "RC-DN", "FD-1", "WC-1 36\"",
    "Type 2 - 36\" x 84\"", "Ø 2\"", "2x4 @ 16\" o.c.", "16\" O.C.", "AFF 42\"", "36\" AFF", "0\"", "00\"",
    "Room 105's door is 36\" wide", "Toilet 105A, 3' 4\" clear", "Level 3 is 10'-0\" above L1",
]


def test_dimension_fuzz_50_strings_no_exceptions_plausible_values():
    assert len(FUZZ) == 50 and len(set(FUZZ)) == 50
    for s in FUZZ:
        dims = verify.find_dims(s)                       # must not raise
        assert all(isinstance(t, str) and math.isfinite(v) and 0 <= v <= 1200 for t, v in dims), (s, dims)
        assert verify.parse_dim(s) is None or verify.parse_dim(s) >= 0


@pytest.mark.parametrize("text,inches", [
    ("3'-0\"", [36.0]), ("3'-0 1/2\"", [36.5]), ("3' - 0\"", [36.0]), ("3'-6\"", [42.0]), ("2' 6\"", [30.0]),
    ("2'-0\" x 6'-8\"", [24.0, 80.0]), ("3'-0\" x 7'-0\"", [36.0, 84.0]), ("36\" x 84\"", [36.0, 84.0]),
    ("1/2\"", [0.5]), ("1 1/2\"", [1.5]), ("1-1/2\"", [1.5]), ("3/4\"", [0.75]), ("12 in", [12.0]),
    ("3 feet", [36.0]), ("3 ft", [36.0]), ("3.5 ft", [42.0]), ("36 in.", [36.0]), ("3'-6 1/2\"", [42.5]),
    ("Owner's 3' 4\"", [40.0]), ("105's", []), ("WC-2's", []), ("WC-36\"", []), ("Z-1910\"", []), ("C1.10", []),
    ("Type 2 - 36\" x 84\"", [36.0, 84.0]), ("16\" O.C.", [16.0]), ("AFF 42\"", [42.0]), ("0\"", [0.0]),
    ("Level 3 is 10'-0\" above L1", [120.0]), ("Toilet 105A, 3' 4\" clear", [40.0]),
])
def test_dimension_fuzz_expected_values(text, inches):
    assert [v for _, v in verify.find_dims(text)] == pytest.approx(inches), text


def test_dimension_check_consistent_and_conflict_and_skip():
    assert verify.dimension_check('The bar is 36" AFF', ["3'-0\""])["code_verdict"] == "consistent"
    assert verify.dimension_check('The bar is 36" AFF', ["34\""])["code_verdict"] == "conflict"
    assert verify.dimension_check("No dimension here", ["34\""]) is None


@pytest.mark.xfail(strict=True, reason="BUG: a claim that states a dimension is not checked at all when the judges "
                   "quote no dimension, so a 'consistent' verdict on 36\" AFF goes through unverified")
def test_claim_dimension_with_no_quoted_dimension_is_not_consistent(world, monkeypatch):
    box = box_for(world, {"WC-2"})
    a = judge("consistent", 0.9, quoted=("WC-2",), box=box)
    monkeypatch.setattr(llm, "call", fake({"A": a, "B": a}))
    res = verify.verify('The grab bar is mounted at 36" AFF.', verify_ctx(world))
    assert res["verdict"] != "consistent"


# ---- check() end to end with mocked judges -----------------------------------------------------------
def test_check_named_sheet_claim_card(world, monkeypatch):
    out = judge(quoted=("105A", "WC-3"), box=box_for(world, {"105A", "WC-2", "WC-3"}))
    card = run_check(monkeypatch, PREFIXED, out, judge(conf=0.8, quoted=("105A", "WC-3"), box=box_for(world, {"105A", "WC-2", "WC-3"})))
    assert_card_valid(card, PREFIXED, world["sheets"])
    assert card["verdict"] == "conflict" and card["confidence"] == 0.8
    assert any(c.get("check") == "named_sheet" for c in card["checks"])
    assert card["evidence"][0]["sheet"] == "add1-p018"


def test_check_unnamed_claim_card(world, monkeypatch):
    out = judge(quoted=("105A", "WC-3"), box=box_for(world, {"105A", "WC-2", "WC-3"}))
    card = run_check(monkeypatch, CLAIM, out)
    assert_card_valid(card, CLAIM, world["sheets"])
    assert card["verdict"] == "conflict" and card["evidence"][0]["sheet"] == "add1-p018"


def test_check_negated_claim_card(world, monkeypatch):
    cands, _ = check.candidates(NEGATED, world["sheets"], world["src"], world["rows"])
    out = judge("consistent", box=box_on(cands[0], {"105A", "WC-2"}), quoted=("105A", "WC-2"))
    card = run_check(monkeypatch, NEGATED, out)
    assert_card_valid(card, NEGATED, world["sheets"])
    assert card["verdict"] == "consistent"


def test_check_title_block_claim_card(world, monkeypatch):
    sheets, src, rows = world["sheets"], world["src"], world["rows"]
    cands, _ = check.candidates(TITLE_CLAIM, sheets, src, rows)
    out = judge("consistent", quoted=("FIRST", "FLOOR", "PLUMBING"), box=box_on(cands[0], {"FIRST", "FLOOR", "PLUMBING"}))
    card = run_check(monkeypatch, TITLE_CLAIM, out)
    assert_card_valid(card, TITLE_CLAIM, sheets)
    assert card["verdict"] == "consistent" and card["evidence"]


def test_check_nonsense_claim_is_unclear(world, monkeypatch):
    card = run_check(monkeypatch, NONSENSE, judge("unclear", 0.2, quoted=(), box=None))
    assert_card_valid(card, NONSENSE, world["sheets"])
    assert card["verdict"] == "unclear" and card["evidence"] == []


def test_check_nonsense_claim_with_confident_judges_still_unclear(world, monkeypatch):
    """Both judges say consistent and quote 'cheese' with a box: 'cheese' is not on the sheet, so grounding fails."""
    cands, _ = check.candidates(NONSENSE, world["sheets"], world["src"], world["rows"])
    first = cands[0]["words"][0]["bbox"]                          # some real region of the crop, not a cheat box
    box = verify.pt_to_box(first, cands[0]["rect"])
    out = judge("consistent", 0.95, quoted=("cheese",), box=box)
    card = run_check(monkeypatch, NONSENSE, out)
    assert_card_valid(card, NONSENSE, world["sheets"])
    assert card["verdict"] == "unclear" and card["confidence"] <= 0.5
    g = next(c for c in card["checks"] if c.get("check") == "grounding")
    assert g["pass"] is False and "cheese" in g["detail"]


@pytest.mark.xfail(strict=True, reason="BUG: a claim naming a sheet number that exists in sheet_index but not in "
                   "sheets (e.g. its source is missing) crashes candidates() with ValueError: max() of an empty "
                   "sequence instead of returning no candidate")
def test_named_sheet_absent_from_sheets_does_not_crash(world):
    rows = dict(world["rows"])
    rows["ghost-p1"] = {"sheet": "ghost-p1", "source": "addX", "issue_order": 9, "sheet_number": "Q9.9",
                        "current": True, "word_count": 0}
    cands, notes = check.candidates("Check Q9.9 for the closet", world["sheets"], world["src"], rows)
    assert isinstance(cands, list)
