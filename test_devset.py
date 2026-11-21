"""Dev claim set checks. The label tests parse each claim's TEXT (sheet, room, value) and check it against the
words files; they do not re-run the generator. Needs build/ (symlink to the main checkout's build dir)."""
import json
import re
from collections import Counter
from pathlib import Path

import pytest

from studio import devset as D

HERE = Path(__file__).parent
CLAIMS = json.loads((HERE / "studio/devset/claims_dev.json").read_text())
pytestmark = pytest.mark.skipif(not (HERE / "build/sheet_index.json").exists(),
                                reason="build/ missing (run ingest or symlink it)")
FIELDS = {"id", "claim", "expected", "sheet_number", "current_sheet", "superseded_sheet", "anchor", "bbox_pt",
          "evidence_tokens", "provenance"}
RADIUS = 125.0  # the value must be this close to its room tag (same as devset.ATTRIB_PT, restated on purpose)

LOC = r"(?:room|Toilet) (?P<a>\w+)"
PATTERNS = [
    re.compile(rf"^On (?P<s>\w+), the water closets in {LOC} are tagged (?P<v>\S+)\.$"),
    re.compile(rf"^On (?P<s>\w+), {LOC} references detail (?P<v>\S+)\.$"),
    re.compile(rf"^On (?P<s>\w+), the (?:tag|callout) at {LOC} reads (?P<v>.+)\.$"),
    re.compile(rf"^On (?P<s>\w+), the list entry for {LOC} is (?P<v>.+)\.$"),
]
NAME = re.compile(r"^On (?P<s>\w+), room (?P<a>\w+) is (?P<name>[A-Z][A-Z /.&-]+)\.$")
SHEET_NO = {r["sheet"]: r["sheet_number"] for r in json.loads((HERE / "build/sheet_index.json").read_text())["rows"]}


def parse(claim):
    """-> dict(s, a, v | name). Raises AssertionError if the sentence is not one of the generator's value/name forms."""
    for p in PATTERNS + [NAME]:
        m = p.match(claim)
        if m:
            return m.groupdict()
    raise AssertionError(f"unparseable claim: {claim}")


def near(words, pt, text, r=RADIUS):
    return sum(1 for w in words if w["text"] == text and ((D.center(w)[0] - pt[0]) ** 2 + (D.center(w)[1] - pt[1]) ** 2) ** 0.5 <= r)


def anchor_point(words, a, bbox):
    hits = [D.center(w) for w in words if w["text"] == a and bbox[0] <= D.center(w)[0] <= bbox[2] and bbox[1] <= D.center(w)[1] <= bbox[3]]
    return hits[0] if hits else None


def window_texts(words, bbox):
    return {w["text"] for w in words if bbox[0] <= D.center(w)[0] <= bbox[2] and bbox[1] <= D.center(w)[1] <= bbox[3]}


def check_claim(c):
    """Verify one non-unclear claim's label from its text and the words files alone."""
    p = parse(c["claim"])
    assert SHEET_NO[c["current_sheet"]] == p["s"] == c["sheet_number"], (c["id"], "sheet named in the claim")
    cur, old = D.load_words(c["current_sheet"]), D.load_words(c["superseded_sheet"])
    off = c["offset_pt"]
    pt = anchor_point(cur, p["a"], c["bbox_pt"])
    assert pt, (c["id"], "anchor room tag not inside the crop window", p["a"])
    pt_old = (pt[0] - off[0], pt[1] - off[1])
    old_box = [c["bbox_pt"][0] - off[0], c["bbox_pt"][1] - off[1], c["bbox_pt"][2] - off[0], c["bbox_pt"][3] - off[1]]
    if "name" in p:      # "room 124 is PROGRAM AIDS": the name sits just above the tag on the current sheet
        words = p["name"].split()
        above = lambda ws, q: {w["text"] for w in ws if 0 < q[1] - D.center(w)[1] <= 60 and abs(D.center(w)[0] - q[0]) <= 90}  # noqa: E731
        if c["expected"] == "consistent":
            assert set(words) <= above(cur, pt), (c["id"], words)
        else:
            assert not set(words) & (window_texts(cur, c["bbox_pt"]) | window_texts(old, old_box)), (c["id"], "decoy name is visible")
            assert set(c["evidence_tokens"][:-1]) <= above(cur, pt), (c["id"], "real name must sit above the tag")
        return
    v = p["v"]
    if "list entry for" in c["claim"]:   # a list/schedule line: the value must sit on the room tag's own text row
        on_row = lambda ws, q: sum(1 for w in ws if w["text"] == v and abs(D.center(w)[1] - q[1]) <= 3 and abs(D.center(w)[0] - q[0]) <= 150)  # noqa: E731
        if c["expected"] == "consistent":
            assert on_row(cur, pt) == 1 and on_row(old, pt_old) == 0, (c["id"], "value not on the tag's row")
        else:
            assert on_row(old, pt_old) == 1 and on_row(cur, pt) == 0, (c["id"], "old value not on the tag's row")
        return
    n_cur, n_old = near(cur, pt, v), near(old, pt_old, v)
    if c["expected"] == "consistent":
        assert n_cur >= 1 and n_cur > n_old, (c["id"], v, n_cur, n_old)
    else:
        if n_old >= 1:   # stale value: was at the spot on the superseded sheet, dropped on the current one
            assert n_cur < n_old, (c["id"], v, n_cur, n_old)
        else:            # nothing was there: the asserted value must be visible nowhere in the crop, on either sheet
            assert v not in window_texts(cur, c["bbox_pt"]) | window_texts(old, old_box), (c["id"], v)
            assert near(cur, pt, c["evidence_tokens"][0]) >= 1, (c["id"], "real value not at the anchor")


def test_schema_and_unique_ids():
    assert all(FIELDS <= set(c) for c in CLAIMS)
    assert len({c["id"] for c in CLAIMS}) == len(CLAIMS)
    assert all(c["expected"] in {"conflict", "consistent", "unclear"} for c in CLAIMS)
    assert all(c["provenance"] and c["claim"] for c in CLAIMS)


def test_wording_names_the_sheet_and_has_no_negations_or_counts():
    bad = re.compile(r"\b(no|not|none|never|without|times|twice|once|two|three|four|both|all)\b|\d+x\b", re.I)
    for c in CLAIMS:
        assert c["claim"].startswith(f"On {c['sheet_number']}, "), c["claim"]
        assert not bad.search(c["claim"]), c["claim"]
        assert len(c["evidence_tokens"]) <= 3 or c["kind"] != "unclear"


def test_p101_toilet_105a_wc2_to_wc3_pair():
    stale = [c for c in CLAIMS if c["claim"] == "On P101, the water closets in Toilet 105A are tagged WC-2."]
    cur = [c for c in CLAIMS if c["claim"] == "On P101, the water closets in Toilet 105A are tagged WC-3."]
    assert len(stale) == 1 and stale[0]["expected"] == "conflict"
    assert len(cur) == 1 and cur[0]["expected"] == "consistent"


def test_every_value_claim_label_holds_against_the_words():
    checked = [c for c in CLAIMS if c["expected"] != "unclear"]
    assert len(checked) >= 20
    for c in checked:
        check_claim(c)


def test_evidence_tokens_exist_inside_bbox_on_the_current_sheet():
    for c in CLAIMS:
        have = D.in_window(D.load_words(c["current_sheet"]), c["bbox_pt"]) if c["evidence_tokens"] else []
        texts = Counter(w["text"] for w in have)
        for tok in c["evidence_tokens"]:
            if c["kind"] == "stale":     # the stale (old) value lives on the superseded sheet
                off = c["offset_pt"]
                box = [c["bbox_pt"][0] - off[0], c["bbox_pt"][1] - off[1], c["bbox_pt"][2] - off[0], c["bbox_pt"][3] - off[1]]
                assert tok in window_texts(D.load_words(c["superseded_sheet"]), box), (c["id"], tok)
                assert texts[tok] < Counter(w["text"] for w in D.in_window(D.load_words(c["superseded_sheet"]), box))[tok]
            else:
                assert texts[tok] >= 1, (c["id"], tok)


def test_unclear_claims_name_sheet_and_location_and_ask_for_something_not_on_it():
    unclear = [c for c in CLAIMS if c["expected"] == "unclear"]
    assert len(unclear) == 5
    assert len({re.sub(r"On \w+, ", "", c["claim"]).split(" is ")[0].split(" at ")[0] for c in unclear}) >= 3   # varied wording
    for c in unclear:
        m = re.search(rf"On (?P<s>\w+), .*?{LOC}", c["claim"])
        assert m and m["s"] == c["sheet_number"] == SHEET_NO[c["current_sheet"]], c["claim"]
        cur = D.load_words(c["current_sheet"])
        assert anchor_point(cur, m["a"], c["bbox_pt"]), (c["id"], "location outside the crop")
        text = " ".join(w["text"] for s in (c["current_sheet"], c["superseded_sheet"]) for w in D.load_words(s)).lower()
        assert all(t.lower() not in text for t in c["absent_tokens"]), c["id"]


def test_labels_balanced_and_spread(capsys):
    lab = Counter(c["expected"] for c in CLAIMS)
    sheets = Counter(c["sheet_number"] for c in CLAIMS)
    with capsys.disabled():
        print(f"\nlabels: {dict(lab)}  sheets: {len(sheets)} {dict(sheets)}")
    assert 25 <= len(CLAIMS) <= 50
    assert lab["conflict"] == lab["consistent"] and lab["unclear"] == 5
    assert len(sheets) >= 6 and max(sheets.values()) <= 12


def test_committed_file_matches_regeneration():
    claims, _ = D.build()
    keys = ("id", "claim", "expected", "bbox_pt", "anchor", "evidence_tokens")
    assert [{k: c[k] for k in keys} for c in claims] == [{k: c[k] for k in keys} for c in CLAIMS]


def test_offset_estimate_recovers_known_shift():
    old = [{"text": f"TOK{i}", "bbox": [10.0 * i, 5.0 * i, 10.0 * i + 8, 5.0 * i + 6]} for i in range(20)]
    new = [{"text": w["text"], "bbox": [w["bbox"][0] + 3, w["bbox"][1] - 2, w["bbox"][2] + 3, w["bbox"][3] - 2]} for w in old]
    assert D.estimate_offset(old, new) == (3.0, -2.0)
    new[4]["text"] = "CHANGED"
    removed, added = D.align(old, new, (3.0, -2.0))
    assert [w["text"] for w in removed] == ["TOK4"] and [w["text"] for w in added] == ["CHANGED"]
