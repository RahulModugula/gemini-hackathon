"""Guards for the labelled claim sets: eval/dev_v3.json (dev) and eval/heldout_v2.json (frozen), plus the frozen heldout_v1.

Structure checks need no build/ dir; the sheet-bound and word checks need build/ (symlink to the main checkout's build dir) and skip without it.
"""
import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "eval"
BUILD = ROOT / "build"
HELDOUT_V1_SHA = "a200c8b4db2702e22704f8d829c862d684c370e0a48fc196806adce9759eaa39"
LABELS = {"conflict", "consistent", "unclear"}
REQUIRED = {"id", "claim", "expected", "sheet_number", "current_sheet", "evidence_tokens", "bbox_pt", "provenance"}

DEV = json.loads((EVAL / "dev_v3.json").read_text())
HO2 = json.loads((EVAL / "heldout_v2.json").read_text())
HO1 = json.loads((EVAL / "heldout_v1.json").read_text())
needs_build = pytest.mark.skipif(not (BUILD / "sheet_index.json").exists(), reason="build/ missing (symlink it)")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.mark.parametrize("name,claims", [("dev_v3", DEV), ("heldout_v2", HO2)])
def test_schema_and_labels(name, claims):
    assert isinstance(claims, list) and claims
    for c in claims:
        assert REQUIRED <= set(c), (c.get("id"), REQUIRED - set(c))
        assert c["expected"] in LABELS, c["id"]
        assert isinstance(c["claim"], str) and c["claim"].strip(), c["id"]
        assert isinstance(c["sheet_number"], str) and isinstance(c["current_sheet"], str), c["id"]
        assert isinstance(c["evidence_tokens"], list) and isinstance(c["provenance"], str) and c["provenance"], c["id"]
        if c["expected"] != "unclear":
            assert c["evidence_tokens"], f"{c['id']}: consistent/conflict needs evidence tokens"
        b = c["bbox_pt"]
        assert len(b) == 4 and b[0] < b[2] and b[1] < b[3], c["id"]


def test_ids_and_claims_unique_across_sets():
    ids = [c["id"] for c in DEV] + [c["id"] for c in HO2] + [c["id"] for c in HO1]
    assert len(ids) == len(set(ids)), [i for i, n in Counter(ids).items() if n > 1]
    texts = [c["claim"].strip().lower() for c in DEV + HO2 + HO1]
    assert len(texts) == len(set(texts)), "duplicate claim text across or within sets"


def test_heldout_v1_frozen():
    assert sha(EVAL / "heldout_v1.json") == HELDOUT_V1_SHA
    assert (EVAL / "heldout_v1.sha256").read_text().split()[0] == HELDOUT_V1_SHA


def test_heldout_v2_frozen_and_documented():
    got = sha(EVAL / "heldout_v2.json")
    assert (EVAL / "heldout_v2.sha256").read_text().split()[0] == got, "heldout_v2.json changed after the freeze"
    doc = (EVAL / "HELDOUT.md").read_text()
    assert got in doc and HELDOUT_V1_SHA in doc


def test_run_eval_accepts_the_files():
    from eval import run_eval
    for p in (EVAL / "dev_v3.json", EVAL / "heldout_v2.json"):
        ok, _ = run_eval.frozen_ok(p)
        assert ok
        for c in json.loads(p.read_text()):
            assert {"id", "claim", "expected", "sheet_number"} <= set(c)


def test_label_balance_and_coverage():
    for claims in (DEV, HO2):
        n = Counter(c["expected"] for c in claims)
        assert set(n) == LABELS
        assert min(n.values()) / len(claims) >= 0.2, n
    assert len(DEV) >= 90 and len(HO2) >= 35


@needs_build
def test_every_changed_sheet_is_covered_in_dev():
    idx = json.loads((BUILD / "sheet_index.json").read_text())
    changed = {r["sheet_number"] for r in idx["rows"] if not r["current"]}
    covered = {c["sheet_number"] for c in DEV}
    assert changed <= covered, changed - covered
    unchanged = [c for c in DEV if not c.get("superseded_sheet")]
    assert len(unchanged) >= 15, len(unchanged)


@needs_build
def test_sheets_exist_bboxes_in_bounds_and_evidence_on_sheet():
    idx = {r["sheet"]: r for r in json.loads((BUILD / "sheet_index.json").read_text())["rows"]}
    for c in DEV + HO2:
        row = idx[c["current_sheet"]]
        assert row["current"], f"{c['id']}: {c['current_sheet']} is not a current sheet"
        assert row["sheet_number"] == c["sheet_number"], c["id"]
        sup = c.get("superseded_sheet")
        if sup:
            assert sup in idx, c["id"]
        info = json.loads((BUILD / f"words/{c['current_sheet']}.json").read_text())
        b = c["bbox_pt"]
        assert 0 <= b[0] < b[2] <= info["width"] + 0.5 and 0 <= b[1] < b[3] <= info["height"] + 0.5, f"{c['id']}: bbox out of sheet bounds {b}"
        words = info["words"]
        if c["expected"] == "unclear":
            continue
        if c.get("kind") == "stale" and c["id"][4:6].isdigit():
            continue   # generator stale claims: evidence token is the OLD value, absent from the current window by design
        inside = {w["text"] for w in words if b[0] <= (w["bbox"][0] + w["bbox"][2]) / 2 <= b[2] and b[1] <= (w["bbox"][1] + w["bbox"][3]) / 2 <= b[3]}
        missing = [t for t in c["evidence_tokens"] if t not in inside]
        assert not missing, f"{c['id']}: evidence tokens not in bbox on {c['current_sheet']}: {missing}"


@needs_build
def test_unclear_tokens_absent_from_every_current_sheet():
    idx = json.loads((BUILD / "sheet_index.json").read_text())
    text = " ".join(w["text"].lower() for r in idx["rows"] if r["current"]
                    for w in json.loads((BUILD / f"words/{r['sheet']}.json").read_text())["words"])
    for c in DEV + HO2:
        if c["expected"] == "unclear" and not c["id"].startswith("dev_u"):   # dev_uNN are the generator's, checked per sheet there
            for t in c["absent_tokens"]:
                assert t.lower() not in text, f"{c['id']}: unclear token {t!r} is on a current sheet"
