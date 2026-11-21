from analyze_sheets import parse_narrative_text

COVER = "This Addendum consists of 5 pages and 2 revised sheets"


def test_inline_bullet_parses():
    page = "Drawings:\nA201 - FIRST FLOOR PLAN\n• REVISE: x\n•\nADD: y\nEnd of Addendum 01"
    r = parse_narrative_text(COVER, [page])
    assert r["sheets"] == [{"sheet_number": "A201", "title": "FIRST FLOOR PLAN",
                            "changes": [{"action": "REVISE", "text": "x"}, {"action": "ADD", "text": "y"}]}]
    assert r["unparsed"] == [] and r["cover_claim"] == 2


def test_stray_line_is_reported_not_dropped():
    r = parse_narrative_text(COVER, ["Drawings:\nArchitectural:\nWHO KNOWS\nA201 - PLAN\nREVISE: x\n2",
                                     "3\nEnd of Addendum"])
    assert r["unparsed"] == ["WHO KNOWS"]  # "2" and "3" are the current page numbers, dropped
    assert r["sheets"][0]["changes"] == [{"action": "REVISE", "text": "x"}]


def test_page_number_only_matches_current_page():
    r = parse_narrative_text(COVER, ["Drawings:\nA201 - PLAN", "3\n7\nEnd of Addendum"])
    assert r["unparsed"] == ["7"]  # on page 3 only the line "3" is a page number
