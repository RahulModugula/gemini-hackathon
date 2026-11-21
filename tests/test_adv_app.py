"""Adversarial / security tests for app.py (tests only; no product code changed).

Requests that must bypass httpx's client-side URL normalisation (``..`` segments, encoded slashes, NUL bytes,
explicit Content-Length lies) are sent through a raw ASGI call so the app sees exactly the bytes we intend.
"""
import asyncio
import json
import threading
from pathlib import Path
from urllib.parse import unquote_to_bytes

import pytest
from fastapi.testclient import TestClient
from google.genai import errors as genai_errors

import app as appmod
import llm
import video_claims

HERE = Path(__file__).resolve().parent.parent   # repo root (this file lives in tests/)
APP_SRC = (HERE / "app.py").read_bytes()
KEY = "AIzaTESTKEY123"
CANARY = b"SECRET_CANARY_DO_NOT_SERVE"
MAX_AUDIO = appmod.MAX_AUDIO
CARD = {"claim": "x", "speaker": None, "timestamp": "2026-11-21T00:00:00Z", "verdict": "conflict", "confidence": 0.9,
        "evidence": [], "spec_refs": [], "checks": [], "reason": "r", "rfi_draft": None}
JSON_H = {"Content-Type": "application/json"}


# ---- helpers ----------------------------------------------------------------------------------------------
def asgi(method, target, body=b"", headers=()):
    """Send one request straight into the ASGI app with the path bytes exactly as given (no client normalisation)."""
    raw_path, _, qs = target.partition("?")
    raw = raw_path.encode("utf-8")
    path = unquote_to_bytes(raw).decode("utf-8", "surrogateescape")
    hdrs = [(k.lower().encode(), v.encode()) for k, v in headers] + [(b"host", b"testserver")]
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": raw, "root_path": "",
             "query_string": qs.encode(), "headers": hdrs, "client": ("127.0.0.1", 50000),
             "server": ("testserver", 80)}
    got = {"status": None, "headers": {}, "body": b""}
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.get_running_loop().create_future()   # never signal disconnect mid-response
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.start":
            got["status"] = msg["status"]
            got["headers"] = {k.decode().lower(): v.decode("latin-1") for k, v in msg.get("headers", [])}
        elif msg["type"] == "http.response.body":
            got["body"] += msg.get("body", b"")

    asyncio.run(appmod.app(scope, receive, send))
    return got


def leaks(status, headers, body, needle=KEY.encode()):
    blob = body + b"\n" + "\n".join(f"{k}: {v}" for k, v in headers.items()).encode()
    return needle in blob or b"AIza" in blob


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "BUILD", tmp_path)
    monkeypatch.setattr(llm, "run_spend", lambda rid=None: 0.0)
    calls = []

    def fake_check(claim):
        calls.append(claim)
        return json.loads(json.dumps(CARD)) | {"claim": claim}

    monkeypatch.setattr(appmod.checker, "check", fake_check)
    return TestClient(appmod.app, raise_server_exceptions=False), tmp_path, calls


@pytest.fixture
def key_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    monkeypatch.delenv("LLM_CACHE", raising=False)


# ---- static routes: path traversal ------------------------------------------------------------------------
@pytest.fixture
def traversal_fs(tmp_path, monkeypatch):
    """BUILD = tmp/build. Canary secrets sit where a successful traversal would land (tmp/.env, tmp/secret.txt)."""
    build = tmp_path / "build"
    (build / "crops").mkdir(parents=True)
    (build / "crops" / "ok.png").write_bytes(b"png-bytes")
    (tmp_path / ".env").write_bytes(CANARY + b" GEMINI_API_KEY=" + KEY.encode())
    (tmp_path / "secret.txt").write_bytes(CANARY)
    monkeypatch.setattr(appmod, "BUILD", build)
    return build


def test_traversal_fixture_positive_control(traversal_fs):
    """The raw-ASGI helper really reaches the crops route: a real file comes back 200."""
    r = asgi("GET", "/crops/ok.png")
    assert r["status"] == 200 and r["body"] == b"png-bytes"


@pytest.mark.parametrize("target", [
    "/crops/../../.env",
    "/crops/../.env",
    "/crops/%2e%2e/%2e%2e/.env",
    "/crops/..%2F..%2F.env",
    "/crops/..%2f..%2f.env",
    "/crops/%2e%2e%2f%2e%2e%2f.env",
    "/crops/..%5C..%5C.env",
    "/crops/%252e%252e%252f.env",
    "/crops/..",
    "/crops/%2e%2e",
    "/crops/.env",
    "/crops/%2Fetc%2Fpasswd",
    "/crops//etc/passwd",
    "/crops/%2F.env",
    "/crops/a%00.png",
    "/crops/%00",
    "/crops/ok.png%00.env",
])
def test_crops_traversal_never_serves_outside_files(traversal_fs, target):
    r = asgi("GET", target)
    assert r["status"] in (400, 404), target
    assert CANARY not in r["body"] and b"GEMINI_API_KEY" not in r["body"] and b"root:" not in r["body"]


@pytest.mark.parametrize("target", [
    "/assets/..%2Fapp.py",
    "/assets/%2e%2e%2fapp.py",
    "/assets/../app.py",
    "/assets/../../app.py",
    "/assets/%2e%2e/%2e%2e/app.py",
    "/assets/..%5capp.py",
    "/assets/%252e%252e%252fapp.py",
    "/assets//etc/passwd",
    "/assets/%2Fetc%2Fpasswd",
    "/assets/%00",
    "/assets/p101_current.png/../../app.py",
])
def test_assets_traversal_never_serves_outside_files(target):
    r = asgi("GET", target)
    assert r["status"] != 200, target
    assert APP_SRC not in r["body"] and b"FastAPI backend" not in r["body"] and b"root:" not in r["body"]


def test_crops_do_not_expose_ui_source_or_dotfiles(traversal_fs):
    assert asgi("GET", "/crops/index.html")["status"] == 404
    assert asgi("GET", "/crops/.env")["status"] == 404


def test_crops_error_bodies_are_plain_not_found(traversal_fs):
    r = asgi("GET", "/crops/..%2F..%2F.env")
    assert b"Traceback" not in r["body"] and b"/Users/" not in r["body"]


# ---- /api/check: claim boundaries and payloads ------------------------------------------------------------
@pytest.mark.parametrize("claim,status", [
    ("", 400), (" ", 400), ("\t\n", 400),
    ("x", 200), ("a" * 500, 200), ("a" * 501, 400), ("a" * 100_000, 400),
])
def test_check_claim_length_boundaries(env, claim, status):
    c, _, calls = env
    r = c.post("/api/check", json={"claim": claim})
    assert r.status_code == status
    assert (len(calls) == 1) == (status == 200)


def test_check_length_counts_after_strip(env):
    c, _, calls = env
    assert c.post("/api/check", json={"claim": "  " + "a" * 500 + "  "}).status_code == 200
    assert calls == ["a" * 500]


def test_check_unicode_claim_round_trips(env):
    c, _, calls = env
    claim = "Ünïcødé ⚠️ 105A — WC‑2 🚽 ​‮ rtl"
    r = c.post("/api/check", json={"claim": claim})
    assert r.status_code == 200 and r.json()["claim"] == claim and calls == [claim]


SCRIPT = '<script>alert(document.cookie)</script><img src=x onerror=alert(1)>"\'</b>'


def test_stored_html_claim_comes_back_as_json_data(env):
    c, _, _ = env
    assert c.post("/api/check", json={"claim": SCRIPT}).status_code == 200
    r = c.get("/api/cards")
    assert r.headers["content-type"].startswith("application/json")
    assert "text/html" not in r.headers["content-type"]
    assert json.loads(r.text)[0]["claim"] == SCRIPT


def test_served_index_has_no_html_injection_sinks():
    """The UI builds every claim with textContent / createElement, never innerHTML-style sinks."""
    from fastapi.testclient import TestClient as TC
    page = TC(appmod.app).get("/").text
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in page, sink
    assert "textContent" in page and "createElement" in page


# ---- /api/check: wrong JSON shapes and bodies -------------------------------------------------------------
@pytest.mark.parametrize("payload", [
    {"claim": 123}, {"claim": None}, {"claim": ["x"]}, {"claim": {"a": 1}}, {"claim": True},
    {"claim": 1.5}, {"other": "x"}, ["x"], "just a string", 123, None, True, [],
])
def test_check_rejects_wrong_json_shapes(env, payload):
    c, _, calls = env
    r = c.post("/api/check", content=json.dumps(payload), headers=JSON_H)
    assert r.status_code == 400 and not calls


def test_check_missing_body_is_400(env):
    c, _, calls = env
    assert c.post("/api/check", headers=JSON_H).status_code == 400
    assert not calls


@pytest.mark.parametrize("raw", [b"{claim:", b"{'claim': 'x'}", b"\xff\xfe garbage", b""])
def test_check_malformed_json_is_400(env, raw):
    c, _, calls = env
    assert c.post("/api/check", content=raw, headers=JSON_H).status_code == 400
    assert not calls


def test_check_form_encoded_body_is_400(env):
    c, _, calls = env
    r = c.post("/api/check", content=b"claim=Toilet+105A", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 400 and not calls


@pytest.mark.xfail(strict=True, reason="BUG: /api/check ignores Content-Type and parses any body as JSON, so a "
                   "cross-site text/plain form POST (a CORS 'simple request', no preflight) is processed: it spends "
                   "LLM budget and writes the ledger. Expected 415 for non-JSON content types.")
def test_check_text_plain_json_body_is_rejected(env):
    c, _, calls = env
    r = c.post("/api/check", content=b'{"claim": "cross-site"}', headers={"Content-Type": "text/plain"})
    assert r.status_code == 415 and not calls


@pytest.mark.xfail(strict=True, reason="BUG: /api/check returns 500 (not 400) for a claim with a lone UTF-16 "
                   "surrogate: _finish() calls seed.encode() on it and raises UnicodeEncodeError. Repro: POST "
                   "/api/check body b'{\"claim\": \"bad \\\\ud800 char\"}' with a mocked check.")
def test_check_lone_surrogate_claim_is_not_a_500(env):
    c, _, _ = env
    r = c.post("/api/check", content=b'{"claim": "bad \\ud800 char"}', headers=JSON_H)
    assert r.status_code < 500


@pytest.mark.xfail(strict=True, reason="BUG: deeply nested JSON (100k '[') makes json.loads raise RecursionError, "
                   "which request.json() does not map to 400, so /api/check returns 500. Repro: POST "
                   "'[' * 100000 + ']' * 100000 with Content-Type application/json.")
def test_check_deeply_nested_json_is_not_a_500(env):
    c, _, _ = env
    r = c.post("/api/check", content=("[" * 100_000 + "]" * 100_000).encode(), headers=JSON_H)
    assert r.status_code < 500


@pytest.mark.xfail(strict=True, reason="BUG: a card whose text fields hold a lone surrogate is appended to the ledger "
                   "(json.dumps escapes it) and then the response encode fails; every later GET /api/cards then "
                   "returns 500 because _read_ledger() re-parses the poisoned line. Repro: mocked check returns "
                   "reason='split \\ud83d emoji', then GET /api/cards -> 500.")
def test_poisoned_ledger_line_does_not_break_cards(env):
    c, tmp, _ = env
    (tmp / "cards.jsonl").write_text(json.dumps(CARD | {"claim": "poison", "reason": "split \ud83d emoji"}) + "\n")
    assert c.get("/api/cards").status_code == 200


# ---- /api/audio -------------------------------------------------------------------------------------------
@pytest.fixture
def audio_env(env, monkeypatch):
    c, tmp, calls = env
    seen = []
    monkeypatch.setattr(appmod.audio_claims, "extract_claims", lambda d, m: seen.append((len(d), m)) or [])
    return c, tmp, seen


@pytest.mark.parametrize("ctype", ["text/plain", "application/json", "video/mp4", "image/png", "audio/*",
                                   "audio/flac", "application/octet-stream", "audio"])
def test_audio_wrong_content_type_is_415(audio_env, ctype):
    c, _, seen = audio_env
    assert c.post("/api/audio", content=b"RIFFxxxx", headers={"Content-Type": ctype}).status_code == 415
    assert not seen


def test_audio_missing_content_type_is_415(audio_env):
    c, _, seen = audio_env
    assert c.post("/api/audio", content=b"RIFFxxxx").status_code == 415 and not seen


def test_audio_empty_body_is_400(audio_env):
    c, _, seen = audio_env
    assert c.post("/api/audio", content=b"", headers={"Content-Type": "audio/wav"}).status_code == 400 and not seen


def test_audio_content_type_case_and_params_normalised(audio_env):
    c, _, seen = audio_env
    assert c.post("/api/audio", content=b"abc", headers={"Content-Type": "AUDIO/WAV; charset=x"}).status_code == 200
    assert seen == [(3, "audio/wav")]


def test_audio_content_length_header_small_body_big_is_413(audio_env):
    c, _, seen = audio_env
    big = b"0" * (MAX_AUDIO + 1)
    r = asgi("POST", "/api/audio", big, [("Content-Type", "audio/wav"), ("Content-Length", "5")])
    assert r["status"] == 413 and not seen


def test_audio_content_length_header_big_body_small_is_413(audio_env):
    c, _, seen = audio_env
    r = asgi("POST", "/api/audio", b"abc", [("Content-Type", "audio/wav"), ("Content-Length", str(MAX_AUDIO + 1))])
    assert r["status"] == 413 and not seen


@pytest.mark.parametrize("cl", ["abc", "1e3", "5.5", "--1"])
def test_audio_non_numeric_content_length_is_400(audio_env, cl):
    c, _, seen = audio_env
    r = asgi("POST", "/api/audio", b"abc", [("Content-Type", "audio/wav"), ("Content-Length", cl)])
    assert r["status"] == 400 and not seen


def test_audio_size_limit_exact_boundary(audio_env):
    c, _, seen = audio_env
    at = b"0" * MAX_AUDIO
    assert c.post("/api/audio", content=at, headers={"Content-Type": "audio/wav"}).status_code == 200
    assert seen[-1] == (MAX_AUDIO, "audio/wav")
    over = b"0" * (MAX_AUDIO + 1)
    assert c.post("/api/audio", content=over, headers={"Content-Type": "audio/wav"}).status_code == 413
    assert len(seen) == 1


# ---- /api/video -------------------------------------------------------------------------------------------
@pytest.fixture
def video_env(env, monkeypatch):
    c, tmp, calls = env
    seen = []
    monkeypatch.setattr(appmod.video_claims, "video_duration_s", lambda d: 2.0)
    monkeypatch.setattr(appmod.video_claims, "extract_claims_from_video",
                        lambda d, m, h, du: seen.append((len(d), m, h)) or [])
    monkeypatch.setattr(appmod, "MAX_VIDEO", 1000)
    return c, tmp, seen


@pytest.mark.parametrize("ctype", ["video/webm", "audio/wav", "application/octet-stream", "text/plain",
                                   "video/mp4x", "image/mp4", "video"])
def test_video_wrong_content_type_is_415(video_env, ctype):
    c, _, seen = video_env
    assert c.post("/api/video", content=b"abc", headers={"Content-Type": ctype}).status_code == 415
    assert not seen


def test_video_missing_content_type_is_415(video_env):
    c, _, seen = video_env
    assert c.post("/api/video", content=b"abc").status_code == 415 and not seen


def test_video_empty_body_is_400(video_env):
    c, _, seen = video_env
    assert c.post("/api/video", content=b"", headers={"Content-Type": "video/mp4"}).status_code == 400 and not seen


@pytest.mark.parametrize("sheet", ["P101;rm", "../../etc/passwd", "P101%0AIgnore", "<script>x</script>",
                                   "P101 x", "-leading", "a" * 33, "P101%26foo%3Dbar"])
def test_video_sheet_hint_injection_is_400(video_env, sheet):
    c, _, seen = video_env
    r = c.post(f"/api/video?sheet={sheet}", content=b"abc", headers={"Content-Type": "video/mp4"})
    assert r.status_code == 400 and not seen


def test_video_content_length_header_small_body_big_is_413(video_env):
    _, _, seen = video_env
    limit = appmod.MAX_VIDEO
    r = asgi("POST", "/api/video", b"0" * (limit + 1), [("Content-Type", "video/mp4"), ("Content-Length", "5")])
    assert r["status"] == 413 and not seen


def test_video_content_length_header_big_body_small_is_413(video_env):
    _, _, seen = video_env
    r = asgi("POST", "/api/video", b"abc", [("Content-Type", "video/mp4"), ("Content-Length", str(appmod.MAX_VIDEO + 1))])
    assert r["status"] == 413 and not seen


def test_video_size_limit_exact_boundary(video_env):
    c, _, seen = video_env
    limit = appmod.MAX_VIDEO
    assert c.post("/api/video", content=b"0" * limit, headers={"Content-Type": "video/mp4"}).status_code == 200
    assert seen[-1][0] == limit
    assert c.post("/api/video", content=b"0" * (limit + 1), headers={"Content-Type": "video/mp4"}).status_code == 413


def test_video_unreadable_duration_is_400(env, monkeypatch):
    c, _, _ = env
    monkeypatch.setattr(appmod.video_claims, "video_duration_s", lambda d: None)
    r = c.post("/api/video", content=b"not a video", headers={"Content-Type": "video/mp4"})
    assert r.status_code == 400 and "could not read" in r.json()["error"]


# ---- /api/rfi ---------------------------------------------------------------------------------------------
@pytest.fixture
def rfi_env(env, monkeypatch):
    c, tmp, _ = env
    seen = []
    monkeypatch.setattr(appmod.rfi, "draft_rfi", lambda cd, *a, **k: seen.append(cd) or
                        {"text": "T", "status": "draft", "answered_by": None, "quote": None})
    return c, tmp, seen


@pytest.mark.parametrize("cid", ["nope", "", "' OR 1=1 --", "../../etc/passwd", "c_0000000000\x00", "*",
                                 "c_" + "A" * 500, "<script>x</script>", "c_%2e%2e", "1; DROP TABLE cards"])
def test_rfi_unknown_or_injection_ids_are_404(rfi_env, cid):
    c, _, seen = rfi_env
    r = c.post("/api/rfi", json={"card_id": cid})
    assert r.status_code == 404 and r.json() == {"error": "unknown card_id"} and not seen


@pytest.mark.parametrize("body", [{"card_id": 123}, {"card_id": None}, {"card_id": ["c_1"]},
                                  {"card_id": {"id": "x"}}, {}, ["c_1"], "c_1"])
def test_rfi_non_string_card_id_is_404(rfi_env, body):
    c, _, seen = rfi_env
    assert c.post("/api/rfi", json=body).status_code == 404 and not seen


@pytest.mark.parametrize("raw", [None, b"{card_id:", b""])
def test_rfi_missing_or_invalid_body_is_400(rfi_env, raw):
    c, _, seen = rfi_env
    r = c.post("/api/rfi", content=raw, headers=JSON_H) if raw is not None else c.post("/api/rfi", headers=JSON_H)
    assert r.status_code == 400 and not seen


def test_rfi_known_card_draft_only_returns_whitelisted_keys(rfi_env, monkeypatch):
    c, tmp, seen = rfi_env
    card = c.post("/api/check", json={"claim": "gate?"}).json()
    monkeypatch.setattr(appmod.rfi, "draft_rfi", lambda cd, *a, **k: seen.append(cd) or
                        {"text": "T", "status": "draft", "answered_by": None, "quote": None, "secret_internal": KEY})
    r = c.post("/api/rfi", json={"card_id": card["id"]})
    assert r.status_code == 200 and set(r.json()) == {"status", "text", "answered_by", "quote"}
    assert seen[0]["claim"] == "gate?" and KEY not in r.text


# ---- secrets: nothing leaks from any endpoint -------------------------------------------------------------
def test_health_does_not_leak_key(key_env, env):
    c, _, _ = env
    r = c.get("/api/health")
    assert r.status_code == 200 and set(r.json()) == {"ok", "cache_mode", "run_spend"}
    assert not leaks(r.status_code, r.headers, r.content)


def test_leak_detector_is_not_vacuous(key_env, env, monkeypatch):
    """Positive control: a deliberately leaking response must be flagged, or the scans above prove nothing."""
    c, _, _ = env
    monkeypatch.setattr(appmod.checker, "check", lambda claim: CARD | {"claim": claim, "reason": f"leak {KEY}"})
    r = c.post("/api/check", json={"claim": "control"})
    assert r.status_code == 200 and leaks(r.status_code, r.headers, r.content)


def _raise(exc):
    def f(*a, **k):
        raise exc
    return f


SCENARIOS = {
    "check_runtime_error": ("/api/check", "json", {"claim": "q"},
                            ("check", RuntimeError(f"boom {KEY} prompt: Toilet 105A"))),
    "check_cap": ("/api/check", "json", {"claim": "q"}, ("check", llm.CapExceeded(f"cap {KEY} Toilet"))),
    "check_genai_apierror": ("/api/check", "json", {"claim": "q"},
                             ("check", genai_errors.APIError(500, {"error": {"message": KEY}}))),
    "audio_runtime_error": ("/api/audio", "audio", b"RIFFxxxx",
                            ("audio_extract", RuntimeError(f"audio {KEY}"))),
    "video_runtime_error": ("/api/video", "video", b"0" * 16,
                            ("video_extract", RuntimeError(f"video {KEY}"))),
    "rfi_runtime_error": ("/api/rfi", "rfi", None, ("rfi", RuntimeError(f"rfi {KEY}"))),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_key_never_in_error_response(key_env, env, monkeypatch, name):
    c, tmp, _ = env
    path, kind, payload, (where, exc) = SCENARIOS[name]
    monkeypatch.setattr(video_claims, "video_duration_s", lambda d: 2.0)
    monkeypatch.setattr(appmod.video_claims, "video_duration_s", lambda d: 2.0)
    monkeypatch.setattr(appmod.audio_claims, "extract_claims", _raise(exc) if where == "audio_extract" else (lambda d, m: []))
    monkeypatch.setattr(appmod.video_claims, "extract_claims_from_video", _raise(exc) if where == "video_extract" else (lambda d, m, h, du: []))
    monkeypatch.setattr(appmod.rfi, "draft_rfi", _raise(exc) if where == "rfi" else (lambda cd, *a, **k: {}))
    if where == "check":
        monkeypatch.setattr(appmod.checker, "check", _raise(exc))
    if kind == "json":
        r = c.post(path, json=payload)
    elif kind == "audio":
        r = c.post(path, content=payload, headers={"Content-Type": "audio/wav"})
    elif kind == "video":
        r = c.post(path, content=payload, headers={"Content-Type": "video/mp4"})
    else:
        c.post("/api/check", json={"claim": "gate"})  # make a real card so the rfi route reaches draft_rfi
        cid = json.loads((tmp / "cards.jsonl").read_text().splitlines()[0])["id"]
        r = c.post(path, json={"card_id": cid})
    assert r.status_code >= 400
    assert not leaks(r.status_code, r.headers, r.content)
    assert b"Traceback" not in r.content and b'File "' not in r.content
    assert b"prompt" not in r.content and b"Toilet" not in r.content


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/", None), ("GET", "/api/cards", None), ("GET", "/api/health", None),
    ("GET", "/crops/missing.png", None), ("GET", "/nope", None), ("GET", "/assets/missing.png", None),
    ("POST", "/api/check", b"{bad"), ("POST", "/api/rfi", b"{bad"), ("PUT", "/api/check", b"{}"),
    ("DELETE", "/api/cards", None),
])
def test_key_never_in_any_plain_response(key_env, env, method, path, body):
    c, _, _ = env
    r = asgi(method, path, body or b"", [("Content-Type", "application/json")])
    assert not leaks(r["status"], r["headers"], r["body"])


# ---- concurrency -----------------------------------------------------------------------------------------
def test_concurrent_checks_keep_ledger_intact(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "BUILD", tmp_path)
    monkeypatch.setattr(llm, "run_spend", lambda rid=None: 0.0)
    import time

    def slow_check(claim):
        time.sleep(0.01)
        return CARD | {"claim": claim}

    monkeypatch.setattr(appmod.checker, "check", slow_check)
    n = 20
    barrier = threading.Barrier(n)
    results = {}
    errors = []

    def worker(i):
        try:
            client = TestClient(appmod.app, raise_server_exceptions=False)
            barrier.wait()
            results[i] = client.post("/api/check", json={"claim": f"claim-{i}"}).status_code
        except Exception as e:  # pragma: no cover - surfaced below
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors and set(results) == set(range(n)) and set(results.values()) == {200}
    lines = (tmp_path / "cards.jsonl").read_text().splitlines()
    cards = [json.loads(ln) for ln in lines]              # every line valid JSON
    assert len(cards) == n
    assert {c["claim"] for c in cards} == {f"claim-{i}" for i in range(n)}
    assert len({c["id"] for c in cards}) == n


# ---- CORS ------------------------------------------------------------------------------------------------
def test_no_cors_headers_on_cross_origin_get(env):
    """No CORSMiddleware is installed: cross-origin reads are blocked by browsers (no ACAO header is sent)."""
    c, _, _ = env
    r = c.get("/api/cards", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers
    assert "access-control-allow-credentials" not in r.headers


def test_no_cors_preflight_for_check(env):
    c, _, calls = env
    r = c.options("/api/check", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
                                         "Access-Control-Request-Headers": "content-type"})
    assert r.status_code == 405 and "access-control-allow-origin" not in r.headers and not calls


# ---- response hygiene ------------------------------------------------------------------------------------
def test_unhandled_exception_body_is_generic(key_env, env, monkeypatch):
    c, _, _ = env
    monkeypatch.setattr(appmod.checker, "check", _raise(ValueError("internal /Users/rmodugula/secret path")))
    r = c.post("/api/check", json={"claim": "q"})
    assert r.status_code == 500
    assert r.text == "Internal Server Error"
    assert "/Users/" not in r.text and "ValueError" not in r.text
