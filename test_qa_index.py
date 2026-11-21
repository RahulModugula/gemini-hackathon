import re
from pathlib import Path

import pymupdf
import pytest

import qa_index

DATA = Path(__file__).parent / "data"
have = (DATA / "addendum-1.pdf").exists() and (DATA / "addendum-2.pdf").exists()
pytestmark = pytest.mark.skipif(not have, reason="data/addendum-*.pdf missing")

# Hand count from the PDF text (2026-11-21): Add 1 pp.2-3 = 8 "Question:" entries; Add 2 pp.2-4 = 18 (the access-control
# question appears twice, 5th and 7th; one extra "Clarification:" for sheet ME100 has no Question/Response form).
HAND = {"add1": 8, "add2": 18}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("build")
    qa, spec = qa_index.build(out)
    return qa, spec


def by(qa, tag):
    return [q for q in qa if q["id"].startswith(tag + "-")]


def raw_question_count(pdf):
    """Independent count: 'Question:' at line start on the letter-size pages before 'Drawings:'."""
    n = 0
    for page in pymupdf.open(pdf):
        t = page.get_text()
        if page.rect.width > 700 or re.search(r"^Drawings\s*:", t, re.M):
            n += len(re.findall(r"^Question\s*:", t.split("Drawings:")[0], re.M))
            break
        n += len(re.findall(r"^Question\s*:", t, re.M))
    return n


@pytest.mark.parametrize("tag,fn", [("add1", "addendum-1.pdf"), ("add2", "addendum-2.pdf")])
def test_counts_match_hand_count(built, tag, fn):
    qa, _ = built
    assert len(by(qa, tag)) == HAND[tag] == raw_question_count(DATA / fn)
    assert [q["id"] for q in by(qa, tag)] == [f"{tag}-q{i}" for i in range(1, HAND[tag] + 1)]


def test_add1_first_and_last_exact(built):
    a = by(built[0], "add1")
    assert a[0] == {"id": "add1-q1", "source": "Addendum 1", "page": 2,
                    "question": "Division 27 specification not included in project manual.",
                    "response": "Specification is added in Addendum #01"}
    assert a[-1] == {"id": "add1-q8", "source": "Addendum 1", "page": 2,
                     "question": "Who is to be responsible for supply and installation of the dishwasher and/or kitchen equipment?",
                     "response": "The submitted contract drawings are limited to rough in scope for the kitchen equipment. "
                                 "Equipment procurement and installation will be coordinated through Outagamie Count and the YMCA."}


def test_add2_first_and_last_exact(built):
    a = by(built[0], "add2")
    assert a[0]["page"] == 2 and a[0]["response"] == "Additional fire alarm scope added in Addendum #01."
    assert a[0]["question"] == ("The existing FACP is going to be removed, however, there is a large portion of the building "
                                "that is not part of this renovation. Is anything needed for detection and notification in the "
                                "areas marked “not in scope”.")
    assert a[-1]["page"] == 4 and a[-1]["question"].startswith("Is there a drawing for the fencing with footing depth?")
    assert a[-1]["response"].startswith("Yes, all footings to be set in concrete.")
    assert a[-1]["response"].endswith("Only top and bottom rails, no middle rail. Black vinyl fence.")


def test_question_spanning_pages_keeps_response(built):
    meraki = next(q for q in by(built[0], "add2") if "delivery schedule for Owner-furnished Meraki" in q["question"])
    assert meraki["page"] == 2 and meraki["response"].startswith("Delivery schedule for the owner provided")


def test_no_page_numbers_or_bullets_leak(built):
    for q in built[0]:
        assert "•" not in q["question"] + q["response"]
        assert not re.search(r"\s\d\s+(Question|Response)", q["question"] + q["response"])
        assert "Question:" not in q["response"] and "Clarification" not in q["response"]
        assert q["question"] and q["response"]


def test_spec_sections_indexed(built):
    spec = built[1]
    assert {(c["section"]) for c in spec} == {"12 24 13", "23 09 14", "27 00 00"}
    assert min(c["page"] for c in spec) == 23 and max(c["page"] for c in spec) == 36
    shade = [c for c in spec if c["section"] == "12 24 13"]
    assert any("cordless manually operated roller-shade" in c["text"] for c in shade)
    assert all(len(c["text"]) <= qa_index.CHUNK_MAX + 400 for c in spec)
