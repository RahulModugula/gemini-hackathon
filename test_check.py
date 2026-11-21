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
    t0 = time.monotonic()
    run(monkeypatch, judge_json(world), delay=0.4)
    assert time.monotonic() - t0 < 0.75      # two 0.4 s calls overlap; serial would be >= 0.8 s plus rendering


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
    for claim_dim, want_pass in ((f'{size + 5:g}"', False), (w["text"], True)):
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
