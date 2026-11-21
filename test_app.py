import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as appmod
import llm

HERE = Path(__file__).parent
CARD = {"claim": "x", "speaker": None, "timestamp": "2026-11-21T00:00:00Z", "verdict": "conflict", "confidence": 0.9,
        "evidence": [{"sheet": "add1-p018", "revision": "add1", "bbox": [110, 110, 130, 130],
                      "crop": "build/crops/abc123.png", "crop_rect_pt": [100, 100, 200, 200], "label": "tag"}],
        "spec_refs": [], "checks": [], "reason": "r", "rfi_draft": None}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "BUILD", tmp_path)
    calls = []

    def fake_check(claim):
        calls.append(claim)
        return json.loads(json.dumps(CARD)) | {"claim": claim}

    monkeypatch.setattr(appmod.checker, "check", fake_check)
    monkeypatch.setattr(llm, "run_spend", lambda rid=None: 0.0)
    return TestClient(appmod.app, raise_server_exceptions=False), tmp_path, calls


def test_check_happy_path_ledger_and_crop_rewrite(env):
    c, tmp, calls = env
    r = c.post("/api/check", json={"claim": "  Toilet 105A is WC-2  "})
    assert r.status_code == 200
    card = r.json()
    assert calls == ["Toilet 105A is WC-2"] and card["id"].startswith("c_")
    ev = card["evidence"][0]
    assert ev["crop"] == "/crops/abc123.png"
    assert ev["box_2d_on_crop"] == [100, 100, 300, 300]       # 10..30 pt of a 100 pt crop, on a 0..1000 scale
    ledger = [json.loads(ln) for ln in (tmp / "cards.jsonl").read_text().splitlines()]
    assert len(ledger) == 1 and ledger[0]["id"] == card["id"]
    c.post("/api/check", json={"claim": "second"})
    out = c.get("/api/cards").json()
    fixtures = json.loads((HERE / "ui" / "cards.json").read_text())
    assert [x["claim"] for x in out] == ["second", "Toilet 105A is WC-2"] + [f["claim"] for f in fixtures]   # ledger newest first, then fixtures


def test_cards_fall_back_to_fixtures(env):
    c, _, _ = env
    cards = c.get("/api/cards").json()
    assert cards == json.loads((HERE / "ui" / "cards.json").read_text())


@pytest.mark.parametrize("body", [{"claim": ""}, {"claim": "   "}, {"claim": "a" * 501}, {}, {"claim": 5}])
def test_check_rejects_bad_claims(env, body):
    c, tmp, calls = env
    assert c.post("/api/check", json=body).status_code == 400
    assert not calls and not (tmp / "cards.jsonl").exists()


def test_check_503_does_not_leak(env, monkeypatch):
    c, tmp, _ = env

    def boom(claim):
        raise llm.CapExceeded("cap hit AIzaSECRETKEY prompt: Toilet 105A")

    monkeypatch.setattr(appmod.checker, "check", boom)
    r = c.post("/api/check", json={"claim": "anything"})
    assert r.status_code == 503 and r.json() == {"error": "CapExceeded"}
    assert "SECRET" not in r.text and "Toilet" not in r.text and "Traceback" not in r.text
    assert not (tmp / "cards.jsonl").exists()


def test_cache_miss_is_503(env, monkeypatch):
    c, _, _ = env
    monkeypatch.setattr(appmod.checker, "check", lambda claim: (_ for _ in ()).throw(llm.CacheMiss("k")))
    assert c.post("/api/check", json={"claim": "q"}).json() == {"error": "CacheMiss"}


def test_audio(env, monkeypatch):
    c, tmp, calls = env
    wav = (HERE / "tests" / "audio" / "clip1.wav").read_bytes()
    seen = {}
    claims = [{"claim": "one", "speaker": "Speaker 1", "t_start_s": 0.5, "t_end_s": 2.0, "quote": "one"},
              {"claim": "two", "speaker": "Speaker 2", "t_start_s": 3.0, "t_end_s": 4.0, "quote": "two"}]

    def fake_extract(data, mime):
        seen.update(n=len(data), mime=mime)
        return claims

    monkeypatch.setattr(appmod.audio_claims, "extract_claims", fake_extract)
    r = c.post("/api/audio", content=wav, headers={"Content-Type": "audio/wav"})
    assert r.status_code == 200 and seen == {"n": len(wav), "mime": "audio/wav"}
    body = r.json()
    assert body["claims"] == claims
    assert sorted(calls) == ["one", "two"]
    by = {x["claim"]: x for x in body["cards"]}
    assert by["two"]["speaker"] == "Speaker 2" and by["two"]["t_start_s"] == 3.0
    assert len((tmp / "cards.jsonl").read_text().splitlines()) == 2


@pytest.mark.parametrize("sent,passed", [("audio/mp4", "audio/mp4"), ("audio/x-m4a", "audio/m4a"),
                                         ("audio/aac", "audio/aac"), ("audio/mpeg", "audio/mpeg")])
def test_audio_safari_and_compressed_types_accepted(env, monkeypatch, sent, passed):
    """Safari's MediaRecorder sends audio/mp4; it used to get a 415."""
    c, _, _ = env
    seen = {}
    monkeypatch.setattr(appmod.audio_claims, "extract_claims", lambda d, m: seen.update(mime=m) or [])
    r = c.post("/api/audio", content=b"abc", headers={"Content-Type": sent})
    assert r.status_code == 200 and seen["mime"] == passed


def test_audio_webm_codec_param_and_limits(env, monkeypatch):
    c, _, _ = env
    monkeypatch.setattr(appmod.audio_claims, "extract_claims", lambda d, m: [])
    r = c.post("/api/audio", content=b"abc", headers={"Content-Type": "audio/webm;codecs=opus"})
    assert r.status_code == 200 and r.json() == {"claims": [], "cards": []}
    assert c.post("/api/audio", content=b"abc", headers={"Content-Type": "text/plain"}).status_code == 415
    assert c.post("/api/audio", content=b"", headers={"Content-Type": "audio/wav"}).status_code == 400
    big = b"0" * (appmod.MAX_AUDIO + 1)
    assert c.post("/api/audio", content=big, headers={"Content-Type": "audio/wav"}).status_code == 413


def test_rfi_draft_and_blocked(env, monkeypatch):
    c, _, _ = env
    card = c.post("/api/check", json={"claim": "gate question"}).json()
    monkeypatch.setattr(appmod.rfi, "draft_rfi", lambda cd: {"text": "T", "status": "draft", "answered_by": None, "quote": None,
                                                              "extra": 1})
    assert c.post("/api/rfi", json={"card_id": card["id"]}).json() == {"status": "draft", "text": "T", "answered_by": None,
                                                                       "quote": None}
    monkeypatch.setattr(appmod.rfi, "draft_rfi", lambda cd: {"text": "T", "status": "blocked", "answered_by": "Addendum 1 Q6",
                                                              "quote": "Provide padlock latches"})
    r = c.post("/api/rfi", json={"card_id": card["id"]}).json()
    assert r["status"] == "blocked" and r["answered_by"] == "Addendum 1 Q6" and r["quote"] == "Provide padlock latches"
    assert c.post("/api/rfi", json={"card_id": "nope"}).status_code == 404


def test_rfi_works_on_fixture_card(env, monkeypatch):
    c, _, _ = env
    fid = json.loads((HERE / "ui" / "cards.json").read_text())[0]["id"]
    monkeypatch.setattr(appmod.rfi, "draft_rfi", lambda cd: {"text": cd["claim"], "status": "draft", "answered_by": None,
                                                              "quote": None})
    assert c.post("/api/rfi", json={"card_id": fid}).json()["status"] == "draft"


def test_health(env, monkeypatch):
    c, _, _ = env
    monkeypatch.setenv("LLM_CACHE", "replay")
    monkeypatch.setattr(llm, "run_spend", lambda rid=None: 0.1234)
    assert c.get("/api/health").json() == {"ok": True, "cache_mode": "replay", "run_spend": 0.1234}


def test_static(env):
    c, tmp, _ = env
    assert "SheetSight" in c.get("/").text
    assert c.get("/assets/p101_current.png").status_code == 200
    fx = next((HERE / "ui" / "crops").iterdir()).name
    assert c.get(f"/crops/{fx}").status_code == 200           # ui fixture crop
    (tmp / "crops").mkdir()
    (tmp / "crops" / "z.png").write_bytes(b"png")
    assert c.get("/crops/z.png").content == b"png"             # build crop
    assert c.get("/crops/..%2Fapp.py").status_code == 404


def _sse(text):
    out = []
    for block in text.strip().split("\n\n"):
        ev, data = block.split("\n", 1)
        out.append((ev.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return out


def test_stream_emits_steps_then_card_and_appends_ledger(env, monkeypatch):
    c, tmp, _ = env

    def fake_check(claim, progress=None):
        for st in ("search", "crop", "judge_a", "judge_b", "grounding", "revision"):
            progress(st, "start")
            progress(st, "done")
        return json.loads(json.dumps(CARD)) | {"claim": claim}

    monkeypatch.setattr(appmod.checker, "check", fake_check)
    r = c.get("/api/check/stream", params={"claim": "  Toilet 105A is WC-2 "})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    ev = _sse(r.text)
    assert [k for k, _ in ev] == ["step"] * 13 + ["card"]
    steps = [d for k, d in ev if k == "step"]
    assert [s["step"] for s in steps][:2] == ["search", "search"] and steps[-1]["step"] == "sealed"
    assert all("t" in s and s["status"] in ("start", "done") for s in steps)
    card = ev[-1][1]
    assert card["claim"] == "Toilet 105A is WC-2" and card["id"].startswith("c_") and len(card["seal"]) == 8
    assert steps[-1]["seal"] == card["seal"]
    assert len((tmp / "cards.jsonl").read_text().splitlines()) == 1


def test_stream_validation_and_error_event(env, monkeypatch):
    c, tmp, _ = env
    assert c.get("/api/check/stream").status_code == 400
    assert c.get("/api/check/stream", params={"claim": "x" * 501}).status_code == 400

    def boom(claim, progress=None):
        raise llm.CapExceeded("cap hit AIzaSECRETKEY prompt: Toilet 105A")

    monkeypatch.setattr(appmod.checker, "check", boom)
    r = c.get("/api/check/stream", params={"claim": "q"})
    assert _sse(r.text) == [("error", {"error": "CapExceeded"})]
    assert "SECRET" not in r.text and not (tmp / "cards.jsonl").exists()


def test_seal_hash_depends_on_claim_boxes_and_sheets():
    ev = [{"sheet": "s1", "bbox": [1, 2, 3, 4]}]
    h = appmod.seal_hash("c", ev)
    assert len(h) == 8 and h == appmod.seal_hash("c", ev)
    assert h != appmod.seal_hash("d", ev)
    assert h != appmod.seal_hash("c", [{"sheet": "s1", "bbox": [1, 2, 3, 5]}])
    assert h != appmod.seal_hash("c", [{"sheet": "s2", "bbox": [1, 2, 3, 4]}])


def test_metrics_endpoint(env):
    c, tmp, _ = env
    assert c.get("/api/metrics").json() is None
    (tmp / "metrics.json").write_text('{"accuracy": 0.9}')
    assert c.get("/api/metrics").json() == {"accuracy": 0.9}


def test_finish_rewrites_superseded_crop_path():
    card = appmod._finish({"claim": "c", "evidence": [], "checks": [{"by": "code", "check": "addendum_diff",
                                                                    "superseded_crop": "build/crops/old.png"}]}, "c")
    assert card["checks"][0]["superseded_crop"] == "/crops/old.png"
