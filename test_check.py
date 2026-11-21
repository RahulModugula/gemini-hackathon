"""T7/T8 tests: no network. llm.call is monkeypatched with canned judge JSON; the data is the real build/."""
import json
import time
from pathlib import Path

import pytest

import check
import llm
import search
import verify
from roles import ROLES

CLAIM = "All the water closets in Toilet 105A are type WC-2."
HERE = Path(__file__).parent


@pytest.fixture(scope="module")
def world():
    sheets, src, rows, narr = check._load()
    cur = check.pick(CLAIM, sheets, src, rows)
    return {"sheets": sheets, "src": src, "rows": rows, "cur": cur}


def box_for(world, texts):
    """box_2d (0-1000 on the current crop) tightly around every word whose text is in `texts`."""
    cur = world["cur"]
    bbs = [w["bbox"] for w in cur["words"] if w["text"] in texts and 1360 < w["bbox"][0] < 1430 and 860 < w["bbox"][1] < 970]
    assert bbs
    union = [min(b[0] for b in bbs), min(b[1] for b in bbs), max(b[2] for b in bbs), max(b[3] for b in bbs)]
    return verify.pt_to_box(union, cur["rect"])


def judge_json(world, verdict="conflict", conf=0.9, quoted=("105A", "WC-2", "WC-3"), box=None, **kw):
    return {"verdict": verdict, "confidence": conf, "reason": "Two of the three closets are WC-3 on the current sheet.",
            "quoted_words": list(quoted),
            "evidence": [{"image": "current", "box_2d": box or box_for(world, {"105A", "WC-2", "WC-3"}),
                          "label": "water closets at Toilet 105A"}],
            "changed_by_addendum": True, "needs_code": "", **kw}


def fake(by_model, delay=0.0):
    def call(model, parts, *, schema=None, purpose, thinking=None):
        time.sleep(delay)
        out = by_model[model]
        return llm.Result(text=json.dumps(out), json=out)
    return call


def run(monkeypatch, a, b=None, claim=CLAIM, delay=0.0):
    monkeypatch.setattr(llm, "call", fake({ROLES["judge"]: a, ROLES["second"]: b or a}, delay))
    return check.check(claim)


def test_card_shape_and_end_to_end(monkeypatch, world):
    card = run(monkeypatch, judge_json(world), judge_json(world, conf=0.8))
    assert set(card) == {"claim", "speaker", "timestamp", "verdict", "confidence", "evidence", "spec_refs", "checks",
                         "reason", "rfi_draft"}
    assert card["claim"] == CLAIM and card["speaker"] is None and card["rfi_draft"] is None and card["spec_refs"] == []
    assert card["timestamp"].endswith("Z")
    assert card["verdict"] == "conflict" and card["confidence"] == 0.8   # min of the agreeing judges
    assert card["evidence"], "expected evidence"
    ev = card["evidence"][0]
    assert ev["sheet"] == "add1-p018" and ev["revision"] == "add1" and ev["label"]
    assert len(ev["bbox"]) == 4 and ev["bbox"][0] < ev["bbox"][2] and ev["bbox"][1] < ev["bbox"][3]
    assert 1360 < ev["bbox"][0] < 1375 and 880 < ev["bbox"][1] < 890          # pt on the sheet, not 0-1000
    assert ev["crop"].startswith("build/crops/") and (HERE / ev["crop"]).stat().st_size > 1000
    by = {c["by"]: c for c in card["checks"] if "verdict" in c and c["by"] != "code"}
    assert set(by) == {"gemini-4-argon", "gemini-4-carbon"} and all("latency_s" in c for c in by.values())
    code = {c["check"]: c for c in card["checks"] if c["by"] == "code"}
    assert code["grounding"]["pass"] and code["currency"]["pass"]
    diff = code["addendum_diff"]
    assert diff["only_current"].get("WC-3", 0) >= 1 and diff["only_superseded"].get("WC-2", 0) >= 1
    assert diff["narrative"], "addendum narrative changes should be attached"


def test_judges_disagree_is_unclear(monkeypatch, world):
    card = run(monkeypatch, judge_json(world, "conflict", 0.9), judge_json(world, "consistent", 0.9))
    assert card["verdict"] == "unclear" and card["confidence"] <= 0.5
    assert {c["verdict"] for c in card["checks"] if c["by"] != "code"} == {"conflict", "consistent"}


def test_judges_run_concurrently(monkeypatch, world):
    """Measure overlap, not wall time: with the judge pair (x2 candidates) the calls must be in flight together."""
    import threading
    lock, st = threading.Lock(), {"now": 0, "max": 0}
    out = judge_json(world)

    def call(model, parts, *, schema=None, purpose, thinking=None):
        with lock:
            st["now"] += 1
            st["max"] = max(st["max"], st["now"])
        time.sleep(0.2)
        with lock:
            st["now"] -= 1
        return llm.Result(text=json.dumps(out), json=out)

    monkeypatch.setattr(llm, "call", call)
    check.check(CLAIM)
    assert st["max"] >= 2


def empty_box(world):
    cur, r = world["cur"], world["cur"]["rect"]
    for x in range(0, 900, 50):
        for y in range(0, 900, 50):
            b = [y, x, y + 40, x + 40]
            pt = verify.box_to_pt(b, r)
            if not any(verify._share_inside(w["bbox"], pt) > 0 for w in cur["words"]):
                return b
    raise AssertionError("no empty region")


@pytest.mark.parametrize("variant", ["word_not_on_sheet", "box_on_blank_area", "quote_outside_box"])
def test_hallucinated_quote_is_caught(monkeypatch, world, variant):
    if variant == "word_not_on_sheet":
        bad = judge_json(world, quoted=("105A", "WC-9"))
    elif variant == "box_on_blank_area":
        bad = judge_json(world, box=empty_box(world))
    else:   # WC-2 exists in the crop but the box only covers the 105A/WC-3 area above it
        bad = judge_json(world, quoted=("WC-2",), box=box_for(world, {"105A", "WC-3"}))
    card = run(monkeypatch, bad)
    g = next(c for c in card["checks"] if c.get("check") == "grounding")
    assert g["pass"] is False and g["by"] == "code" and g["detail"]
    assert card["verdict"] == "unclear" and card["confidence"] <= 0.5


def ctx_for(world, rows, a, b):
    return {"judge": "A", "second": "B", "thinking": "LOW", "schema": {}, "parts": [], "rows": rows,
            "crops": {"current": world["cur"]}, "narrative": [], "_": (a, b)}


def test_superseded_evidence_fails_currency(monkeypatch, world):
    rows = {k: dict(v) for k, v in world["rows"].items()}
    rows["add1-p018"].update(current=False, superseded_by="add2-p999")
    out = judge_json(world)
    monkeypatch.setattr(llm, "call", fake({"A": out, "B": out}))
    res = verify.verify(CLAIM, ctx_for(world, rows, out, out))
    c = next(c for c in res["checks"] if c.get("check") == "currency")
    assert c["pass"] is False and "add2-p999" in c["detail"]
    assert res["verdict"] == "unclear" and res["confidence"] <= 0.5
    assert verify.currency(["dwg-p034"], world["rows"]) and "add1-p018" in verify.currency(["dwg-p034"], world["rows"])[0]
    assert verify.currency(["add1-p018"], world["rows"]) == []


@pytest.mark.parametrize("s,inches", [("3'-0\"", 36), ("3'-0 1/2\"", 36.5), ("36\"", 36), ("1/2\"", 0.5), ("3'", 36),
                                      ("3'-6\"", 42), ("1 1/2\"", 1.5), ("2' 6\"", 30), ("3'-1/2\"", 36.5)])
def test_parse_dim(s, inches):
    assert verify.parse_dim(s) == pytest.approx(inches)


def test_parse_dim_rejects_non_dimensions():
    assert verify.parse_dim("WC-2") is None and verify.parse_dim("3'-0\" 4'-0\"") is None and verify.parse_dim("") is None


def test_dimension_math_mismatch_fails_and_match_passes(monkeypatch, world):
    claim = "The grab bar is mounted at 36\" AFF."
    d = verify.dimension_check(claim, ["34\""])
    assert d["code_verdict"] == "conflict" and "36" in d["detail"]
    assert verify.dimension_check(claim, ["3'-0\""])["code_verdict"] == "consistent"
    assert verify.dimension_check(claim, ["WC-2"]) is None and verify.dimension_check("no dims", ["34\""]) is None
    # end to end through verify() with real words: the box holds a pipe size word, the claim states another size
    w = next(w for w in world["cur"]["words"] if w["text"].endswith('"') and verify.parse_dim(w["text"]) is not None)
    box = verify.pt_to_box(w["bbox"], world["cur"]["rect"])
    box = [max(0, box[0] - 3), max(0, box[1] - 3), min(1000, box[2] + 3), min(1000, box[3] + 3)]
    size = verify.parse_dim(w["text"])
    for claim_dim, want_pass in ((f'{size + 5:g}"', False), (f"0'-{size:g}\"", True)):   # 0'-3" == 3": not a copy of the word
        out = judge_json(world, "consistent", quoted=(w["text"],), box=box)
        monkeypatch.setattr(llm, "call", fake({"A": out, "B": out}))
        res = verify.verify(f"The pipe is {claim_dim}", ctx_for(world, world["rows"], out, out))
        dim = next(c for c in res["checks"] if c.get("check") == "dimension")
        assert dim["pass"] is want_pass and (res["verdict"] == "consistent") is want_pass, (dim, res["verdict"])


def test_box_pt_round_trip():
    rect = [1068.23, 640.68, 1588.23, 1160.68]
    for box in ([0, 0, 1000, 1000], [100, 250, 400, 900], [333, 17, 334, 18]):
        pt = verify.box_to_pt(box, rect)
        assert verify.pt_to_box(pt, rect) == box
    pt = verify.box_to_pt([500, 500, 1000, 1000], rect)
    assert pt == pytest.approx([1328.23, 900.68, 1588.23, 1160.68])


# ---- candidate selection, negation, concurrency ------------------------------------------------------
def test_negation_words_stripped_from_search_query_only(monkeypatch, world):
    assert check.positive_query("There is no grab bar in Toilet 105A") == "There is grab bar in Toilet 105A"
    assert check.positive_query("Toilet 105A does not have WC-2") == "Toilet 105A does have WC-2"
    assert check.positive_query("not") == "not"          # never an empty query
    seen = []
    real = search.search_sheets
    monkeypatch.setattr(search, "search_sheets", lambda q, k=3: seen.append(q) or real(q, k))
    claim = "There is no WC-3 in Toilet 105A"
    sheets, src, rows, _ = check._load()
    check.candidates(claim, sheets, src, rows)
    assert seen == ["There is WC-3 in Toilet 105A"]
    parts = check.build_parts(claim, {"current": check.render(world["cur"], world["src"])})
    assert any(claim in (getattr(p, "text", None) or "") for p in parts)   # the judge still sees the negation


def test_named_sheet_restricts_to_current_version(world):
    sheets, src, rows = world["sheets"], world["src"], world["rows"]
    assert check.named_sheets("Check C1.10 and p101.", rows).keys() == {"c110", "p101"}
    cands, notes = check.candidates("Sheet P101 shows WC-2 for all closets in 105A", sheets, src, rows)
    assert [c["sheet"] for c in cands] == ["add1-p018"]          # never the superseded dwg-p034
    assert "dwg-p034" in notes[0] and "add1-p018" in notes[0]
    cands, _ = check.candidates("Sheet C1.10 has a note about erosion control", sheets, src, rows)
    assert [rows[c["sheet"]]["sheet_number"] for c in cands] == ["C1.10"] and rows[cands[0]["sheet"]]["current"] is not False


def test_unnamed_claim_judges_two_candidates_and_keeps_grounded_one(monkeypatch, world):
    sheets, src, rows = world["sheets"], world["src"], world["rows"]
    cands, notes = check.candidates(CLAIM, sheets, src, rows)
    assert len(cands) == 2 and notes == [] and cands[0]["sheet"] == "add1-p018"
    card = run(monkeypatch, judge_json(world))
    assert card["evidence"][0]["sheet"] == "add1-p018"
    alt = [c for c in card["checks"] if c.get("check") == "alternative_candidate"]
    assert len(alt) == 1 and alt[0]["sheet"] != "add1-p018" and alt[0]["chosen_sheet"] == "add1-p018"
    assert alt[0]["grounding_pass"] is False     # canned boxes only fit P101
    # the rule is "grounded beats ungrounded", whatever the confidence or the candidate order
    r_good = {"verdict": "conflict", "confidence": 0.6, "checks": [{"check": "grounding", "pass": True}]}
    r_bad = {"verdict": "conflict", "confidence": 0.95, "checks": [{"check": "grounding", "pass": False}]}
    assert check._rank(r_good) > check._rank(r_bad)


def test_concurrency_bounded_and_thread_safe(monkeypatch, world):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    lock, state = threading.Lock(), {"now": 0, "max": 0}
    out = judge_json(world)

    def call(model, parts, *, schema=None, purpose, thinking=None):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.15)
        with lock:
            state["now"] -= 1
        return llm.Result(text=json.dumps(out), json=out)

    monkeypatch.setattr(llm, "call", call)
    check.check(CLAIM)
    assert 3 <= state["max"] <= 4                # two candidates x two judges, never more than 4 in flight
    with ThreadPoolExecutor(8) as ex:            # 8 concurrent whole checks (mocked calls) all return full cards
        cards = list(ex.map(lambda _: check.check(CLAIM), range(8)))
    assert all(c["verdict"] == "conflict" and c["evidence"] for c in cards)


# ---- review round: judge failures, current-only grounding, geometry, dimensions, twin offset ------------
def test_one_judge_raising_an_sdk_error_gives_unclear(monkeypatch, world):
    from google.genai import errors
    ok = judge_json(world)

    def call(model, parts, *, schema=None, purpose, thinking=None):
        if model == ROLES["second"]:
            raise errors.APIError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}})
        return llm.Result(text=json.dumps(ok), json=ok)

    monkeypatch.setattr(llm, "call", call)
    card = check.check(CLAIM)
    assert card["verdict"] == "unclear" and card["confidence"] <= 0.5
    failed = next(c for c in card["checks"] if c["by"] == ROLES["second"])
    assert "APIError" in failed["note"] and failed["verdict"] == "unclear"


def test_both_judges_raising_gives_unclear_zero(monkeypatch, world):
    from google.genai import errors

    def call(model, parts, *, schema=None, purpose, thinking=None):
        raise errors.APIError(500, {"error": {"message": "boom"}})

    monkeypatch.setattr(llm, "call", call)
    card = check.check(CLAIM)
    assert card["verdict"] == "unclear" and card["confidence"] == 0.0 and card["evidence"] == []


def test_malformed_judge_output_does_not_crash(monkeypatch, world):
    bad = judge_json(world)
    bad["evidence"] = ["junk", 5, None, {"image": "current", "box_2d": "no", "label": "x"}]
    bad["quoted_words"] = ["WC-2", 7, None]
    card = run(monkeypatch, bad)
    g = next(c for c in card["checks"] if c.get("check") == "grounding")
    assert g["pass"] is False and card["verdict"] == "unclear"


def two_crops(world):
    cur = world["cur"]
    old = check.superseded_twin(cur, world["sheets"], world["src"], world["rows"])
    return {"current": cur, "superseded": old}, old


def test_confident_verdict_needs_grounding_on_the_current_image(world):
    crops, old = two_crops(world)
    w = next(w for w in old["words"] if w["text"] == "WC-2" and w["bbox"][0] > 1360)
    sup_box = verify.pt_to_box(w["bbox"], old["rect"])
    only_sup = {"verdict": "consistent", "confidence": 0.9, "reason": "r", "quoted_words": ["WC-2"],
                "evidence": [{"image": "superseded", "box_2d": sup_box, "label": "old"}]}
    probs = verify.grounding(only_sup, crops)
    assert probs and any("current" in p for p in probs)              # no current box, quote not in a current box
    both = {**only_sup, "evidence": only_sup["evidence"] + [{"image": "current", "box_2d": box_for(world, {"WC-2"}),
                                                              "label": "now"}]}
    assert verify.grounding(both, crops) == []
    # an unclear verdict may still point at the superseded image
    assert verify.grounding({**only_sup, "verdict": "unclear"}, crops) == []


def test_superseded_only_evidence_ships_unclear_not_confident(monkeypatch, world):
    crops, old = two_crops(world)
    w = next(w for w in old["words"] if w["text"] == "WC-2" and w["bbox"][0] > 1360)
    out = {"verdict": "consistent", "confidence": 0.95, "reason": "r", "quoted_words": ["WC-2"],
           "changed_by_addendum": False, "needs_code": "",
           "evidence": [{"image": "superseded", "box_2d": verify.pt_to_box(w["bbox"], old["rect"]), "label": "old"}]}
    monkeypatch.setattr(llm, "call", fake({"A": out, "B": out}))
    ctx = {"judge": "A", "second": "B", "thinking": "LOW", "schema": {}, "parts": [], "rows": world["rows"],
           "crops": crops, "narrative": []}
    res = verify.verify(CLAIM, ctx)
    assert res["verdict"] == "unclear" and res["confidence"] <= 0.5


def test_box_pt_asymmetric_literal():
    """Non-square rect, hand-computed: an x/y or xmin/ymin swap in either direction changes every number."""
    rect = [100.0, 200.0, 500.0, 1000.0]               # w = 400, h = 800
    box = [100, 250, 600, 750]                          # [ymin, xmin, ymax, xmax]
    # x0 = 100 + 0.25*400 = 200; y0 = 200 + 0.10*800 = 280; x1 = 100 + 0.75*400 = 400; y1 = 200 + 0.60*800 = 680
    assert verify.box_to_pt(box, rect) == pytest.approx([200.0, 280.0, 400.0, 680.0])
    assert verify.pt_to_box([200.0, 280.0, 400.0, 680.0], rect) == box


@pytest.mark.parametrize("text,expect", [
    ("Room 105's", []), ("WC-2's", []), ("3' 4 bars", [36.0]), ("3'-0\" and 4' 6\"", [36.0, 54.0]),
    ("36 in", [36.0]), ("3 feet", [36.0]), ("1/2 in", [0.5]), ("3'-6", [42.0]), ("WC-36\"", []),
])
def test_find_dims_edge_cases(text, expect):
    assert [v for _, v in verify.find_dims(text)] == pytest.approx(expect)


def test_dimension_check_uses_both_judges_quotes(monkeypatch, world):
    a = judge_json(world, "consistent", quoted=("WC-2",), box=box_for(world, {"WC-2"}))
    b = judge_json(world, "consistent", quoted=("WC-2",), box=box_for(world, {"WC-2"}))
    b["quoted_words"] = ["WC-2", "34\""]       # only the second judge quotes a dimension
    monkeypatch.setattr(llm, "call", fake({"A": a, "B": b}))
    ctx = {"judge": "A", "second": "B", "thinking": "LOW", "schema": {}, "parts": [], "rows": world["rows"],
           "crops": {"current": world["cur"]}, "narrative": []}
    res = verify.verify('The grab bar is 36" AFF', ctx)
    assert any(c.get("check") == "dimension" for c in res["checks"])


def test_page_offset_follows_the_local_shift():
    def ws(items):
        return [{"text": t, "bbox": [x, y, x + 20, y + 8]} for t, x, y in items]
    old = ws([("AAA", 100, 100), ("BBB", 200, 100), ("CCC", 300, 100), ("DDD", 1000, 100), ("EEE", 1100, 100),
              ("FFF", 1200, 100)])
    cur = ws([("AAA", 100, 190), ("BBB", 200, 190), ("CCC", 300, 190), ("DDD", 1000, 100), ("EEE", 1100, 100),
              ("FFF", 1200, 100)])
    assert check.page_offset(old, cur, rect=[0, 100, 500, 300], k=3)[:2] == (0.0, 90.0)       # shifted region
    assert check.page_offset(old, cur, rect=[900, 0, 1400, 200], k=3)[:2] == (0.0, 0.0)       # unmoved region
    assert check.page_offset(old[:2], cur[:2], rect=[0, 0, 500, 300]) == (0.0, 0.0, 2)  # too few matches


def test_page_offset_on_real_reissue_ed200():
    """dwg-p046 -> add2-p009 moves content 50-90 pt in y; a crop there must follow it (words by uniqueness)."""
    import collections
    a, b = check._words("dwg-p046"), check._words("add2-p009")

    def once(ws):
        d = collections.defaultdict(list)
        for w in ws:
            d[w["text"]].append(w["bbox"])
        return {t: v[0] for t, v in d.items() if len(v) == 1 and len(t) >= 3}
    A, B = once(a), once(b)
    for target in (50, 70, 90):
        t = next(t for t in A if t in B and round(B[t][1] - A[t][1]) == target)
        dx, dy, _ = check.page_offset(a, b, rect=check.crop_rect(B[t], 3024, 2160))
        assert (dx, dy) == (0.0, float(target))


def test_superseded_twin_rect_follows_the_reissue_shift(world):
    """ED200: the add2 sheet moved content 90 pt down vs the bid set. With add1-p020 out of play the twin is
    dwg-p046, and its crop must be offset so the same words are in it."""
    import collections
    sheets = {k: v for k, v in world["sheets"].items() if k != "add1-p020"}
    a, b = check._words("dwg-p046"), check._words("add2-p009")

    def once(ws):
        d = collections.defaultdict(list)
        for w in ws:
            d[w["text"]].append(w["bbox"])
        return {t: v[0] for t, v in d.items() if len(v) == 1 and len(t) >= 3}
    A, B = once(a), once(b)
    t = next(t for t in A if t in B and round(B[t][1] - A[t][1]) == 90)
    cur = check._crop("add2-p009", check.crop_rect(B[t], 3024, 2160), sheets, world["src"], world["rows"])
    twin = check.superseded_twin(cur, sheets, world["src"], world["rows"])
    assert twin["sheet"] == "dwg-p046" and twin["offset"] == (0.0, 90.0)
    assert twin["rect"][1] == pytest.approx(cur["rect"][1] - 90) and twin["rect"][0] == pytest.approx(cur["rect"][0])
    assert t in {w["text"] for w in twin["words"]}


# ---- named-sheet claims must not land on the title block ----------------------------------------------
PREFIXED = "On P101, the water closets in TOILET 105A are tagged WC-2."
UNPREFIXED = "The water closets in TOILET 105A are tagged WC-2."
TITLE_CLAIM = "Sheet P101 is titled FIRST FLOOR PLUMBING NEW WORK PLAN"


def _centre(c):
    return ((c["rect"][0] + c["rect"][2]) / 2, (c["rect"][1] + c["rect"][3]) / 2)


def test_sheet_prefix_picks_the_same_crop_as_no_prefix(world):
    sheets, src, rows = world["sheets"], world["src"], world["rows"]
    w = sheets["add1-p018"]["width"]
    pre, notes = check.candidates(PREFIXED, sheets, src, rows)
    plain, _ = check.candidates(UNPREFIXED, sheets, src, rows)
    assert [c["sheet"] for c in pre] == ["add1-p018"] and notes
    assert pre[0]["rect"] == pytest.approx(plain[0]["rect"])
    cx, cy = _centre(pre[0])
    assert cx < 0.88 * w and 1360 - 260 < cx < 1425 + 260 and 880 - 260 < cy < 895 + 260   # around 105A
    assert "105A" in {x["text"] for x in pre[0]["words"]}


def test_sheet_number_and_filler_are_not_search_or_identifier_text(world):
    named = check.named_sheets(PREFIXED, world["rows"])
    assert check.search_text(PREFIXED, named) == "the water closets in TOILET 105A are tagged WC-2."
    assert "p101" not in check._id_tokens(check.search_text(PREFIXED, named))


def test_title_block_claim_may_still_pick_the_title_block(world):
    sheets, src, rows = world["sheets"], world["src"], world["rows"]
    assert check.is_title_claim(TITLE_CLAIM) and not check.is_title_claim(PREFIXED)
    cands, _ = check.candidates(TITLE_CLAIM, sheets, src, rows)
    assert cands[0]["sheet"] == "add1-p018"
    assert _centre(cands[0])[0] >= 0.88 * sheets["add1-p018"]["width"]
    assert "NEW" in {x["text"] for x in cands[0]["words"]} or any("PLUMBING" in x["text"] for x in cands[0]["words"])


def test_no_crop_is_centred_in_the_title_strip_for_content_claims(world):
    sheets, src, rows = world["sheets"], world["src"], world["rows"]
    for claim in (PREFIXED, UNPREFIXED, "Grab bar mounting height is 36\" AFF", "Sheet A201 shows cubbies in care rooms",
                  "Project No. 250081.00 sheet P101"):   # last: its raw best hit is the title block
        for c in check.candidates(claim, sheets, src, rows)[0]:
            assert _centre(c)[0] < 0.88 * sheets[c["sheet"]]["width"], (claim, c["sheet"], c["rect"])


# ---- grounding rules: claimed value, superseded boxes, real evidence ------------------------------------
CLAIM_109B = "On P101, the water closets in Toilet 109B are tagged WC-2."


def _box_pt(crop, pt):
    return verify.pt_to_box(pt, crop["rect"])


def test_conflict_quoting_the_claimed_value_is_grounded_109b(world):
    """Live miss: both judges said conflict and quoted WC-2 (the value the claim asserts) with a box that, rightly,
    does not contain it."""
    cur = check.candidates(CLAIM_109B, world["sheets"], world["src"], world["rows"])[0][0]
    box = _box_pt(cur, [1060, 825, 1110, 872])                      # TOILET 109B, WC-1 and the "1" beside it
    out = {"verdict": "conflict", "confidence": 0.95, "reason": "r", "quoted_words": ["TOILET", "109B", "WC-1", "1", "WC-2"],
           "evidence": [{"image": "current", "box_2d": box, "label": "109B closet"}]}
    problems, acc = verify.grounding_report(out, {"current": cur}, CLAIM_109B)
    assert problems == []
    assert acc["WC-2"] == "claim_value" and acc["WC-1"] == "current_box" and acc["109B"] == "current_box"
    # the same output for a consistent verdict is NOT rescued by (b)-free luck: WC-2 is in the claim, so it passes
    # (a), and WC-1 is real evidence; what a consistent verdict may not do is quote a word that is nowhere
    assert verify.grounding({**out, "verdict": "consistent", "quoted_words": ["WC-9"]}, {"current": cur}, CLAIM_109B)
    # and the check detail records which rule accepted what
    assert "claim_value" in str(acc)


def test_conflict_whose_only_evidence_is_copied_from_the_claim_fails(world):
    cur = check.candidates(CLAIM_109B, world["sheets"], world["src"], world["rows"])[0][0]
    box = _box_pt(cur, [1060, 825, 1110, 872])
    out = {"verdict": "conflict", "confidence": 0.9, "reason": "r", "quoted_words": ["TOILET", "109B"],
           "evidence": [{"image": "current", "box_2d": box, "label": "just the room tag"}]}
    problems = verify.grounding(out, {"current": cur}, CLAIM_109B)
    assert any("not just copied from the claim" in p for p in problems)
    # a quote the claim never mentioned, inside the box, is what makes it evidence
    assert verify.grounding({**out, "quoted_words": ["TOILET", "109B", "WC-1"]}, {"current": cur}, CLAIM_109B) == []


def test_superseded_box_accepted_for_conflict_but_not_consistent(world):
    crops, old = two_crops(world)
    cur = crops["current"]
    gone = next(w for w in old["words"] if w["text"] == "WC-2" and w["bbox"][0] > 1360 and w["bbox"][1] < 900)  # now WC-3
    now = next(w for w in cur["words"] if w["text"] == "WC-3" and abs(w["bbox"][1] - gone["bbox"][1]) < 12)
    claim = "The closet beside Toilet 105A is unchanged."                                      # does not mention WC-2
    ev = [{"image": "current", "box_2d": verify.pt_to_box(now["bbox"], cur["rect"]), "label": "now"},
          {"image": "superseded", "box_2d": verify.pt_to_box(gone["bbox"], old["rect"]), "label": "was"}]
    out = {"verdict": "conflict", "confidence": 0.9, "reason": "r", "quoted_words": ["WC-3", "WC-2"], "evidence": ev}
    problems, acc = verify.grounding_report(out, crops, claim)
    assert problems == [] and acc == {"WC-3": "current_box", "WC-2": "superseded_box"}
    bad = verify.grounding({**out, "verdict": "consistent"}, crops, claim)
    assert any("'WC-2'" in p for p in bad)                                 # consistent may not lean on the old sheet


def test_grounding_check_detail_records_accepted_rules(monkeypatch, world):
    out = judge_json(world)                                                # quotes 105A, WC-2, WC-3 in a current box
    monkeypatch.setattr(llm, "call", fake({"A": out, "B": out}))
    ctx = {"judge": "A", "second": "B", "thinking": "LOW", "schema": {}, "parts": [], "rows": world["rows"],
           "crops": {"current": world["cur"]}, "narrative": []}
    g = next(c for c in verify.verify(CLAIM, ctx)["checks"] if c.get("check") == "grounding")
    assert g["pass"] and g["accepted"]["A"]["WC-3"] == "current_box" and "current_box" in g["detail"]


def test_addendum_diff_boxes_land_on_the_changed_words(monkeypatch, world):
    """diff_boxes: box_2d on the crop for words only on one version; the current WC-3s sit inside the crop's 0-1000 frame."""
    card = run(monkeypatch, judge_json(world))
    diff = next(c for c in card["checks"] if c.get("check") == "addendum_diff")
    boxes = diff["diff_boxes"]
    assert diff["current_sheet"] == "add1-p018" and diff["superseded_sheet"].startswith("dwg-")
    assert diff["superseded_crop"].startswith("build/crops/") and (HERE / diff["superseded_crop"]).stat().st_size > 1000
    assert len(boxes["current"]) == sum(diff["only_current"].values())
    assert len(boxes["superseded"]) == sum(diff["only_superseded"].values())
    assert boxes["current"] and boxes["superseded"]
    for b in boxes["current"] + boxes["superseded"]:
        assert 0 <= b[0] < b[2] <= 1000 and 0 <= b[1] < b[3] <= 1000
    cur = world["cur"]
    wc3 = [verify.pt_to_box(w["bbox"], cur["rect"]) for w in cur["words"] if w["text"] == "WC-3"]
    assert wc3 and all(b in boxes["current"] for b in wc3)


def test_diff_boxes_unit():
    crop = {"rect": [0, 0, 100, 100], "words": [{"text": "A", "bbox": [10, 10, 20, 20]}, {"text": "B", "bbox": [30, 30, 40, 40]},
                                                {"text": "B", "bbox": [50, 50, 60, 60]}]}
    assert verify.diff_boxes(crop, {"B": 1}) == [[300, 300, 400, 400]]      # each text used at most n times
    assert verify.diff_boxes(crop, {}) == []


def test_progress_callback_order_and_judge_threads(monkeypatch, world):
    events = []
    monkeypatch.setattr(llm, "call", fake({ROLES["judge"]: judge_json(world), ROLES["second"]: judge_json(world)}))
    card = check.check(CLAIM, progress=lambda step, status, **info: events.append((step, status, info.get("cand"))))
    assert [e[:2] for e in events[:4]] == [("search", "start"), ("search", "done"), ("crop", "start"), ("crop", "done")]
    cands = {e[2] for e in events[4:]}
    assert cands == {0, 1}                                    # two candidates, every later event tagged with its index
    for i in cands:                                           # per candidate: both judges finish before grounding
        ev = [e[:2] for e in events if e[2] == i]
        assert {("judge_a", "start"), ("judge_b", "start"), ("judge_a", "done"), ("judge_b", "done")} <= set(ev)
        g = ev.index(("grounding", "start"))
        assert ev.index(("judge_a", "done")) < g and ev.index(("judge_b", "done")) < g
        assert ev[-4:] == [("grounding", "start"), ("grounding", "done"), ("revision", "start"), ("revision", "done")]
    assert card["verdict"] == "conflict"
