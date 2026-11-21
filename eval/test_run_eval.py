import json

import pytest

from eval.run_eval import frozen_ok, kappa, summarise, wilson


def test_wilson_known_values():
    assert wilson(15, 20) == [0.531, 0.888]
    assert wilson(18, 20) == [0.699, 0.972]
    assert wilson(0, 20)[0] == 0.0 and wilson(0, 0) == [0.0, 0.0]


def test_kappa_perfect_and_chance():
    assert kappa([("a", "a"), ("b", "b")]) == 1.0
    assert kappa([("a", "b"), ("b", "a")]) == -1.0


def test_summary_counts_errors_as_wrong_and_precision():
    rows = [
        {"id": 1, "expected": "conflict", "verdict": "conflict", "correct": True, "judges": {"a": "conflict", "b": "conflict"}, "latency_s": 5, "cost_usd": 0.02},
        {"id": 2, "expected": "consistent", "verdict": "conflict", "correct": False, "judges": {"a": "conflict", "b": "conflict"}, "latency_s": 7, "cost_usd": 0.02},
        {"id": 3, "expected": "unclear", "verdict": "unclear", "correct": True, "judges": {"a": "unclear", "b": "consistent"}, "latency_s": 9, "cost_usd": 0.04},
        {"id": 4, "expected": "conflict", "error": "APIError: boom", "latency_s": 1},
    ]
    m = summarise(rows, "t", "x.json")
    assert m["n"] == 4 and m["correct"] == 2 and m["errors"] == 1 and m["accuracy"] == 0.5
    assert m["conflict_precision"] == 0.5 and m["false_alarms"] == 1 and m["conflict_recall"] == 0.5
    assert m["coverage"] == round(2 / 4, 3) and m["latency_p50_s"] == 7 and m["cost_per_card_usd"] == round(0.08 / 3, 4)
    assert m["by_label"] == {"conflict": "1/2", "consistent": "0/1", "unclear": "1/1"}


def test_frozen_file_guard(tmp_path):
    f = tmp_path / "set.json"
    f.write_text(json.dumps([{"id": 1}]))
    import hashlib
    (tmp_path / "set.sha256").write_text(hashlib.sha256(f.read_bytes()).hexdigest() + "  set.json\n")
    assert frozen_ok(f)[0]
    f.write_text(json.dumps([{"id": 2}]))
    assert not frozen_ok(f)[0]


def test_real_heldout_is_still_frozen():
    ok, why = frozen_ok("eval/heldout_v1.json")
    assert ok, why
