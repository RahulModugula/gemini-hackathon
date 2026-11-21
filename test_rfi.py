import json
from types import SimpleNamespace

import pytest

import llm
import rfi

QA = [
    {"id": "add1-q1", "source": "Addendum 1", "page": 2, "question": "Division 27 specification not included in project manual.",
     "response": "Specification is added in Addendum #01"},
    {"id": "add1-q2", "source": "Addendum 1", "page": 2, "question": "Who is responsible for playground equipment and surface installation?",
     "response": "We will work with the client to select a playground consultant who will specify the equipment and surfacing."},
    {"id": "add1-q6", "source": "Addendum 1", "page": 2,
     "question": "Do the gates need a maglock/strike setup with self-closing hinges or will padlock latches and standard hinges be ok?",
     "response": "Provide padlock latches and standard hinges."},
    {"id": "add2-q11", "source": "Addendum 2", "page": 3, "question": "Who is responsible for playground equipment and surface installation?",
     "response": "YMCA’s contractor to provide and install the play surface and playground equipment (updated from Addendum #01)."},
    {"id": "add2-q14", "source": "Addendum 2", "page": 4, "question": "Is there a generator as part of this scope?",
     "response": "There is not a generator included with this project."},
]

# Labelled queries for calibrating rfi.MIN_SCORE against the REAL index (build/qa_index.json). RELATED are rephrasings of
# real addenda questions; UNRELATED are checker-style claims/questions the addenda never addressed.
RELATED = [
    "Who is going to supply and put in the playground equipment and the play surface?",
    "Should the yard gates get padlock latches and standard hinges, or maglocks with self-closing hinges?",
    "Is a generator included in this project?",
    "Who provides the Meraki switches and access points, and who configures them?",
    "Does the contractor or the owner supply and install the dishwasher and kitchen equipment?",
    "Is the activity center mural paint by the GC and what spec applies?",
    "Chain operated or cordless roller shades: which operation does 12 24 13 require?",
    "Is there an existing sprinkler system, and are flow and tamper switches needed?",
    "Do we need metal deck under the storm shelter concrete roof slab?",
    "Does PL-2 apply anywhere on the project?",
    "Are duct smoke detectors required on the furnaces?",
    "Is Division 27 cabling in the electrical subcontractor's scope or bid separately?",
    "Who pays for special testing and inspections, the contractor or the owner?",
    "Will the interior playground fence be 4 ft if alternate A1 is accepted?",
]
UNRELATED = [
    "Door 105A is 2'-8\" wide on A201 but the schedule requires 3'-0\" clear.",
    "The corridor width on A201 is 5'-0\" but the code plan says 6'-0\" minimum egress.",
    "Elevator pit depth conflicts with the structural drawing.",
    "Is the roof drain overflow scupper size consistent with the plumbing schedule?",
    "The grab bar height in toilet 109A is 36 in on A800 but 33 in on the ADA note.",
    "Window type W3 glazing U-value does not match the energy compliance sheet.",
    "Panel C breaker sizes on E400 do not add up to the main service rating.",
    "Water closet WC-2 flush valve rough-in height differs between P101 and the fixture schedule.",
    "The wall type A4 stud gauge on A1000 conflicts with the structural notes.",
    "Concrete footing rebar spacing on S500 differs from the general notes.",
    "Ceiling height in the infant room is 9'-0\" on A301 but 8'-6\" on the finish schedule.",
    "Which hollow metal frame profile is used at the exterior door in the warehouse?",
]
GATE_Q = "Should the playground gates use padlock latches with standard hinges, or maglocks with self-closing hinges?"
CARD = {
    "claim": "Door 105A is shown 2'-8\" wide on A201 but the schedule requires 3'-0\" clear.",
    "evidence": [{"sheet": "add1-p007", "revision": "add1", "bbox": [0, 0, 1, 1], "crop": "x.png"}],
    "spec_refs": ["08 71 00 2.1.A"],
    "checks": [{"by": "code", "verdict": "conflict", "note": "2'-8\" < 3'-0\""}],
}


@pytest.fixture
def sheets(tmp_path, monkeypatch):
    # rows copied from the real build/sheet_index.json (T5 output)
    (tmp_path / "sheet_index.json").write_text(json.dumps({"summary": {}, "rows": [
        {"sheet": "add1-p007", "source": "add1", "issue_order": 1, "sheet_number": "A201", "title": "FIRST FLOOR PLAN",
         "revision": "6/12/26 ADDENDUM #01", "revision_no": 1, "issue_date": "6/02/2026",
         "revisions": [{"no": 1, "date": "6/12/26", "description": "ADDENDUM #01"}], "discipline": "Architectural",
         "word_count": 1903, "tiles": 35, "current": True, "superseded_by": None},
        {"sheet": "add1-p018", "source": "add1", "issue_order": 1, "sheet_number": "P101",
         "title": "FIRST FLOOR PLUMBING NEW WORK PLAN", "revision": "06/12/2026 ADDENDUM #01", "revision_no": 1,
         "issue_date": "06/02/2026", "revisions": [{"no": 1, "date": "06/12/2026", "description": "ADDENDUM #01"}],
         "discipline": "Plumbing", "word_count": 592, "tiles": 35, "current": True, "superseded_by": None}]}))
    monkeypatch.setattr(rfi, "BUILD", tmp_path)
    return tmp_path


class Fake:
    """Stands in for llm.call; records calls."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def __call__(self, model, parts, *, schema=None, purpose, thinking=None):
        self.calls.append(SimpleNamespace(model=model, parts=parts, schema=schema, purpose=purpose, thinking=thinking))
        a = self.answer(parts[0]) if callable(self.answer) else self.answer
        return llm.Result(text=json.dumps(a), json=a)


def use(monkeypatch, answer):
    f = Fake(answer)
    monkeypatch.setattr(rfi.llm, "call", f)
    return f


def test_a_rephrased_addendum_question_is_blocked_with_exact_quote(monkeypatch, sheets):
    f = use(monkeypatch, {"answered": True, "qa_id": "add1-q6", "quote": "Provide padlock latches and standard hinges.",
                          "reason": "Response selects padlock latches."})
    card = dict(CARD, claim=GATE_Q)
    out = rfi.draft_rfi(card, QA)
    assert out["status"] == "blocked" and out["answered_by"] == "add1 p2 Q6"
    assert out["quote"] == "Provide padlock latches and standard hinges." and out["quote"] in QA[2]["response"]
    c = f.calls[0]
    assert (c.model, c.thinking, c.purpose) == ("gemini-4-hydrogen", "LOW", "rfi_already_answered")
    assert set(c.schema["properties"]) == {"answered", "qa_id", "quote", "reason"}


def test_later_addendum_supersedes_identical_question(monkeypatch):
    q = "Who is responsible for supplying and installing the playground equipment and play surface?"
    f = use(monkeypatch, lambda prompt: {"answered": True, "qa_id": "add2-q11", "quote": "YMCA’s contractor to provide and install",
                                         "reason": "r"})
    out = rfi.already_answered(q, QA)
    assert out["answered_by"] == "add2 p3 Q11"
    assert "[add1-q2]" not in f.calls[0].parts[0] and "[add2-q11]" in f.calls[0].parts[0]


def test_b_fabricated_quote_is_not_blocked(monkeypatch, sheets):
    use(monkeypatch, {"answered": True, "qa_id": "add1-q6", "quote": "Provide magnetic locks and self-closing hinges.",
                      "reason": "made up"})
    out = rfi.draft_rfi(dict(CARD, claim=GATE_Q), QA)
    assert out["status"] == "draft" and out["answered_by"] is None and out["quote"] is None
    assert "not a substantial exact substring" in rfi.already_answered(GATE_Q, QA)["reason"]


def test_b2_model_names_a_qa_that_was_not_retrieved(monkeypatch):
    use(monkeypatch, {"answered": True, "qa_id": "add2-q14", "quote": "There is not a generator", "reason": "r"})
    out = rfi.already_answered(GATE_Q, QA)
    assert not out["answered"] and "not a retrieved candidate" in out["reason"]


def test_b3_empty_quote_is_not_answered(monkeypatch):
    use(monkeypatch, {"answered": True, "qa_id": "add1-q6", "quote": "", "reason": "r"})
    assert not rfi.already_answered(GATE_Q, QA)["answered"]


def test_c_unrelated_question_is_a_draft_and_skips_the_model(monkeypatch, sheets):
    f = use(monkeypatch, {"answered": True, "qa_id": "add1-q6", "quote": "Provide padlock", "reason": "should not be called"})
    out = rfi.draft_rfi(dict(CARD, claim="Window type W3 glazing U-value does not match the energy compliance sheet"), QA)
    assert out["status"] == "draft" and out["answered_by"] is None and f.calls == []


def test_c2_model_says_not_answered_is_a_draft(monkeypatch, sheets):
    use(monkeypatch, {"answered": False, "qa_id": "", "quote": "", "reason": "same topic, different question"})
    out = rfi.draft_rfi(dict(CARD, claim="Is a generator transfer switch required at the panel?"), QA)
    assert out["status"] == "draft"


def test_d_rfi_text_has_sheet_and_revision(monkeypatch, sheets):
    use(monkeypatch, {"answered": False, "qa_id": "", "quote": "", "reason": ""})
    text = rfi.draft_rfi(dict(CARD, claim="Elevator pit depth conflicts"), QA)["text"]
    for needle in ("To:", "From:", "Subject:", "Question:", "Reference:", "Suggested resolution:",
                   "Sheet A201 \u2014 FIRST FLOOR PLAN, rev 6/12/26 ADDENDUM #01", "08 71 00 2.1.A", "2'-8\" < 3'-0\""):
        assert needle in text, needle


def test_d2_text_without_sheets_json_falls_back_to_card_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(rfi, "BUILD", tmp_path)
    assert "Sheet add1-p007, rev add1" in rfi.rfi_text(CARD)


def test_llm_error_propagates(monkeypatch):
    def boom(*a, **k):
        raise llm.CapExceeded("cap")
    monkeypatch.setattr(rfi.llm, "call", boom)
    with pytest.raises(llm.CapExceeded):
        rfi.already_answered(GATE_Q, QA)


def test_reference_uses_real_sheet_index_shape(sheets):
    card = dict(CARD, evidence=[{"sheet": "add1-p018", "revision": "add1"}])
    assert "Sheet P101 \u2014 FIRST FLOOR PLUMBING NEW WORK PLAN, rev 06/12/2026 ADDENDUM #01" in rfi.rfi_text(card)


@pytest.mark.parametrize("quote", ["a", "padlock", "Provide", "latches and", "Provide padlock latches", "padlock latches and"])
def test_token_quotes_are_rejected(monkeypatch, quote):
    assert quote in QA[2]["response"]
    use(monkeypatch, {"answered": True, "qa_id": "add1-q6", "quote": quote, "reason": "r"})
    out = rfi.already_answered(GATE_Q, QA)
    assert not out["answered"] and "substantial" in out["reason"]


def test_quote_ok_rules():
    r = "Provide padlock latches and standard hinges."
    assert rfi.quote_ok("Provide padlock latches and standard hinges.", r)
    assert rfi.quote_ok("padlock latches and standard hinges", r)
    assert not rfi.quote_ok("standard hinges.", r)          # 3 words... too short
    assert not rfi.quote_ok("Provide padlock latches", r)    # < 4 words
    assert rfi.quote_ok("Specification is added", "Specification is added in Addendum #01") is False  # 3 words
    assert rfi.quote_ok("Not required", "Not required") and rfi.quote_ok("Not req", "Not required.") is True
    assert not rfi.quote_ok("No", "Not required")            # < 40% of a short response


@pytest.mark.parametrize("bad", [["answered"], "yes", None, 3,
                                 {"answered": True, "qa_id": "add1-q6", "quote": ["Provide padlock latches"], "reason": "r"},
                                 {"answered": True, "qa_id": "add1-q6", "quote": 7, "reason": "r"},
                                 {"answered": "true", "qa_id": "add1-q6", "quote": "Provide padlock latches and standard hinges.",
                                  "reason": "r"},
                                 {"answered": True, "qa_id": ["add1-q6"], "quote": "Provide padlock latches and standard hinges.",
                                  "reason": "r"}])
def test_malformed_model_output_is_not_answered(monkeypatch, bad):
    f = Fake(None)
    f.answer = bad
    monkeypatch.setattr(rfi.llm, "call", lambda *a, **k: llm.Result(text="", json=bad))
    assert not rfi.already_answered(GATE_Q, QA)["answered"]


def test_candidates_listed_latest_addendum_first_and_told_so(monkeypatch):
    f = use(monkeypatch, {"answered": False, "qa_id": "", "quote": "", "reason": ""})
    rfi.already_answered("Who provides the generator and the padlock latches for the playground gates?", QA, min_score=0.0)
    prompt = f.calls[0].parts[0]
    assert "Later addenda supersede earlier answers" in prompt
    ids = [i for i in ("add1-q6", "add1-q2", "add2-q14", "add2-q11") if f"[{i}]" in prompt]
    assert len(ids) >= 2
    order = sorted(ids, key=lambda i: prompt.index(f"[{i}]"))
    assert [i.split("-")[0] for i in order] == sorted((i.split("-")[0] for i in order), reverse=True)


def _real_qa():
    import qa_index
    from pathlib import Path
    d = Path(__file__).parent / "data"
    if not (d / "addendum-1.pdf").exists():
        pytest.skip("addendum PDFs missing")
    return qa_index.parse_qa(d / "addendum-1.pdf", "add1") + qa_index.parse_qa(d / "addendum-2.pdf", "add2")


def test_threshold_separation_on_labelled_queries():
    qa = _real_qa()
    best = lambda q: rfi.retrieve(q, qa)[0][0]
    assert len(RELATED) >= 10 and len(UNRELATED) >= 10
    rel, unr = sorted(best(q) for q in RELATED), sorted(best(q) for q in UNRELATED)
    print(f"related n={len(rel)} min={rel[0]:.3f} median={rel[len(rel) // 2]:.3f}; "
          f"unrelated n={len(unr)} max={unr[-1]:.3f} median={unr[len(unr) // 2]:.3f}; MIN_SCORE={rfi.MIN_SCORE}")
    assert rel[0] >= rfi.MIN_SCORE, "a real rephrased question would never reach the model"
    assert sum(u >= rfi.MIN_SCORE for u in unr) <= len(unr) // 4   # most unrelated claims skip the model
