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
    # a conflict may quote the claim's own (contradicted) value next to the boxed real one: the dimension check must
    # compare only boxed quotes, or the copied claim value makes it 'consistent' and vetoes a correct conflict
    claim_dim = f'{size + 5:g}"'
    out = judge_json(world, "conflict", quoted=(claim_dim, w["text"]), box=box)
    monkeypatch.setattr(llm, "call", fake({"A": out, "B": out}))
    res = verify.verify(f"The pipe is {claim_dim}", ctx_for(world, world["rows"], out, out))
    dim = next(c for c in res["checks"] if c.get("check") == "dimension")
    assert dim["code_verdict"] == "conflict" and dim["pass"] and res["verdict"] == "conflict", (dim, res["reason"])


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
    w = next(w for w in world["cur"]["words"] if w["text"].endswith('"') and verify.parse_dim(w["text"]) is not None)
    bx = verify.pt_to_box(w["bbox"], world["cur"]["rect"])
    bx = [max(0, bx[0] - 3), max(0, bx[1] - 3), min(1000, bx[2] + 3), min(1000, bx[3] + 3)]
    b = judge_json(world, "consistent", quoted=(w["text"],), box=bx)   # only the second judge quotes a (boxed) dimension
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


# ---- coverage fixes: stale claims on A900 list entries; E200 room tag ----------------------------------
@pytest.mark.parametrize("room,old,new", [("103", '19"', '18"'), ("102", '15"', '18"')])
def test_stale_list_entry_claim_picks_the_list_not_the_room_label(world, room, old, new):
    """A900: 'TWOS 103 - 01' (a lower-right drawing label) ties the sink-height list on the room number; the list
    entry holds the OLD value only on the superseded sheet, so the twin must count when ranking hits."""
    sheets, src, rows = world["sheets"], world["src"], world["rows"]
    claim = f'On A900, the list entry for room {room} is {old}.'
    cands, _ = check.candidates(claim, sheets, src, rows)
    c = cands[0]
    assert c["sheet"] == "add1-p013"
    texts = {w["text"] for w in c["words"]}
    assert "RIM" in texts and new in texts and "HEIGHTS:" in texts, (c["rect"], sorted(texts)[:20])
    twin = check.superseded_twin(c, sheets, src, rows)
    assert old in {w["text"] for w in twin["words"]}


def test_score_counts_twin_tokens_but_prefers_current():
    crop = {"words": [{"text": "103"}]}
    twin = {"words": [{"text": '19"'}]}
    claim = 'room 103 is 19"'
    assert check._score(claim, crop, 5, twin=twin) > check._score(claim, crop, 0)
    assert check._score(claim, crop, 0, twin=twin)[:2] == (2, 1)


def test_dev_05c_recorded_judge_output_is_grounded(world):
    """E200 'tag at room 109 reads A-36': both judges said consistent; their recorded boxes must ground."""
    sheets, src, rows = world["sheets"], world["src"], world["rows"]
    claim = "On E200, the tag at room 109 reads A-36."
    c = check.candidates(claim, sheets, src, rows)[0][0]
    out = {"verdict": "consistent", "confidence": 0.92, "quoted_words": ["FLEX", "109", "A-36", "TP,", '60"AFF'],
           "evidence": [{"image": "current", "box_2d": [814, 0, 860, 64]}, {"image": "current", "box_2d": [678, 198, 735, 284]}]}
    probs, acc = verify.grounding_report(out, {"current": c}, claim)
    assert probs == [] and acc["A-36"] == "current_box"


# ---- review findings: consistent must box the claim's identifiers; dimension claims need a boxed dimension -------
_RV_CROP = {"sheet": "p1", "sheet_number": "P101", "rect": [0, 0, 1000, 1000],
            "words": [{"text": "105A", "bbox": [100, 100, 140, 110]}, {"text": "WC-3", "bbox": [600, 600, 640, 610]},
                      {"text": "WC-2", "bbox": [700, 700, 740, 710]}, {"text": "TYP", "bbox": [300, 300, 330, 310]}]}


def _rv(v, q, *boxes):
    return {"verdict": v, "confidence": 0.9, "reason": "r", "quoted_words": q,
            "evidence": [{"image": "current", "box_2d": b, "label": ""} for b in boxes]}


def test_consistent_cannot_lean_on_an_unboxed_claim_value():
    """Claim says WC-2; judges box only the room number and quote WC-2 from the claim: nothing proves WC-2."""
    out = _rv("consistent", ["105A WC-2"], [95, 95, 115, 145])
    probs, _ = verify.grounding_report(out, {"current": _RV_CROP}, "On P101, Room 105A has a WC-2 water closet")
    assert probs
    out = _rv("consistent", ["105A"], [95, 95, 115, 145])          # quoting only the room is not enough either
    probs, _ = verify.grounding_report(out, {"current": _RV_CROP}, "On P101, Room 105A has a WC-2 water closet")
    assert any("WC-2" in p or "wc2" in p for p in probs), probs
    ok = _rv("consistent", ["105A", "WC-2"], [95, 95, 115, 145], [695, 695, 715, 745])
    assert verify.grounding_report(ok, {"current": _RV_CROP}, "On P101, Room 105A has a WC-2 water closet")[0] == []


def test_conflict_on_a_dimension_claim_needs_a_boxed_dimension(monkeypatch):
    o = _rv("conflict", ["TYP", '36"'], [295, 295, 315, 335])
    monkeypatch.setattr(verify, "_run_judge", lambda m, p, c: (o, 0.1))
    r = verify.verify('Door 101 is 36" wide', {"judge": "a", "second": "b", "crops": {"current": _RV_CROP},
                                               "rows": {"p1": {"current": True}}, "narrative": ()})
    assert r["verdict"] == "unclear", r


# ---- coverage v3: schedule rows / note lines cut off at the crop edge -----------------------------------------
@pytest.mark.parametrize("claim,sheet,need", [
    ('Door 100A.1 is a 3\'-0" by 7\'-0" door with a 20 minute rating and the FEMA impact rated door comment.',
     "dwg-p024", {"100A.1", "3'", "7'", "20", "MIN.", "FEMA", "IMPACT"}),
    ("Door 099.1, the vestibule door, is 7'-2\" high with a door operator.", "dwg-p024", {"099.1", "7'", '2"', "OPERATOR"}),
    ('The plumbing fixture schedule lists WC-3 as the American Standard Cadet 2462.016, floor mount at a 15.0" rim height.',
     "add1-p017", {"WC-3", "AMERICAN", '15.0"', "RIM"}),
    ("E300 fixture type AA is the Lithonia 2x4 flat panel troffer at 3200 lumens and 24.6 watts.",
     "dwg-p051", {"AA", "TROFFER", "3200", "24.6", "LITHONIA"}),
    ("L2.00 has the playground equipment supplied by the contractor from Playworld.", "dwg-p011",
     {"5.03", "playground", "provided", "Northland", "Recreation."}),
    ("The Color and Material Schedule lists the toilet partition TP-1 as Scranton Hiny Hiders in Linen.", "add2-p007",
     {"TP-1", "Scranton", "Hiders", "Linen"}),
])
def test_crop_covers_the_whole_schedule_row(world, claim, sheet, need):
    """Dev v3 live misses: both judges said 'unclear, the size / manufacturer / remarks column is cut off'."""
    cands, _ = check.candidates(claim, world["sheets"], world["src"], world["rows"])
    c = next(c for c in cands if c["sheet"] == sheet)
    texts = {w["text"] for w in c["words"]}
    assert need <= texts, (c["rect"], sorted(need - texts))
    assert c["rect"][2] - c["rect"][0] <= check.ROW_MAX_W + 1e-6


def _w(t, x, block, y=100):
    return {"text": t, "bbox": [x, y, x + 30, y + 10], "block": block}


def test_hit_rect_only_widens_and_caps_width():
    row = [_w(t, x, 7) for t, x in (("A", 100), ("B", 400), ("C", 700), ("C2", 900), ("C3", 1000), ("D", 3000))]
    base = check.crop_rect([400, 100, 430, 110], 4000, 3000)
    r = check.hit_rect([400, 100, 430, 110], 4000, 3000, row)
    assert r[0] <= base[0] and r[2] >= base[2] and r[1] == base[1] and r[3] == base[3]
    assert r[0] <= 100 - check.ROW_PAD and r[2] >= 1030 and r[2] < 3000   # D is 1970 pt past C3: not this row
    off_row = [_w("Z", 1500, 7, y=300)] * 6
    assert check.hit_rect([400, 100, 430, 110], 4000, 3000, off_row) == base
    long_row = [_w(str(i), x, 7) for i, x in enumerate(range(0, 3900, 200))]
    r = check.hit_rect([2000, 100, 2030, 110], 4000, 3000, long_row)
    assert abs((r[2] - r[0]) - check.ROW_MAX_W) < 1e-6 and r[0] <= 2000 and r[2] >= 2030


def test_plan_labels_on_one_line_do_not_widen_the_crop():
    """A plan tag (one small text block) with other labels that happen to sit on its line keeps the default crop;
    a schedule column in another block joins only when our block is a real text row on the exact same baseline."""
    base = check.crop_rect([400, 100, 430, 110], 4000, 3000)
    plan = [_w("WC-3", 400, 1), _w("105A", 430, 1)] + [_w(f"L{i}", 700 + 250 * i, 10 + i) for i in range(5)]
    assert check.hit_rect([400, 100, 430, 110], 4000, 3000, plan) == base
    sched = [_w(f"c{i}", 400 + 40 * i, 1) for i in range(5)] + [_w("REMARK", 900, 2), _w("OFFSET", 1000, 3, y=104)]
    r = check.hit_rect([400, 100, 430, 110], 4000, 3000, sched)
    assert r[2] >= 930 + check.ROW_PAD - 1e-6                   # same-baseline column in block 2 joins
    assert check.row_span([400, 100, 430, 110], sched)[1] == 930   # the off-baseline word in block 3 does not


_CTX_CROP = {"sheet": "e1", "sheet_number": "E200", "rect": [0, 0, 1000, 1000],
             "words": [{"text": "NOTES:", "bbox": [100, 100, 140, 110]}, {"text": "HEADING:", "bbox": [100, 282, 160, 292]},
                       {"text": "FACP", "bbox": [100, 300, 140, 310]},
                       {"text": "SERVES", "bbox": [150, 300, 190, 310]}, {"text": "OWLS", "bbox": [200, 300, 240, 310]},
                       {"text": "A-36", "bbox": [600, 600, 640, 610]}, {"text": "A-13", "bbox": [650, 620, 690, 630]},
                       {"text": "109", "bbox": [600, 500, 630, 510]}]}
_NOTE_BOX, _A36_BOX = [295, 95, 315, 245], [495, 595, 615, 645]       # FACP SERVES OWLS / 109 + A-36


def test_unboxed_context_quote_is_accepted_once_boxed_evidence_exists():
    """Dev v3 live misses (n79, 05s): both judges right, but one also quoted a heading / a neighbouring tag that is
    printed in the crop outside its box, and grounding vetoed the card."""
    ok = _rv("consistent", ["HEADING:", "FACP SERVES OWLS"], _NOTE_BOX)          # heading one line above the box
    probs, acc = verify.grounding_report(ok, {"current": _CTX_CROP}, "The FACP serves the OWLS space")
    assert probs == [] and acc["HEADING:"] == "crop_word" and acc["OWLS"] == "current_box"
    conflict = _rv("conflict", ["109", "A-36", "A-13"], _A36_BOX)
    probs, acc = verify.grounding_report(conflict, {"current": _CTX_CROP}, "On E200, the tag at room 109 reads A-39.")
    assert probs == [] and acc["A-13"] == "crop_word" and acc["A-36"] == "current_box"


def test_unboxed_context_never_replaces_evidence():
    """The false-alarm guards stay: a word not printed in the crop, a conflict whose only non-claim word is unboxed,
    and a consistent verdict whose claim identifier is unboxed all still fail."""
    crops = {"current": _CTX_CROP}
    invented = _rv("consistent", ["FACP SERVES OWLS", "CHILDCARE"], _NOTE_BOX)
    assert verify.grounding_report(invented, crops, "The FACP serves the OWLS space")[0]
    only_unboxed = _rv("conflict", ["109", "A-13"], [495, 595, 515, 635])         # box holds 109 only
    probs, _ = verify.grounding_report(only_unboxed, crops, "On E200, the tag at room 109 reads A-39.")
    assert any("A-13" in p for p in probs)
    ident = _rv("consistent", ["109", "A-13"], [495, 595, 515, 635])
    probs, _ = verify.grounding_report(ident, crops, "On E200, the tag at room 109 reads A-13.")
    assert any("a13" in p for p in probs), probs


def test_unboxed_context_must_sit_next_to_a_box():
    """Review finding: boxing one unrelated word must not let the deciding value come from anywhere in a (wide)
    crop. A word far from every current box is not context, so it still fails."""
    crops = {"current": _CTX_CROP}
    far_conflict = _rv("conflict", ["SERVES", "A-13"], [295, 145, 315, 195])      # box: SERVES; A-13 is 400+ pt away
    probs, _ = verify.grounding_report(far_conflict, crops, "The tag at room 109 reads A-39")
    assert any("A-13" in p for p in probs), probs
    far_consistent = _rv("consistent", ["FACP", "NOTES:"], [295, 95, 315, 145])   # NOTES: is 190 pt above FACP
    assert verify.grounding_report(far_consistent, crops, "The FACP has notes")[0]


def test_answer_value_never_uses_an_unboxed_context_word():
    card = {"verdict": "consistent", "reason": "r", "evidence": [{"bbox": [0, 0, 1, 1]}],
            "checks": [{"by": "a", "quoted_words": ["NOTES:", "OWLS"]},
                       {"by": "code", "check": "grounding", "pass": True,
                        "accepted": {"a": {"NOTES:": "crop_word", "OWLS": "current_box"}}}]}
    assert check._answer(card)["value"] == "OWLS"


def test_unboxed_context_dimension_does_not_back_a_dimension_claim(monkeypatch):
    crop = {**_CTX_CROP, "words": _CTX_CROP["words"] + [{"text": '42"', "bbox": [650, 640, 680, 650]}]}
    o = _rv("conflict", ["A-13", '42"'], [615, 645, 635, 695])           # box: A-13; 42" printed just below it
    monkeypatch.setattr(verify, "_run_judge", lambda m, p, c: (o, 0.1))
    r = verify.verify('Door 101 is 36" wide', {"judge": "a", "second": "b", "crops": {"current": crop},
                                               "rows": {"e1": {"current": True}}, "narrative": ()})
    assert r["verdict"] == "unclear", r


@pytest.mark.parametrize("claim", ["Demolition keynote D07 removes the existing ceiling and soffit throughout the entire project area.",
                                   "Demolition keynote D07 is removal of the existing carpet flooring."])
def test_keynote_claim_gets_the_legend_not_the_bare_tag(world, claim):
    """Dev v3 live misses: AD201's best hit is the D07 tag on the plan; both judges said 'the keynote legend is not
    in the crop'. The legend on the same sheet holds D07 and the claim's words."""
    cands, _ = check.candidates(claim, world["sheets"], world["src"], world["rows"])
    c = next(c for c in cands if c["sheet"] == "add2-p006")
    texts = {w["text"] for w in c["words"]}
    assert {"D07", "REMOVE", "CEILING", "SOFFIT", "THROUGHOUT"} <= texts, c["rect"]


def test_judge_prompt_treats_placeholders_like_silence(world):
    """Dev v3 fix run: with the whole A1100 row in the crop, both judges called 'Bright White grout' / 'herringbone
    pattern' a conflict against "to be selected from awarded MFR standard color" / "Installation: TBD". A value left
    open is not a contradicting value; the rule must reach the judges in every claim prompt."""
    parts = check.build_parts("A1100 names Bright White as the grout color for PTW-1.", {})
    text = " ".join(p.text for p in parts if getattr(p, "text", None))
    assert "Silence is not a contradiction" in text
    assert "TBD" in text and "to be selected" in text


def test_a_dominant_search_hit_stays_a_candidate(world):
    """Dev v3 live miss: the claim's (wrong) number 250 re-ranked two sheets that merely print '250' above G002's
    code summary, the search hit that scores over 3x anything else; both judges then saw the wrong sheets."""
    claim = "The code summary allows 250 feet of exit access travel distance in non-sprinklered educational occupancies."
    cands, _ = check.candidates(claim, world["sheets"], world["src"], world["rows"])
    assert len(cands) == 2
    assert any(c["sheet"] == "add1-p006" and {"200", "FEET", "TRAVEL"} <= {w["text"] for w in c["words"]}
               for c in cands), [c["sheet"] for c in cands]
