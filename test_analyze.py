import json
from pathlib import Path

import pytest

import analyze_sheets

BUILD, DATA = Path("build"), Path("data")
pytestmark = pytest.mark.skipif(
    not (BUILD / "sheets.json").exists() or not (DATA / "addendum-1.pdf").exists()
    or not (DATA / "addendum-2.pdf").exists(),
    reason="build/ (run ingest.py) or addendum PDFs missing")


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    """Run the analysis into a scratch dir so the test never rewrites build/ outputs."""
    t = tmp_path_factory.mktemp("analyze")
    (t / "sheets.json").symlink_to((BUILD / "sheets.json").resolve())
    (t / "words").symlink_to((BUILD / "words").resolve())
    analyze_sheets.main(str(t), str(DATA))
    idx = json.loads((t / "sheet_index.json").read_text())
    return idx, json.loads((t / "addenda_changes.json").read_text())


def test_every_row_has_sheet_number(out):
    idx, _ = out
    assert len(idx["rows"]) == 76
    assert all(r["sheet_number"] for r in idx["rows"])
    assert idx["summary"]["unmatched"] == []


def test_titles_and_revisions_found(out):
    idx, _ = out
    s = idx["summary"]
    # 75/76: G000 (cover sheet) has no Sheet:/Scale: labels in its text layer, so it is listed, not guessed.
    assert s["titles_found"] >= 70
    assert s["unmatched_titles"] == ["dwg-p001"]
    assert s["unmatched_revisions"] == []
    rows = {r["sheet"]: r for r in idx["rows"]}
    assert rows["dwg-p013"]["title"] == "FIRST FLOOR PLAN"
    assert rows["dwg-p004"]["title"] == "PROJECT LOCATION & GENERAL NOTES"  # rotated civil sheet
    assert rows["dwg-p001"]["title"] is None
    assert rows["add1-p007"]["revision"] == "6/12/26 ADDENDUM #01"
    assert rows["add2-p007"]["revision_no"] == 2  # A1100 revised twice: latest wins
    assert [r["no"] for r in rows["add2-p007"]["revisions"]] == [1, 2]
    assert rows["dwg-p013"]["revision"] == "6/02/2026" and rows["dwg-p013"]["revision_no"] == 0
    # stray note text near M501's revision area must not become a revision row
    assert rows["dwg-p042"]["revisions"] == []


def test_narrative_matches_bound_sheets(out):
    idx, _ = out
    cc = idx["summary"]["addenda_crosscheck"]
    for src in ("add1", "add2"):
        assert cc[src]["bound_not_listed"] == [] and cc[src]["listed_not_bound"] == []
        assert cc[src]["narrative_listed"] == cc[src]["bound"]
    assert cc["add2"]["narrative_listed"] == 5 and cc["add2"]["cover_claim_matches_listed"]
    # the one explained discrepancy: Addendum 1 cover says 16, narrative lists and drawings bind 17
    assert cc["add1"]["cover_claim_revised_sheets"] == 16
    assert cc["add1"]["narrative_listed"] == 17 and not cc["add1"]["cover_claim_matches_listed"]


def test_a201_current_is_from_add1(out):
    idx, _ = out
    a201 = [r for r in idx["rows"] if r["sheet_number"] == "A201"]
    assert {r["source"] for r in a201} == {"dwg", "add1"}
    assert [r["source"] for r in a201 if r["current"]] == ["add1"]


def test_revise_bullets_map_to_sheet(out):
    _, changes = out
    a201 = next(c for c in changes if c["source"] == "add1" and c["sheet_number"] == "A201")
    assert a201["title"] == "FIRST FLOOR PLAN"
    assert [c["action"] for c in a201["changes"]] == ["REVISE", "ADD", "ADD"]
    assert a201["changes"][0]["text"] == "Alternate designation for cubbies in care rooms."
    # A800's bullets straddle a page break (pages 3-4); the page number "4" must not leak into the text
    a800 = next(c for c in changes if c["source"] == "add1" and c["sheet_number"] == "A800")
    assert [c["text"] for c in a800["changes"]] == [
        "Fixture heights at details 10, 11, 12, 13, 14, 15, & 17.",
        "Fixture height Interior Elevation Notes."]
    pl2 = next(c for c in changes if c["source"] == "add2" and c["sheet_number"] == "A1100")
    assert pl2["changes"] == [{"action": "REMOVE", "text": "PL-2 from project."}]


def test_three_issue_sheets_supersede_to_the_latest(out):
    idx, _ = out
    rows = {r["sheet"]: r for r in idx["rows"]}
    for latest, older in (("add2-p007", ("add1-p016", "dwg-p025")),   # A1100
                          ("add2-p009", ("add1-p020", "dwg-p046"))):  # ED200
        assert rows[latest]["current"] is True and rows[latest]["superseded_by"] is None
        for s in older:
            assert rows[s]["current"] is False and rows[s]["superseded_by"] == latest
    assert idx["summary"]["revised_twice"] == ["A1100", "ED200"]


def test_superseded_is_sorted_unique(out):
    sup = out[0]["summary"]["superseded"]
    assert sup == sorted(set(sup)) and len(sup) == 20


def test_removed_variant_and_change_counts(out):
    _, changes = out
    by = {(c["source"], c["sheet_number"]): c["changes"] for c in changes}
    # "REMOVED Conduit ..." (no colon, past tense) normalises to REMOVE
    assert by[("add1", "E200")][-1] == {"action": "REMOVE", "text": "Conduit in Shipping and Receiving"}
    assert [c["action"] for c in by[("add1", "E000")]] == ["REMOVE", "REVISE"]  # "REVISED: SYMBOLS"
    # counted by hand from the narratives: 40 bullets in Addendum 1, 5 in Addendum 2
    assert sum(len(cs) for (src, _), cs in by.items() if src == "add1") == 40
    assert sum(len(cs) for (src, _), cs in by.items() if src == "add2") == 5
    assert len(by[("add1", "E200")]) == 11 and len(by[("add1", "A201")]) == 3
    assert len(by[("add1", "P101")]) == 3 and len(by[("add1", "G002")]) == 1


def test_no_unparsed_narrative_lines(out):
    cc = out[0]["summary"]["addenda_crosscheck"]
    assert cc["add1"]["unparsed"] == [] and cc["add2"]["unparsed"] == []


def test_title_mismatches_between_narrative_and_title_block(out):
    cc = out[0]["summary"]["addenda_crosscheck"]
    assert cc["add2"]["title_mismatches"] == []
    assert cc["add1"]["title_mismatches"] == [{
        "sheet_number": "E400", "narrative": "ELECTRICL SCHEDULES AND DETAILS",  # typo in the narrative
        "title_block": "ELECTRICAL SCHEDULES AND DETAILS"}]


@pytest.mark.parametrize("sheet,title", [
    ("dwg-p002", "TYPICAL MOUNTING HEIGHTS & ADA GUIDELINES"),
    ("dwg-p004", "PROJECT LOCATION & GENERAL NOTES"),   # rotated civil sheet
    ("dwg-p007", "EROSION CONTROL PLAN"),
    ("dwg-p020", "ENLARGED FLOOR PLANS & INTERIOR ELEVATIONS"),
    ("dwg-p027", "STRUCTURAL PLANS AT REMODELING AND NEW SHELTER"),  # three lines
    ("dwg-p042", "MECHANICAL DETAILS"),
    ("dwg-p043", "MECHANICAL-ELECTRICAL SCHEDULES"),
    ("add1-p015", "WINDOW TYPES, BORROWED LITE TYPES"),
    ("add2-p010", "FIRST FLOOR - LIGHTING PLAN"),
])
def test_titles(out, sheet, title):
    assert {r["sheet"]: r["title"] for r in out[0]["rows"]}[sheet] == title
