import json
import shutil
import struct
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app as appmod
import llm
import video_claims as vc
from google.genai import types


def box(typ, payload):
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


def mp4(secs, scale=1000, version=0):
    if version == 0:
        mvhd = struct.pack(">B3xIIII", 0, 0, 0, scale, int(secs * scale)) + b"\0" * 80
    else:
        mvhd = struct.pack(">B3xQQIQ", 1, 0, 0, scale, int(secs * scale)) + b"\0" * 80
    return box(b"ftyp", b"isom\0\0\2\0isom") + box(b"free", b"xx") + box(b"moov", box(b"mvhd", mvhd))


def C(claim="Toilet 105A water closets are WC-2", kind="spoken", a=0.5, b=4.0, q="toilet one-oh-five A are WC-2"):
    return dict(claim=claim, kind=kind, t_start_s=a, t_end_s=b, quote_or_observation=q)


@pytest.fixture
def fake(monkeypatch):
    seen = {}

    def call(model, parts, **kw):
        seen.update(model=model, parts=parts, **kw)
        return SimpleNamespace(json=seen["payload"])

    monkeypatch.setattr(vc.llm, "call", call)
    monkeypatch.setattr(vc, "_ffprobe_duration", lambda d: None)   # mvhd parser unless a test says otherwise
    seen["payload"] = {"claims": []}
    return seen


# ---- duration ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("version,scale", [(0, 1000), (1, 600)])
def test_mvhd_parser(version, scale):
    assert vc._mvhd_duration(mp4(7.5, scale, version)) == pytest.approx(7.5)


def test_mvhd_garbage_and_truncated():
    assert vc._mvhd_duration(b"") is None
    assert vc._mvhd_duration(b"not an mp4 at all, really") is None
    assert vc._mvhd_duration(mp4(3)[:-30]) is None
    assert vc._mvhd_duration(box(b"moov", box(b"mvhd", b"\0" * 4))) is None


def test_duration_falls_back_to_mvhd(monkeypatch):
    monkeypatch.setattr(vc, "_ffprobe_duration", lambda d: None)
    assert vc.video_duration_s(mp4(4)) == pytest.approx(4)
    monkeypatch.setattr(vc, "_ffprobe_duration", lambda d: 9.0)
    assert vc.video_duration_s(mp4(4)) == 9.0
    monkeypatch.setattr(vc, "_ffprobe_duration", lambda d: None)
    assert vc.video_duration_s(b"junk") is None


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg absent")
def test_real_generated_mp4(tmp_path):
    f = tmp_path / "t.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=2:r=10", "-f", "lavfi",
                    "-i", "anullsrc=r=16000:cl=mono", "-t", "2", "-shortest", "-pix_fmt", "yuv420p", str(f)], check=True)
    data = f.read_bytes()
    assert vc._mvhd_duration(data) == pytest.approx(2.0, abs=0.1)
    assert vc._ffprobe_duration(data) == pytest.approx(2.0, abs=0.1)


# ---- extraction -------------------------------------------------------------------------------------------
def test_call_args_and_shape(fake):
    fake["payload"] = {"claims": [C(), C("Fixture tag WC-2 is legible", "visual", 5, 6, "tag reads WC-2")]}
    out = vc.extract_claims_from_video(mp4(8), "video/mp4")
    assert [c["kind"] for c in out] == ["spoken", "visual"]
    assert set(out[0]) == {"claim", "kind", "t_start_s", "t_end_s", "quote_or_observation", "frame_t_s", "frame_box_2d"}
    assert (fake["model"], fake["thinking"], fake["purpose"], fake["schema"]) == (
        "gemini-4-argon", "LOW", "video:claims", vc.SCHEMA)
    part = fake["parts"][0]
    assert part.inline_data.mime_type == "video/mp4" and part.video_metadata.end_offset == "8s"
    assert fake["parts"][1] == vc.PROMPT
    assert "SPOKEN" in vc.PROMPT and "at most 3" in vc.PROMPT and "[]" in vc.PROMPT


def test_sheet_hint_goes_to_prompt(fake):
    vc.extract_claims_from_video(mp4(8), "video/mp4", "P101")
    assert fake["parts"][1].endswith("drawing sheet P101.") and fake["parts"][1].startswith(vc.PROMPT)


def test_validation_drops_bad_items(fake):
    fake["payload"] = {"claims": [C(), C(a=1, b=99), C(a=-1, b=2), C(a=4, b=3), C(claim=" "), C(q=""),
                                  C(kind="other"), {"claim": "x"}, "junk", C(a="x")]}
    assert len(vc.extract_claims_from_video(mp4(5), "video/mp4")) == 1


def test_caps_ten_claims_and_three_visual(fake):
    fake["payload"] = {"claims": [C(kind="visual", claim=f"v{i}") for i in range(6)]
                       + [C(claim=f"s{i}") for i in range(12)]}
    out = vc.extract_claims_from_video(mp4(8), "video/mp4")
    assert len(out) == 10 and sum(c["kind"] == "visual" for c in out) == 3


def test_empty_and_non_dict(fake):
    assert vc.extract_claims_from_video(mp4(5), "video/mp4") == []
    fake["payload"] = None
    assert vc.extract_claims_from_video(mp4(5), "video/mp4") == []


def test_unreadable_duration_raises(fake):
    with pytest.raises(ValueError):
        vc.extract_claims_from_video(b"junk", "video/mp4")


def test_large_video_uses_files_api(fake, monkeypatch):
    log = []
    up = types.Part(file_data=types.FileData(file_uri="https://x/v1beta/files/abc", mime_type="video/mp4"))
    monkeypatch.setattr(vc.llm, "upload_video", lambda d, m, dur: log.append(("up", len(d), m, dur)) or up)
    monkeypatch.setattr(vc.llm, "delete_file", lambda p: log.append(("del", p)))
    monkeypatch.setattr(vc, "MAX_INLINE", 100)
    data = mp4(6) + b"0" * 200
    vc.extract_claims_from_video(data, "video/quicktime")
    assert log[0] == ("up", len(data), "video/quicktime", pytest.approx(6)) and log[1] == ("del", up)
    assert fake["parts"][0] is up


def test_upload_deleted_even_if_call_fails(monkeypatch):
    up = types.Part(file_data=types.FileData(file_uri="u", mime_type="video/mp4"))
    deleted = []
    monkeypatch.setattr(vc, "MAX_INLINE", 1)
    monkeypatch.setattr(vc, "_ffprobe_duration", lambda d: None)
    monkeypatch.setattr(vc.llm, "upload_video", lambda *a: up)
    monkeypatch.setattr(vc.llm, "delete_file", deleted.append)
    monkeypatch.setattr(vc.llm, "call", lambda *a, **k: (_ for _ in ()).throw(llm.CapExceeded("x")))
    with pytest.raises(llm.CapExceeded):
        vc.extract_claims_from_video(mp4(3), "video/mp4")
    assert deleted == [up]


def test_checkable_prefix():
    assert vc.checkable("WC-2 in 105A", "P101") == "On P101, WC-2 in 105A"
    assert vc.checkable("WC-2 in 105A") == "WC-2 in 105A"


# ---- llm helpers ------------------------------------------------------------------------------------------
def test_video_part_and_token_estimate():
    p = llm.video_part(b"x" * 10, "video/mp4", 10)
    assert p.inline_data.data == b"x" * 10 and p.video_metadata.end_offset == "10s"
    assert llm._est_input_tokens([p]) == pytest.approx(3000)
    nometa = llm.video_part(b"x" * 2_000_000, "video/mp4")
    assert llm._est_input_tokens([nometa]) == pytest.approx(2_000_000 / 20000 * 300)
    f = types.Part(file_data=types.FileData(file_uri="u", mime_type="video/mp4"), video_metadata=llm._video_meta(60))
    assert llm._est_input_tokens([f]) == pytest.approx(18000)
    assert llm.worst_case_usd("gemini-4-argon", [p], "LOW") > llm.worst_case_usd("gemini-4-argon", ["x"], "LOW")


class FakeFiles:
    def __init__(self, states):
        self.states, self.deleted, self.gets = list(states), [], 0

    def upload(self, file, config):
        self.up = (file.read(), config)
        return SimpleNamespace(state=SimpleNamespace(name="PROCESSING"), name="files/abc", uri="https://g/v1beta/files/abc")

    def get(self, name):
        self.gets += 1
        return SimpleNamespace(state=SimpleNamespace(name=self.states.pop(0)), name=name, uri="https://g/v1beta/files/abc")

    def delete(self, name):
        self.deleted.append(name)


def test_upload_video_polls_until_active(monkeypatch):
    files = FakeFiles(["PROCESSING", "ACTIVE"])
    monkeypatch.setattr(llm, "_get_client", lambda: SimpleNamespace(files=files))
    monkeypatch.setattr(llm, "_sleep", lambda s: None)
    p = llm.upload_video(b"vid", "video/mp4", 12)
    assert files.up == (b"vid", {"mime_type": "video/mp4"}) and files.gets == 2
    assert p.file_data.file_uri == "https://g/v1beta/files/abc" and p.video_metadata.end_offset == "12s"
    llm.delete_file(p)
    assert files.deleted == ["files/abc"]


def test_upload_video_failed_and_timeout(monkeypatch):
    monkeypatch.setattr(llm, "_sleep", lambda s: None)
    monkeypatch.setattr(llm, "_get_client", lambda: SimpleNamespace(files=FakeFiles(["FAILED"])))
    with pytest.raises(llm.LLMError, match="FAILED"):
        llm.upload_video(b"v", "video/mp4")
    monkeypatch.setattr(llm, "_get_client", lambda: SimpleNamespace(files=FakeFiles(["PROCESSING"] * 100)))
    with pytest.raises(llm.LLMError, match="still processing"):
        llm.upload_video(b"v", "video/mp4")


def test_delete_file_never_raises(monkeypatch):
    monkeypatch.setattr(llm, "_get_client", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    llm.delete_file(types.Part(file_data=types.FileData(file_uri="u", mime_type="video/mp4")))


# ---- endpoint ---------------------------------------------------------------------------------------------
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "BUILD", tmp_path)
    monkeypatch.setattr(vc, "_ffprobe_duration", lambda d: None)
    monkeypatch.setattr(llm, "run_spend", lambda rid=None: 0.0)
    calls = []

    def fake_check(claim):
        calls.append(claim)
        return {"claim": claim, "speaker": None, "timestamp": "2026-11-21T00:00:00Z", "verdict": "match",
                "confidence": 0.9, "evidence": [], "spec_refs": [], "checks": [], "reason": "r", "rfi_draft": None}

    monkeypatch.setattr(appmod.checker, "check", fake_check)
    return TestClient(appmod.app, raise_server_exceptions=False), tmp_path, calls


H = {"Content-Type": "video/mp4"}


def test_video_happy_path(env, monkeypatch):
    c, tmp, calls = env
    seen = {}
    claims = [{"claim": "WC-2 in 105A", "kind": "spoken", "t_start_s": 0.5, "t_end_s": 2.0, "quote_or_observation": "q"},
              {"claim": "Door swings out", "kind": "visual", "t_start_s": 3.0, "t_end_s": 4.0, "quote_or_observation": "o"}]

    def fake_extract(data, mime, hint, duration):
        seen.update(mime=mime, hint=hint, duration=duration)
        return claims

    monkeypatch.setattr(appmod.video_claims, "extract_claims_from_video", fake_extract)
    r = c.post("/api/video?sheet=P101", content=mp4(6), headers={"Content-Type": "video/quicktime; foo=bar"})
    assert r.status_code == 200
    body = r.json()
    assert seen["mime"] == "video/quicktime" and seen["hint"] == "P101" and seen["duration"] == pytest.approx(6)
    assert set(body) == {"claims", "cards", "video_s", "video_url"} and body["claims"] == claims and body["video_s"] == 6.0
    assert sorted(calls) == ["On P101, Door swings out", "On P101, WC-2 in 105A"]   # hint prefixed
    by = {x["claim"]: x for x in body["cards"]}
    d = by["On P101, Door swings out"]
    assert (d["source"], d["t_start_s"], d["kind"]) == ("video", 3.0, "visual")
    assert len((tmp / "cards.jsonl").read_text().splitlines()) == 2


def test_video_no_hint_no_prefix_and_empty(env, monkeypatch):
    c, _, calls = env
    monkeypatch.setattr(appmod.video_claims, "extract_claims_from_video",
                        lambda d, m, h, du: [{"claim": "A", "kind": "spoken", "t_start_s": 0, "t_end_s": 1,
                                              "quote_or_observation": "a"}])
    assert c.post("/api/video", content=mp4(3), headers=H).status_code == 200 and calls == ["A"]
    monkeypatch.setattr(appmod.video_claims, "extract_claims_from_video", lambda d, m, h, du: [])
    r = c.post("/api/video", content=mp4(3), headers=H).json()
    assert r["claims"] == [] and r["cards"] == [] and r["video_s"] == 3.0 and r["video_url"].startswith("/videos/")


def test_video_content_type_size_and_body_errors(env, monkeypatch):
    c, _, _ = env
    monkeypatch.setattr(appmod.video_claims, "extract_claims_from_video", lambda d, m, h, du: [])
    assert c.post("/api/video", content=mp4(2), headers={"Content-Type": "video/mp4;codecs=avc1"}).status_code == 200
    assert c.post("/api/video", content=mp4(2), headers={"Content-Type": "VIDEO/MP4"}).status_code == 200
    for bad in ("video/webm", "audio/wav", "application/octet-stream", "text/plain"):
        assert c.post("/api/video", content=b"abc", headers={"Content-Type": bad}).status_code == 415
    assert c.post("/api/video", content=b"abc").status_code == 415
    assert c.post("/api/video", content=b"", headers=H).status_code == 400
    assert c.post("/api/video", content=b"not an mp4", headers=H).status_code == 400
    assert c.post("/api/video?sheet=P101;rm", content=mp4(2), headers=H).status_code == 400
    monkeypatch.setattr(appmod, "MAX_VIDEO", 1000)
    assert c.post("/api/video", content=b"0" * 1001, headers=H).status_code == 413
    assert c.post("/api/video", content=mp4(2), headers=H).status_code == 200     # exactly under the limit


def test_video_streamed_body_over_limit_is_413(env, monkeypatch):
    c, _, _ = env
    monkeypatch.setattr(appmod, "MAX_VIDEO", 1000)
    r = c.post("/api/video", content=iter([b"0" * 600, b"0" * 600]), headers=H)   # chunked, no Content-Length
    assert r.status_code == 413


@pytest.mark.parametrize("exc", [llm.CapExceeded("cap AIzaSECRET Toilet 105A"), llm.CacheMiss("k"), llm.LLMError("x")])
def test_video_503_from_extract(env, monkeypatch, exc):
    c, tmp, _ = env
    monkeypatch.setattr(appmod.video_claims, "extract_claims_from_video",
                        lambda *a: (_ for _ in ()).throw(exc))
    r = c.post("/api/video", content=mp4(3), headers=H)
    assert r.status_code == 503 and r.json() == {"error": type(exc).__name__}
    assert "SECRET" not in r.text and "Traceback" not in r.text and not (tmp / "cards.jsonl").exists()


def test_video_503_from_check_and_api_error(env, monkeypatch):
    c, _, _ = env
    monkeypatch.setattr(appmod.video_claims, "extract_claims_from_video",
                        lambda d, m, h, du: [{"claim": "A", "kind": "spoken", "t_start_s": 0, "t_end_s": 1,
                                              "quote_or_observation": "a"}])
    monkeypatch.setattr(appmod.checker, "check", lambda claim: (_ for _ in ()).throw(llm.CapExceeded("x")))
    assert c.post("/api/video", content=mp4(3), headers=H).json() == {"error": "CapExceeded"}
    from google.genai import errors
    monkeypatch.setattr(appmod.video_claims, "extract_claims_from_video",
                        lambda *a: (_ for _ in ()).throw(errors.APIError(500, {"error": {"message": "secret"}})))
    r = c.post("/api/video", content=mp4(3), headers=H)
    assert r.status_code == 503 and r.json() == {"error": "APIError"} and "secret" not in r.text


def test_video_checks_run_at_most_3_at_once(env, monkeypatch):
    import threading
    import time
    c, _, _ = env
    live, peak, lock = [0], [0], threading.Lock()

    def slow(claim):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1
        return {"claim": claim, "evidence": []}

    monkeypatch.setattr(appmod.checker, "check", slow)
    monkeypatch.setattr(appmod.video_claims, "extract_claims_from_video",
                        lambda d, m, h, du: [{"claim": f"c{i}", "kind": "spoken", "t_start_s": 0, "t_end_s": 1,
                                              "quote_or_observation": "q"} for i in range(8)])
    assert len(c.post("/api/video", content=mp4(3), headers=H).json()["cards"]) == 8
    assert 1 < peak[0] <= 3
