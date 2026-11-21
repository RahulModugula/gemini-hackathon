"""Room geometry (rooms.py) and the spatial check (verify.spatial_check). No network; llm.call is faked. The real-sheet
tests use the current P101 plumbing plan (add1-p018) from the shared build/ and the bid-set PDFs."""
import json

import numpy as np
import pymupdf
import pytest

import check
import llm
import rooms
import verify
from roles import ROLES

SHEET = "add1-p018"
CLAIM = "All the water closets in Toilet 105A are type WC-2."


@pytest.fixture(scope="module")
def geo():
    return rooms.sheet_rooms(SHEET)


@pytest.fixture(scope="module")
def words():
    return json.loads((check.BUILD / "words" / f"{SHEET}.json").read_text())["words"]


def _box(words, texts, x, y):
    """pt box tightly around the words in `texts` inside the window x=(lo, hi), y=(lo, hi)."""
    bbs = [w["bbox"] for w in words if w["text"] in texts and x[0] < w["bbox"][0] < x[1] and y[0] < w["bbox"][1] < y[1]]
    assert bbs
    return [min(b[0] for b in bbs), min(b[1] for b in bbs), max(b[2] for b in bbs), max(b[3] for b in bbs)]


# ---- extraction on the real sheet ------------------------------------------------------------
def test_finds_labelled_rooms_on_p101(geo):
    nums = {r.number for r in geo["rooms"]}
    assert len(geo["rooms"]) >= 15
    assert {"105A", "109B", "109A", "108", "106", "107A"} <= nums
    assert {r.number: r.name for r in geo["rooms"]}["105A"] == "TOILET"
    assert {r.number: r.name for r in geo["rooms"]}["108"] == "UTILITY"


def test_toilet_105a_tag_counts_match_the_words(geo, words):
    """Ground truth from the text layer: the three closets stacked at x~1371 are WC-3, WC-3, WC-2."""
    stack = sorted((w["bbox"][1], w["text"]) for w in words if w["text"].startswith("WC-") and 1360 < w["bbox"][0] < 1380
                   and 870 < w["bbox"][1] < 970)
    assert [t for _, t in stack] == ["WC-3", "WC-3", "WC-2"]
    r = geo["by_number"]["105a"]
    assert r.tags.get("WC-3") == 2 and r.tags.get("WC-2") == 1
    assert "WC-1" not in r.tags and r.tags.get("L-2") == 3      # three lavatories in the alcove above the closets


def test_toilet_109b_tag_counts(geo):
    r = geo["by_number"]["109b"]
    assert r.tags == {"WC-1": 1, "L-2": 1, "FD-1": 1}
    # 109B is the upper toilet; 109A (its neighbour below) is a different region
    assert r.bbox[3] <= geo["by_number"]["109a"].bbox[1] + 8
    assert not r.contains_pt(*[(a + b) / 2 for a, b in zip(geo["by_number"]["109a"].label_bbox[:2],
                                                           geo["by_number"]["109a"].label_bbox[2:])])


def test_neighbouring_rooms_do_not_overlap_each_others_label(geo):
    for r in geo["rooms"]:
        cx, cy = (r.label_bbox[0] + r.label_bbox[2]) / 2, (r.label_bbox[1] + r.label_bbox[3]) / 2
        assert r.contains_pt(cx, cy), f"{r.number} does not contain its own tag"
        for o in geo["rooms"]:
            if o is not r:
                ox, oy = (o.label_bbox[0] + o.label_bbox[2]) / 2, (o.label_bbox[1] + o.label_bbox[3]) / 2
                assert not r.contains_pt(ox, oy), f"{r.number} swallowed the tag of {o.number}"


def test_cache_round_trip(geo):
    r = geo["by_number"]["105a"]
    back = rooms.Room.from_json(json.loads(json.dumps(r.to_json())))
    assert back.bbox == r.bbox and back.area == r.area and back.tags == r.tags and (back.mask == r.mask).all()


# ---- the spatial check ---------------------------------------------------------------------
@pytest.fixture(scope="module")
def world():
    sheets, src, rows, narr = check._load()
    cur = check.pick(CLAIM, sheets, src, rows)
    cur["rooms"] = rooms.sheet_rooms(SHEET)
    return {"cur": cur, "rows": rows, "sheets": sheets, "src": src}


def _judge(world, box_pt, verdict="consistent", quoted=("105A", "WC-2")):
    cur = world["cur"]
    return {"verdict": verdict, "confidence": 0.9, "reason": "r", "quoted_words": list(quoted),
            "evidence": [{"image": "current", "box_2d": verify.pt_to_box(box_pt, cur["rect"]), "label": "closets"}],
            "changed_by_addendum": False, "needs_code": ""}


def test_evidence_inside_the_named_room_passes(world, words):
    box = _box(words, {"105A", "WC-2", "WC-3"}, (1360, 1430), (860, 970))
    sp = verify.spatial_check(CLAIM, "conflict", {"current": world["cur"]}, [box])
    assert sp["pass"] is True and not sp.get("info")
    assert sp["room"]["number"] == "105A" and sp["tag_count"]["WC-3"] == 2 and sp["tag_count"]["WC-2"] == 1
    assert len(sp["room"]["room_box_2d"]) == 4 and all(0 <= v <= 1000 for v in sp["room"]["room_box_2d"])
    assert sp["tags_in_crop"]["WC-3"] == 2


def test_negative_control_box_in_the_neighbouring_room_fails(world, words):
    """The WC-2 callouts at x~1625-1680 are in Toilet 103A next door, not in 105A."""
    box = _box(words, {"WC-2"}, (1600, 1700), (850, 890))
    sp = verify.spatial_check(CLAIM, "consistent", {"current": world["cur"]}, [box])
    assert sp["pass"] is False and sp["evidence_outside"] == [0]
    assert "outside the room" in sp["detail"]


def test_unclear_verdict_is_never_failed_by_geometry(world, words):
    box = _box(words, {"WC-2"}, (1600, 1700), (850, 890))
    assert verify.spatial_check(CLAIM, "unclear", {"current": world["cur"]}, [box])["pass"] is True


def test_no_room_in_claim_means_no_check(world):
    assert verify.spatial_check("All closets are WC-2.", "conflict", {"current": world["cur"]}, [[0, 0, 1, 1]]) is None
    assert verify.spatial_check(CLAIM, "conflict", {"current": {k: v for k, v in world["cur"].items() if k != "rooms"}},
                                [[0, 0, 1, 1]]) is None


def test_no_geometry_is_info_only_never_a_failure(world, words):
    empty = {"sheet": SHEET, "rooms": [], "unresolved": ["105A"], "by_number": {}}
    cur = {**world["cur"], "rooms": empty}
    box = _box(words, {"WC-2"}, (1600, 1700), (850, 890))
    sp = verify.spatial_check(CLAIM, "conflict", {"current": cur}, [box])
    assert sp["info"] is True and sp["pass"] is True and sp["room"] is None and "no room geometry" in sp["detail"]
    # a room that is on the sheet but could not be resolved (104 leaks) behaves the same
    sp = verify.spatial_check("Room 104 has WC-2.", "conflict", {"current": world["cur"]}, [box])
    assert sp["info"] is True and sp["pass"] is True


def _run(monkeypatch, out, claim=CLAIM):
    monkeypatch.setattr(llm, "call", lambda model, parts, *, schema=None, purpose, thinking=None:
                        llm.Result(text=json.dumps(out), json=out))
    return check.check(claim)


def test_end_to_end_card_inside_room_keeps_verdict_and_carries_room(monkeypatch, world, words):
    out = _judge(world, _box(words, {"105A", "WC-2", "WC-3"}, (1360, 1430), (860, 970)), "conflict", ("105A", "WC-3"))
    card = _run(monkeypatch, out)
    sp = next(c for c in card["checks"] if c.get("check") == "spatial")
    assert sp["pass"] is True and card["verdict"] == "conflict" and sp["tag_count"]["WC-3"] == 2
    ev = card["evidence"][0]
    assert ev["room"]["number"] == "105A" and len(ev["room"]["room_box_2d"]) == 4
    json.dumps(card)                                    # the card stays JSON-serialisable


def test_end_to_end_evidence_in_neighbour_room_is_downgraded(monkeypatch, world, words):
    """Grounding passes (WC-1 is a real word in the box), but the box is Toilet 106's closet, not 105A's."""
    out = _judge(world, _box(words, {"WC-1"}, (1227, 1290), (900, 974)), "consistent", ("WC-1",))
    card = _run(monkeypatch, out)
    assert next(c for c in card["checks"] if c.get("check") == "grounding")["pass"] is True
    sp = next(c for c in card["checks"] if c.get("check") == "spatial")
    assert sp["pass"] is False
    assert card["verdict"] == "unclear" and "outside the room" in card["reason"]


# ---- synthetic drawing: the algorithm itself ---------------------------------------------------
def _page(walls, size=(400, 300)):
    doc = pymupdf.open()
    p = doc.new_page(width=size[0], height=size[1])
    sh = p.new_shape()
    for a, b in walls:
        sh.draw_line(a, b)
        sh.finish(color=(0.56, 0.56, 0.56), width=1.2)
    sh.commit()
    return p


def _tag(text, x, y):
    return {"text": text, "bbox": [x, y, x + 16, y + 8]}


BOX = [((100, 100), (300, 100)), ((300, 100), (300, 200)), ((300, 200), (100, 200)), ((100, 200), (100, 100))]


def test_synthetic_closed_room_is_found_and_has_the_right_size():
    rs, unres = rooms.extract(_page(BOX), [_tag("101", 190, 145)])
    assert not unres and len(rs) == 1 and abs(rs[0].area - 200 * 100) < 0.1 * 200 * 100
    assert rs[0].contains([150, 130, 160, 140]) and not rs[0].contains([50, 130, 60, 140], margin=2)


def test_synthetic_door_gap_is_bridged_but_a_huge_opening_leaks():
    door = [w for w in BOX if w != BOX[3]] + [((100, 100), (100, 135)), ((100, 170), (100, 200))]   # 35 pt opening
    rs, _ = rooms.extract(_page(door), [_tag("101", 190, 145)])
    assert len(rs) == 1 and rs[0].area < 22000
    wide = [w for w in BOX if w != BOX[3]] + [((100, 100), (100, 110)), ((100, 190), (100, 200))]   # 80 pt opening
    rs, unres = rooms.extract(_page(wide), [_tag("101", 190, 145)])
    assert rs == [] and unres == ["101"]                  # not guessed: the fill escapes into the page margin


def test_synthetic_sheet_without_grey_walls_has_no_rooms():
    doc = pymupdf.open()
    p = doc.new_page(width=400, height=300)
    sh = p.new_shape()
    for a, b in BOX:
        sh.draw_line(a, b)
        sh.finish(color=(0, 0, 0), width=3.6)               # heavy BLACK strokes are fire-rating lines, not walls
    sh.commit()
    assert rooms.extract(p, [_tag("101", 190, 145)]) == ([], ["101"])


def test_synthetic_stall_pocket_belongs_to_its_room():
    """A 20 pt deep stall open to the room is narrower than the closing kernel; it must still be counted."""
    walls = BOX + [((100, 150), (140, 150)), ((100, 170), (140, 170))]
    rs, _ = rooms.extract(_page(walls), [_tag("101", 220, 145)])
    assert rs[0].contains_pt(120, 160) and rs[0].contains_pt(220, 150)
