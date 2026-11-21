"""/api/samples, replay (demo) mode, replay-miss marking, caching headers and gzip (feat/ui-v3)."""
import gzip
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as appmod
import llm

HERE = Path(__file__).parent
FIXTURES = json.loads((HERE / "ui" / "cards.json").read_text())
CARD = {"claim": "x", "speaker": None, "timestamp": "2026-11-21T00:00:00Z", "verdict": "conflict", "confidence": 0.9,
        "evidence": [], "spec_refs": [], "checks": [], "reason": "r", "rfi_draft": None}
MISS = {"by": "gemini-4-argon", "verdict": "unclear", "confidence": 0.0,
        "note": "gemini-4-argon failed: CacheMiss: LLM_CACHE=replay: no entry for abc (purpose 'judge')"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "BUILD", tmp_path)
    calls = []

    def fake_check(claim, progress=None):
        calls.append(claim)
        return json.loads(json.dumps(CARD)) | {"claim": claim}

    monkeypatch.setattr(appmod.checker, "check", fake_check)
    monkeypatch.setattr(llm, "run_spend", lambda rid=None: 0.0)
    monkeypatch.delenv("LLM_CACHE", raising=False)
    return TestClient(appmod.app, raise_server_exceptions=False), tmp_path, calls


def _sse(text):
    out = []
    for block in text.strip().split("\n\n"):
        ev, data = block.split("\n", 1)
        out.append((ev.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return out


# ---- samples ---------------------------------------------------------------------------------------------
def test_samples_shape_and_recorded_card_ids(env):
    c, _, _ = env
    s = c.get("/api/samples").json()
    assert len(s) >= 4 and len({x["id"] for x in s}) == len(s)
    for x in s:
        assert set(x) == {"id", "label", "kind", "claim", "why", "card_id"}
        assert x["kind"] in ("claim", "question") and 0 < len(x["claim"]) <= appmod.MAX_CLAIM
    by_claim = {f["claim"]: f["id"] for f in FIXTURES}
    hit = [x for x in s if x["claim"] in by_claim]
    assert hit and all(x["card_id"] == by_claim[x["claim"]] for x in hit)      # recorded fixture is linked
    assert any(x["kind"] == "question" for x in s)                              # the RFI-blocked moment is offered


def test_samples_drop_malformed_entries(env, tmp_path, monkeypatch):
    c, _, _ = env
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "samples.json").write_text(json.dumps([
        {"id": "ok", "claim": " Fine claim ", "kind": "weird"}, {"id": 3, "claim": "bad id"}, {"id": "e", "claim": ""},
        {"id": "long", "claim": "a" * 501}, "nope", {"id": "nc"}]))
    monkeypatch.setattr(appmod, "UI", ui)
    assert c.get("/api/samples").json() == [{"id": "ok", "label": "Fine claim", "claim": "Fine claim", "kind": "claim",
                                             "why": "", "card_id": None}]
    (ui / "samples.json").write_text("{not json")
    assert c.get("/api/samples").json() == []


# ---- replay / demo mode ----------------------------------------------------------------------------------
def test_replay_serves_recorded_card_instantly_without_checking(env, monkeypatch):
    c, tmp, calls = env
    monkeypatch.setenv("LLM_CACHE", "replay")
    fx = FIXTURES[0]
    r = c.post("/api/check", json={"claim": "  " + fx["claim"].upper() + " "}).json()
    assert r["id"] == fx["id"] and r["replayed"] is True and r["verdict"] == fx["verdict"]
    assert not calls and not (tmp / "cards.jsonl").exists()                    # no model, no ledger growth


def test_replay_stream_has_live_event_shapes_flagged_replay(env, monkeypatch):
    c, _, calls = env
    monkeypatch.setenv("LLM_CACHE", "replay")
    fx = FIXTURES[0]
    r = c.get("/api/check/stream", params={"claim": fx["claim"]})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    ev = _sse(r.text)
    assert [k for k, _ in ev] == ["step"] * 13 + ["card"]                       # same count as a live run
    assert all(d["replay"] is True and d["t"] == 0.0 for k, d in ev if k == "step")
    assert ev[-1][1]["id"] == fx["id"] and not calls


def test_no_instant_replay_outside_replay_mode(env, monkeypatch):
    c, _, calls = env
    fx = FIXTURES[0]
    for mode in (None, "readwrite", "off"):
        if mode:
            monkeypatch.setenv("LLM_CACHE", mode)
        r = c.post("/api/check", json={"claim": fx["claim"]}).json()
        assert "replayed" not in r and r["id"] != fx["id"]
    assert calls == [fx["claim"]] * 3


def test_replay_unrecorded_claim_runs_the_checker(env, monkeypatch):
    c, _, calls = env
    monkeypatch.setenv("LLM_CACHE", "replay")
    r = c.post("/api/check", json={"claim": "never recorded"}).json()
    assert calls == ["never recorded"] and "replayed" not in r


def test_cache_miss_card_is_marked_and_never_replayed(env, monkeypatch):
    c, tmp, calls = env
    monkeypatch.setenv("LLM_CACHE", "replay")
    monkeypatch.setattr(appmod.checker, "check",
                        lambda claim, progress=None: calls.append(claim) or (CARD | {"claim": claim, "verdict": "unclear",
                                                                                      "checks": [MISS]}))
    r1 = c.post("/api/check", json={"claim": "miss me"}).json()
    assert r1["replay_miss"] is True
    r2 = c.post("/api/check", json={"claim": "miss me"}).json()                # the miss card is not "recorded"
    assert calls == ["miss me", "miss me"] and "replayed" not in r2


def test_real_unclear_is_not_a_replay_miss(env):
    real = {"by": "gemini-4-argon", "verdict": "unclear", "note": "The crop does not show room 107."}
    code = {"by": "code", "check": "grounding", "pass": False, "note": "CacheMiss in a code note is ignored"}
    assert not appmod._replay_miss(CARD | {"checks": [real, code]})
    assert appmod._replay_miss(CARD | {"checks": [real, MISS]})


# ---- caching headers + gzip ------------------------------------------------------------------------------
def test_cache_headers(env):
    c, tmp, _ = env
    (tmp / "crops").mkdir()
    (tmp / "crops" / "0123456789abcdef.png").write_bytes(b"png")
    assert "immutable" in c.get("/crops/0123456789abcdef.png").headers["cache-control"]
    fx = next((HERE / "ui" / "crops").iterdir()).name                        # fixture names are not content-addressed
    assert c.get(f"/crops/{fx}").headers["cache-control"] == "public, max-age=300"
    (tmp / "frames").mkdir()
    (tmp / "frames" / ("a" * 20 + ".jpg")).write_bytes(b"j")
    (tmp / "frames" / ("a" * 20 + "_crop.jpg")).write_bytes(b"j")
    assert "immutable" in c.get("/frames/" + "a" * 20 + ".jpg").headers["cache-control"]
    assert "immutable" not in c.get("/frames/" + "a" * 20 + "_crop.jpg").headers["cache-control"]
    assert c.get("/").headers["cache-control"] == "no-cache"
    assert "cache-control" not in c.get("/crops/missing.png").headers or c.get("/crops/missing.png").status_code == 404


def test_gzip_on_page_but_not_on_sse(env):
    c, _, _ = env
    r = c.get("/", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip" and "Drawing Claim Checker" in r.text
    raw = c.get("/", headers={"Accept-Encoding": "identity"})
    assert raw.headers.get("content-encoding") is None
    assert gzip.compress(raw.content)                                          # sanity: body is compressible
    s = c.get("/api/check/stream", params={"claim": "q" * 50}, headers={"Accept-Encoding": "gzip"})
    assert s.headers.get("content-encoding") is None and s.text.startswith("event: ")
