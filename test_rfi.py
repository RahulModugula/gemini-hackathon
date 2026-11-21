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
GATE_Q = "Should the playground gates use padlock latches with standard hinges, or maglocks with self-closing hinges?"
CARD = {
    "claim": "Door 105A is shown 2'-8\" wide on A201 but the schedule requires 3'-0\" clear.",
    "evidence": [{"sheet": "add1-p007", "revision": "add1", "bbox": [0, 0, 1, 1], "crop": "x.png"}],
    "spec_refs": ["08 71 00 2.1.A"],
    "checks": [{"by": "code", "verdict": "conflict", "note": "2'-8\" < 3'-0\""}],
}


@pytest.fixture
def sheets(tmp_path, monkeypatch):
    (tmp_path / "sheets.json").write_text(json.dumps({"rows": [
        {"sheet": "add1-p007", "sheet_number": "A201", "title": "FIRST FLOOR PLAN", "revision": "6/22/26 ADDENDUM #02"}]}))
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
    assert "not an exact substring" in rfi.already_answered(GATE_Q, QA)["reason"]


def test_b2_model_names_a_qa_that_was_not_retrieved(monkeypatch):
    use(monkeypatch, {"answered": True, "qa_id": "add2-q14", "quote": "There is not a generator", "reason": "r"})
    out = rfi.already_answered(GATE_Q, QA)
    assert not out["answered"] and "not a retrieved candidate" in out["reason"]


def test_b3_empty_quote_is_not_answered(monkeypatch):
    use(monkeypatch, {"answered": True, "qa_id": "add1-q6", "quote": "", "reason": "r"})
    assert not rfi.already_answered(GATE_Q, QA)["answered"]


def test_c_unrelated_question_is_a_draft_and_skips_the_model(monkeypatch, sheets):
    f = use(monkeypatch, {"answered": True, "qa_id": "add1-q6", "quote": "Provide padlock", "reason": "should not be called"})
    out = rfi.draft_rfi(dict(CARD, claim="Elevator pit depth conflicts with the structural drawing"), QA)
    assert out["status"] == "draft" and out["answered_by"] is None and f.calls == []


def test_c2_model_says_not_answered_is_a_draft(monkeypatch, sheets):
    use(monkeypatch, {"answered": False, "qa_id": "", "quote": "", "reason": "same topic, different question"})
    out = rfi.draft_rfi(dict(CARD, claim="Is a generator transfer switch required at the panel?"), QA)
    assert out["status"] == "draft"


def test_d_rfi_text_has_sheet_and_revision(monkeypatch, sheets):
    use(monkeypatch, {"answered": False, "qa_id": "", "quote": "", "reason": ""})
    text = rfi.draft_rfi(dict(CARD, claim="Elevator pit depth conflicts"), QA)["text"]
    for needle in ("To:", "From:", "Subject:", "Question:", "Reference:", "Suggested resolution:",
                   "Sheet A201 FIRST FLOOR PLAN, revision 6/22/26 ADDENDUM #02", "08 71 00 2.1.A", "2'-8\" < 3'-0\""):
        assert needle in text, needle


def test_d2_text_without_sheets_json_falls_back_to_card_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(rfi, "BUILD", tmp_path)
    assert "Sheet add1-p007, revision add1" in rfi.rfi_text(CARD)


def test_llm_error_propagates(monkeypatch):
    def boom(*a, **k):
        raise llm.CapExceeded("cap")
    monkeypatch.setattr(rfi.llm, "call", boom)
    with pytest.raises(llm.CapExceeded):
        rfi.already_answered(GATE_Q, QA)
