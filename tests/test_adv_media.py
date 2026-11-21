"""Adversarial tests for audio_claims, video_claims and ingest. Tests only; no product code is changed.

Conventions: xfail(strict=True) with reason "BUG: ..." for a defect with a concrete failing input,
"GAP: ..." for an unsupported or unhandled case that is a limitation rather than a crash or a wrong answer.
"""
import io
import struct
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402

import audio_claims as ac  # noqa: E402
import ingest as ing  # noqa: E402
import video_claims as vc  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import llm  # noqa: E402

PDF = ROOT / ing.DEFAULT_PDF
HAVE_DRAWINGS = PDF.exists()
MB = 1024 * 1024


# =====================================================================================================
# helpers
# =====================================================================================================
def wav(rate=16000, ch=1, sw=2, frames=None, secs=None):
    b = io.BytesIO()
    if frames is None:
        frames = int((secs or 0) * rate)
    with wave.open(b, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(sw)
        w.setframerate(rate)
        w.writeframes(b"\0" * frames * ch * sw)
    return b.getvalue()


def raw_wav(declared_data, rate=16000, ch=1, sw=2, rate_field=None, body=b""):
    """Hand-built RIFF/WAVE header with arbitrary fmt/data fields."""
    fmt = struct.pack("<IHHIIHH", 16, 1, ch, rate if rate_field is None else rate_field,
                      rate * ch * sw, ch * sw, sw * 8)
    return (b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(body)) + b"WAVE" + b"fmt " + fmt
            + b"data" + struct.pack("<I", declared_data) + body)


def C(claim="Toilet 105A water closets are WC-2", quote="toilet one-oh-five A are all WC-2", a=0.5, b=4.0,
      speaker="Speaker 1"):
    return dict(claim=claim, speaker=speaker, t_start_s=a, t_end_s=b, quote=quote)


@pytest.fixture
def llm_audio(monkeypatch):
    """Mock llm.call / audio_part (no network). Set seen['payload'] before calling extract_claims."""
    seen = {"payload": {"claims": []}}

    def call(model, parts, **kw):
        seen.update(model=model, parts=parts, **kw)
        return SimpleNamespace(json=seen["payload"])

    monkeypatch.setattr(ac.llm, "call", call)
    monkeypatch.setattr(ac.llm, "audio_part", lambda b, m: ("audio", m))
    return seen


def box(typ, payload):
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


def mvhd(secs, scale=1000, version=0):
    if version == 0:
        body = struct.pack(">B3xIIII", 0, 0, 0, scale, int(secs * scale))
    else:
        body = struct.pack(">B3xQQIQ", 1, 0, 0, scale, int(secs * scale))
    return body + b"\0" * 80


def mp4(secs, scale=1000, version=0):
    return box(b"ftyp", b"isom\0\0\2\0isom") + box(b"free", b"xx") + box(b"moov", box(b"mvhd", mvhd(secs, scale, version)))


def V(claim="WC-2 in 105A", kind="spoken", a=0.5, b=2.0, q="toilet one-oh-five A is WC-2"):
    return dict(claim=claim, kind=kind, t_start_s=a, t_end_s=b, quote_or_observation=q)


@pytest.fixture
def llm_video(monkeypatch):
    seen = {"payload": {"claims": []}}

    def call(model, parts, **kw):
        seen.update(model=model, parts=parts, **kw)
        return SimpleNamespace(json=seen["payload"])

    monkeypatch.setattr(vc.llm, "call", call)
    monkeypatch.setattr(vc.llm, "video_part", lambda d, m, dur=None: ("inline", len(d), dur))
    monkeypatch.setattr(vc, "_ffprobe_duration", lambda d: None)
    return seen


def gray(page, dpi=72):
    return page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)


def ink(pix, bbox, dpi=72):
    """Count non-white gray pixels inside bbox (pt, rendered/top-left space).

    Threshold 250, not 128: small and light-gray legend text is anti-aliased to mid-gray, and a strict
    threshold made real, correctly placed words look inkless (checked on drawing page 4)."""
    r = (pymupdf.Rect(*bbox) * (dpi / 72)) & pymupdf.Rect(0, 0, pix.width, pix.height)
    ir = r.irect
    if ir.is_empty or ir.width == 0 or ir.height == 0:
        return 0
    samples, stride = pix.samples, pix.stride  # 1 byte per pixel (DeviceGray)
    return sum(1 for y in range(ir.y0, ir.y1)
               for v in samples[y * stride + ir.x0: y * stride + ir.x1] if v < 250)


def assert_tiles_cover(sheet, step=37.0):
    w, h = sheet["width"], sheet["height"]
    tiles = sheet["tiles"]
    assert tiles, "sheet must have tiles"
    for t in tiles:
        x0, y0, x1, y1 = t["bbox_pt"]
        assert -1e-6 <= x0 < x1 <= w + 1e-6 and -1e-6 <= y0 < y1 <= h + 1e-6, t
    x = 0.0
    while x <= w:
        y = 0.0
        while y <= h:
            px, py = min(x, w), min(y, h)
            assert any(t["bbox_pt"][0] - 1e-6 <= px <= t["bbox_pt"][2] + 1e-6
                       and t["bbox_pt"][1] - 1e-6 <= py <= t["bbox_pt"][3] + 1e-6
                       for t in tiles), f"uncovered point ({px}, {py})"
            y += step
        x += step
    assert max(t["bbox_pt"][2] for t in tiles) == pytest.approx(w, abs=1e-6)
    assert max(t["bbox_pt"][3] for t in tiles) == pytest.approx(h, abs=1e-6)


def make_pdf(path, rotation=0, rotated_text=False, text=True, size=(1224, 792), drawing_only=False):
    doc = pymupdf.open()
    page = doc.new_page(width=size[0], height=size[1])
    if text:
        page.insert_text((100, 200), "PLAIN WORD", fontsize=24)
    if rotated_text:
        page.insert_text((400, 500), "ROTATED", fontsize=24, rotate=90)
    if drawing_only:
        page.draw_rect(pymupdf.Rect(100, 100, 300, 300), color=(0, 0, 0), fill=(0.5, 0.5, 0.5))
    page.set_rotation(rotation)
    doc.save(str(path))
    doc.close()
    return path


# =====================================================================================================
# audio_claims
# =====================================================================================================
@pytest.mark.parametrize("ch,sw,rate", [(c, s, r) for r in (8000, 16000, 44100, 48000)
                                        for c in (1, 2) for s in (1, 2, 3, 4)])
def test_wav_matrix_duration_and_claim_bounds(llm_audio, ch, sw, rate):
    data = wav(rate=rate, ch=ch, sw=sw, secs=1.5)
    assert ac.duration_s(data) == pytest.approx(1.5)
    llm_audio["payload"] = {"claims": [C(a=0.2, b=1.2), C(a=1.6, b=1.7)]}
    out = ac.extract_claims(data)
    assert out.meta["duration_checked"] is True
    assert [(c["t_start_s"], c["t_end_s"]) for c in out] == [(0.2, 1.2)]
    assert [d["reason"] for d in out.dropped] == ["start_after_clip_end"]


def test_zero_length_wav_has_zero_duration(llm_audio):
    assert ac.duration_s(wav(secs=0)) == 0.0
    llm_audio["payload"] = {"claims": [C(a=0, b=0)]}
    out = ac.extract_claims(wav(secs=0))
    assert out.meta["duration_checked"] is True and out.meta["duration_s"] == 0.0


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="BUG: zero-length WAV (0.0 s) still accepts a claim: "
                          "claim t=0.0..0.5 on a 0-frame file is kept with t_end clamped to 0.0")
def test_zero_length_wav_rejects_claims(llm_audio):
    llm_audio["payload"] = {"claims": [C(a=0, b=0.5)]}
    out = ac.extract_claims(wav(secs=0))
    assert out == [], "nothing can be grounded in a 0-frame clip"


def test_one_frame_wav(llm_audio):
    data = wav(frames=1, rate=16000)
    assert ac.duration_s(data) == pytest.approx(1 / 16000)
    llm_audio["payload"] = {"claims": [C(a=0, b=0.00001), C(a=0.5, b=0.6)]}
    out = ac.extract_claims(data)
    assert [c["t_start_s"] for c in out] == [0.0]
    assert [d["reason"] for d in out.dropped] == ["start_after_clip_end"]


@pytest.mark.parametrize("cut", [0, 4, 11, 20, 30, 36])
def test_truncated_wav_header_is_flagged_not_raised(llm_audio, cut):
    data = wav(secs=0.5)[:cut]
    llm_audio["payload"] = {"claims": [C(a=0, b=0.1)]}
    out = ac.extract_claims(data)
    assert out.meta["duration_checked"] is False and "duration_error" in out.meta
    assert len(out) == 1


def test_random_bytes_labelled_wav_is_flagged(llm_audio):
    llm_audio["payload"] = {"claims": [C(a=0, b=0.5)]}
    out = ac.extract_claims(bytes(range(256)) * 8, mime="audio/wav")
    assert out.meta["duration_checked"] is False and "duration_error" in out.meta


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="GAP: with an unreadable WAV there is no duration bound, so a claim at t=500 s "
                          "on a clip of unknown length is accepted")
def test_unreadable_wav_still_bounds_timestamps(llm_audio):
    llm_audio["payload"] = {"claims": [C(a=0, b=500)]}
    out = ac.extract_claims(b"RIFF\x00\x00\x00\x00WAVEjunk", mime="audio/wav")
    assert out == []


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="BUG: duration trusts the declared data-chunk size: a 64-byte file declaring "
                          "4 GiB of data reports ~134217 s, so a claim at 1000-1001 s is accepted")
def test_huge_declared_nframes_bounded_by_real_bytes(llm_audio):
    data = raw_wav(declared_data=0xFFFFFF00, body=b"\0" * 64)
    llm_audio["payload"] = {"claims": [C(a=1000, b=1001)]}
    out = ac.extract_claims(data)
    assert out == [], f"64-byte file cannot hold 1000 s of audio: {out.meta}"


def test_huge_declared_nframes_is_reported_in_meta(llm_audio):
    data = raw_wav(declared_data=0xFFFFFF00, body=b"\0" * 64)
    llm_audio["payload"] = {"claims": []}
    out = ac.extract_claims(data)
    assert out.meta["duration_checked"] is True and out.meta["duration_s"] > 100000


@pytest.mark.xfail(strict=True, raises=ZeroDivisionError,
                   reason="BUG: WAV with fmt sample-rate 0 passes wave.open, then duration_s divides by zero; "
                          "extract_claims only catches wave.Error/EOFError so the request crashes")
def test_zero_sample_rate_wav_does_not_crash(llm_audio):
    llm_audio["payload"] = {"claims": [C(a=0, b=0.1)]}
    out = ac.extract_claims(raw_wav(declared_data=0, rate_field=0))
    assert out.meta["duration_checked"] is False


@pytest.mark.parametrize("ch,sw", [(0, 2), (1, 0)])
def test_invalid_fmt_fields_are_flagged(llm_audio, ch, sw):
    llm_audio["payload"] = {"claims": [C(a=0, b=0.1)]}
    out = ac.extract_claims(raw_wav(declared_data=0, ch=ch, sw=sw))
    assert out.meta["duration_checked"] is False and "duration_error" in out.meta


def test_claim_cap_keeps_first_ten_of_fifty(llm_audio):
    llm_audio["payload"] = {"claims": [C(claim=f"Toilet {i}A is WC-2", quote=f"toilet {i} a") for i in range(50)]}
    out = ac.extract_claims(wav(secs=5))
    assert len(out) == 10 and out[0]["claim"] == "Toilet 0A is WC-2" and out[-1]["claim"] == "Toilet 9A is WC-2"
    assert [d["reason"] for d in out.dropped] == ["over_limit"] * 40


def test_10k_char_claim_and_quote_capped(llm_audio):
    big = "toilet " * 1430
    llm_audio["payload"] = {"claims": [C(claim=big, quote=big)]}
    out = ac.extract_claims(wav(secs=5))
    assert len(out[0]["claim"]) == 300 and len(out[0]["quote"]) == 300


def test_speaker_label_capped_and_defaulted(llm_audio):
    llm_audio["payload"] = {"claims": [C(speaker="S" * 10000), C(speaker="   \n\t "), C(speaker=None)]}
    out = ac.extract_claims(wav(secs=5))
    assert [len(c["speaker"]) for c in out] == [40, 9, 9]
    assert out[1]["speaker"] == "Speaker 1" and out[2]["speaker"] == "Speaker 1"


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="GAP: speaker labels pass control characters through unchanged "
                          "(the module does not normalise speaker text; the UI does not render it today)")
def test_speaker_control_characters_stripped(llm_audio):
    llm_audio["payload"] = {"claims": [C(speaker="Bob\x00\x07\x1b[31m\nSpeaker 2")]}
    out = ac.extract_claims(wav(secs=5))
    assert all(ord(ch) >= 0x20 for ch in out[0]["speaker"])


def test_speaker_markup_is_kept_verbatim_and_json_safe(llm_audio):
    import json
    evil = '<img src=x onerror="alert(1)">'
    llm_audio["payload"] = {"claims": [C(speaker=evil)]}
    out = ac.extract_claims(wav(secs=5))
    assert out[0]["speaker"] == evil[:40]
    json.dumps(out[0])


@pytest.mark.parametrize("claim,quote", [
    ("Унитаз в санузле 105A", "унитаз в санузле сто пять A"),
    ("厕所 WC-2 在 105A", "厕所 WC 2 在 105A"),
    ("حمام 105A", "حمام ١٠٥ A"),
    ("トイレ 105A", "トイレ 105 A"),
])
def test_other_script_claims_round_trip(llm_audio, claim, quote):
    llm_audio["payload"] = {"claims": [C(claim=claim, quote=quote)]}
    out = ac.extract_claims(wav(secs=5))
    assert out[0]["claim"] == claim and out[0]["quote"] == quote


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="BUG: unverified_quote only sees ASCII tokens, so an identical non-Latin claim and "
                          "quote is flagged unverified (claim 'Унитаз в санузле', quote 'унитаз в санузле')")
def test_identical_non_latin_claim_and_quote_are_verified(llm_audio):
    llm_audio["payload"] = {"claims": [C(claim="Унитаз в санузле", quote="унитаз в санузле")]}
    out = ac.extract_claims(wav(secs=5))
    assert out[0]["unverified_quote"] is False


def test_claim_and_quote_missing_fields_are_handled(llm_audio):
    llm_audio["payload"] = {"claims": [{"claim": "Toilet 105A", "speaker": "Speaker 1",
                                        "t_start_s": 0, "t_end_s": 1}]}
    out = ac.extract_claims(wav(secs=5))
    assert out[0]["quote"] == "" and out[0]["unverified_quote"] is True


@pytest.mark.parametrize("bad", [
    {"t_start_s": float("nan"), "t_end_s": 1},
    {"t_start_s": 0, "t_end_s": float("nan")},
    {"t_start_s": float("inf"), "t_end_s": float("inf")},
    {"t_start_s": "abc", "t_end_s": 1},
    {"t_start_s": "1e400", "t_end_s": 2},
    {"t_start_s": None, "t_end_s": 1},
])
def test_non_finite_or_non_numeric_timestamps_dropped(llm_audio, bad):
    llm_audio["payload"] = {"claims": [dict(C(), **bad)]}
    out = ac.extract_claims(wav(secs=5))
    assert out == [] and out.dropped[0]["reason"] == "bad_timestamps"


def test_string_timestamps_coerced_when_numeric(llm_audio):
    llm_audio["payload"] = {"claims": [dict(C(), t_start_s="1.5", t_end_s="2.5")]}
    out = ac.extract_claims(wav(secs=5))
    assert (out[0]["t_start_s"], out[0]["t_end_s"]) == (1.5, 2.5)


def test_end_before_start_and_both_negative(llm_audio):
    llm_audio["payload"] = {"claims": [C(a=-5, b=-3), C(a=3, b=2.9)]}
    out = ac.extract_claims(wav(secs=5))
    assert out == []
    assert [d["reason"] for d in out.dropped] == ["end_before_start", "end_before_start"]


def test_overlapping_claims_are_all_kept(llm_audio):
    llm_audio["payload"] = {"claims": [C(claim="Toilet 105A is WC-2", a=1, b=3),
                                       C(claim="Grab bar is at 33 inch", quote="grab bar thirty three inches", a=2, b=4),
                                       C(claim="Toilet 105A is WC-2", a=1, b=3)]}
    out = ac.extract_claims(wav(secs=5))
    assert len(out) == 3


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="GAP: ms-scale rescale is a heuristic: on a 2 s clip, a seconds claim at 25-27 s "
                          "is rescaled to 0.025-0.027 s")
def test_seconds_claim_past_clip_not_mistaken_for_ms(llm_audio):
    llm_audio["payload"] = {"claims": [C(a=25, b=27)]}
    out = ac.extract_claims(wav(secs=2))
    assert out == [] or out[0]["t_start_s"] >= 2.0


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="GAP: ms rescale is all-or-nothing; in a mixed batch the 3000 ms claim is clamped to "
                          "the clip end (5.0 s) instead of rescaled to 3.0 s")
def test_mixed_ms_and_seconds_batch(llm_audio):
    llm_audio["payload"] = {"claims": [C(a=0, b=3000), C(a=1, b=2)]}
    out = ac.extract_claims(wav(secs=5))
    assert (out[0]["t_start_s"], out[0]["t_end_s"]) == (0.0, 3.0)


def test_ms_batch_rescaled_when_every_claim_is_out_of_range(llm_audio):
    llm_audio["payload"] = {"claims": [C(a=0, b=3000), C(a=3500, b=4500)]}
    out = ac.extract_claims(wav(secs=5))
    assert [(c["t_start_s"], c["t_end_s"]) for c in out] == [(0, 3.0), (3.5, 4.5)]


@pytest.mark.parametrize("payload", [{"claims": None}, {"claims": "x"}, {"claims": {"a": 1}}, [], "junk", None])
def test_malformed_model_payload_is_empty(llm_audio, payload):
    llm_audio["payload"] = payload
    assert ac.extract_claims(wav(secs=2)) == []


NUMBER_CASES = [
    ("one oh five a", ["105a"], None),
    ("thirty three inches", ["33", "inch"], None),
    ("six foot", ["6", "feet"], None),
    ("W C two", ["wc", "2"], None),
    ("one-oh-five A", ["105a"], None),
    ("a hundred and five", ["105"],
     "GAP: 'hundred' is not parsed, so 'a hundred and five' normalises to ['a', 'hundred', 'and', '5'] not 105"),
    ("one hundred five", ["105"], "GAP: 'hundred' is not parsed, so 'one hundred five' gives ['100', '5'] not 105"),
]


@pytest.mark.parametrize("text,expected,gap", [
    pytest.param(t, e, None, marks=pytest.mark.xfail(strict=True, raises=AssertionError, reason=g)) if g else (t, e, None)
    for t, e, g in NUMBER_CASES
])
def test_number_normalisation_table(text, expected, gap):
    assert ac.normalize_tokens(text) == expected


# =====================================================================================================
# video_claims: duration (mvhd parser)
# =====================================================================================================
@pytest.mark.parametrize("version,scale,secs", [(0, 1, 3.0), (0, 1000, 0.5), (0, 90000, 7.25),
                                                (1, 600, 7.5), (1, 90000, 120.125)])
def test_mvhd_v0_v1_exact(version, scale, secs):
    assert vc._mvhd_duration(mp4(secs, scale, version)) == pytest.approx(secs)


def test_missing_moov_returns_none():
    assert vc._mvhd_duration(box(b"ftyp", b"isom") + box(b"free", b"x" * 40)) is None


@pytest.mark.parametrize("cut", range(0, 64, 3))
def test_truncated_mp4_never_yields_a_duration(cut):
    full = mp4(9)
    assert vc._mvhd_duration(full[:cut]) is None


def test_full_mp4_is_parsed_so_truncation_test_is_meaningful():
    assert vc._mvhd_duration(mp4(9)) == pytest.approx(9)


def test_size_one_extended_box_header():
    payload = mvhd(4, 1000, 0)
    moov = struct.pack(">I4sQ", 1, b"moov", 16 + 8 + len(payload)) + box(b"mvhd", payload)
    assert vc._mvhd_duration(moov) == pytest.approx(4)


def test_size_zero_box_extends_to_end_of_file():
    payload = mvhd(4, 1000, 0)
    moov = struct.pack(">I4s", 0, b"moov") + box(b"mvhd", payload)
    assert vc._mvhd_duration(moov) == pytest.approx(4)


@pytest.mark.parametrize("bad_size", [0xFFFFFFFF, 4, 7, 1 << 31])
def test_giant_or_undersized_box_fails_closed(bad_size):
    data = struct.pack(">I4s", bad_size, b"free") + mp4(2)
    assert vc._mvhd_duration(data) is None


def test_giant_box_declared_before_moov_hides_it():
    """Fails closed (None), so the request is rejected rather than mis-timed."""
    assert vc._mvhd_duration(struct.pack(">I4s", 0xFFFFFFFF, b"mdat") + mp4(2)) is None


def test_timescale_zero_does_not_divide_by_zero(monkeypatch, llm_video):
    data = mp4(3, scale=0)
    assert vc._mvhd_duration(data) is None
    assert vc.video_duration_s(data) is None
    with pytest.raises(ValueError, match="could not read video duration"):
        vc.extract_claims_from_video(data, "video/mp4")


def test_zero_duration_is_unreadable(llm_video):
    assert vc.video_duration_s(mp4(0)) is None


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="GAP: an implausible mvhd duration (2^64-1 s at timescale 1) is accepted as the video "
                          "length and sent to the model as an end offset")
def test_duration_overflow_rejected(llm_video):
    data = box(b"moov", box(b"mvhd", struct.pack(">B3xQQIQ", 1, 0, 0, 1, 2 ** 64 - 1) + b"\0" * 80))
    assert vc.video_duration_s(data) is None


def test_duration_overflow_is_finite_and_does_not_crash(llm_video):
    data = box(b"moov", box(b"mvhd", struct.pack(">B3xQQIQ", 1, 0, 0, 1, 2 ** 64 - 1) + b"\0" * 80))
    d = vc.video_duration_s(data)
    assert d is not None and d == pytest.approx(2 ** 64 - 1) and d < float("inf")


# =====================================================================================================
# video_claims: routing and validation
# =====================================================================================================
def test_inline_at_exactly_20_mib(llm_video, monkeypatch):
    log = []
    monkeypatch.setattr(vc.llm, "upload_video", lambda *a, **k: log.append("upload") or None)
    data = mp4(5) + b"\0" * (vc.MAX_INLINE - len(mp4(5)))
    assert len(data) == 20 * MB
    llm_video["payload"] = {"claims": [V()]}
    vc.extract_claims_from_video(data, "video/mp4", duration=5)
    assert log == [] and llm_video["parts"][0][0] == "inline"


def test_files_api_at_20_mib_plus_one(llm_video, monkeypatch):
    from google.genai import types
    up = types.Part(file_data=types.FileData(file_uri="https://g/v1beta/files/x", mime_type="video/mp4"))
    log = []
    monkeypatch.setattr(vc.llm, "upload_video", lambda d, m, dur: log.append(len(d)) or up)
    monkeypatch.setattr(vc.llm, "delete_file", lambda p: log.append("deleted"))
    data = mp4(5) + b"\0" * (vc.MAX_INLINE + 1 - len(mp4(5)))
    assert len(data) == 20 * MB + 1
    llm_video["payload"] = {"claims": [V()]}
    vc.extract_claims_from_video(data, "video/mp4", duration=5)
    assert log == [len(data), "deleted"] and llm_video["parts"][0] is up


def test_upload_deleted_after_successful_call(llm_video, monkeypatch):
    from google.genai import types
    up = types.Part(file_data=types.FileData(file_uri="u", mime_type="video/mp4"))
    deleted = []
    monkeypatch.setattr(vc, "MAX_INLINE", 1)
    monkeypatch.setattr(vc.llm, "upload_video", lambda *a, **k: up)
    monkeypatch.setattr(vc.llm, "delete_file", deleted.append)
    llm_video["payload"] = {"claims": [V()]}
    vc.extract_claims_from_video(mp4(3), "video/mp4")
    assert deleted == [up]


@pytest.mark.xfail(strict=True, raises=AttributeError,
                   reason="BUG: a non-string claim (model returns 42) passes _valid via str() but then "
                          "c['claim'].strip() raises AttributeError, failing the whole request instead of "
                          "dropping that one claim")
def test_non_string_claim_is_dropped_not_crashing(llm_video):
    llm_video["payload"] = {"claims": [V(claim=42), V(claim="WC-2 in 105A")]}
    out = vc.extract_claims_from_video(mp4(5), "video/mp4")
    assert [c["claim"] for c in out] == ["WC-2 in 105A"]


@pytest.mark.xfail(strict=True, raises=AttributeError,
                   reason="BUG: a list-valued quote_or_observation passes _valid via str() then crashes on .strip()")
def test_non_string_observation_is_dropped_not_crashing(llm_video):
    llm_video["payload"] = {"claims": [V(q=["tag"]), V(claim="WC-2 in 105A")]}
    out = vc.extract_claims_from_video(mp4(5), "video/mp4")
    assert [c["claim"] for c in out] == ["WC-2 in 105A"]


@pytest.mark.parametrize("bad", [
    dict(a=float("nan"), b=1.0),
    dict(a=0.0, b=float("inf")),
    dict(a=-1.0, b=1.0),
    dict(a=3.0, b=2.0),
    dict(a=0.0, b=99.0),
    dict(kind="other"),
    dict(claim="   "),
    dict(q=""),
])
def test_invalid_claim_items_dropped(llm_video, bad):
    llm_video["payload"] = {"claims": [V(**bad), V(claim="ok")]}
    out = vc.extract_claims_from_video(mp4(5), "video/mp4")
    assert [c["claim"] for c in out] == ["ok"]


def test_end_within_half_second_slack_is_kept(llm_video):
    llm_video["payload"] = {"claims": [V(a=4.0, b=5.4)]}
    assert len(vc.extract_claims_from_video(mp4(5), "video/mp4")) == 1


def test_visual_cap_and_total_cap_together(llm_video):
    llm_video["payload"] = {"claims": [V(kind="visual", claim=f"v{i}") for i in range(5)]
                            + [V(claim=f"s{i}") for i in range(20)]}
    out = vc.extract_claims_from_video(mp4(5), "video/mp4")
    assert len(out) == 10 and sum(c["kind"] == "visual" for c in out) == 3


def test_long_claim_text_is_not_truncated_here(llm_video):
    """Video claims have no length cap in this module (audio does); document that it is passed through."""
    long = "x" * 5000
    llm_video["payload"] = {"claims": [V(claim=long, q=long)]}
    assert vc.extract_claims_from_video(mp4(5), "video/mp4")[0]["claim"] == long


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="GAP: sheet_hint is validated only in app.py; extract_claims_from_video interpolates "
                          "any string into the prompt, e.g. '<script>' or '../etc'")
def test_sheet_hint_validated_inside_extractor(llm_video):
    vc.extract_claims_from_video(mp4(5), "video/mp4", "<script>alert(1)</script>")
    assert "<script>" not in llm_video["parts"][1]


def test_sheet_hint_reaches_prompt_verbatim_when_valid(llm_video):
    vc.extract_claims_from_video(mp4(5), "video/mp4", "P101")
    assert llm_video["parts"][1].endswith("drawing sheet P101.")


# =====================================================================================================
# video_claims: sheet hint sanitisation (SHEET_RE + endpoint)
# =====================================================================================================
@pytest.mark.parametrize("hint,ok", [
    ("P101", True), ("A" * 32, True), ("A" * 33, False), ("../etc", False), ("<script>", False),
    ("P101 ", False), (" P101", False), ("", False), ("P/101", False), ("P101;rm", False),
    ("-P101", False), (".P101", False), ("P101‮", False), ("a.b_c-d", True),
])
def test_sheet_regex(hint, ok):
    assert bool(vc.SHEET_RE.match(hint)) is ok


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="BUG: SHEET_RE ends in '$', which also matches before a trailing newline, so "
                          "'P101\\n' passes the validator. app.py strips first so /api/video is safe today, "
                          "but any other caller of the regex is not")
def test_sheet_regex_rejects_trailing_newline():
    assert vc.SHEET_RE.match("P101\n") is None


@pytest.fixture
def video_env(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "BUILD", tmp_path)
    monkeypatch.setattr(vc, "_ffprobe_duration", lambda d: None)
    monkeypatch.setattr(llm, "run_spend", lambda rid=None: 0.0)
    hints = []
    monkeypatch.setattr(appmod.video_claims, "extract_claims_from_video",
                        lambda d, m, h, du: hints.append(h) or [])
    return TestClient(appmod.app, raise_server_exceptions=False), hints


@pytest.mark.parametrize("sheet,status", [
    ("P101", 200), ("A" * 32, 200), ("A" * 33, 400), ("../etc", 400), ("<script>", 400),
    ("P101 ", 200),  # stripped by the endpoint, so this is accepted as P101
    ("P101;rm", 400), ("\x00P101", 400),
])
def test_endpoint_sheet_hint_sanitisation(video_env, sheet, status):
    c, hints = video_env
    r = c.post("/api/video", params={"sheet": sheet}, content=mp4(3),
               headers={"Content-Type": "video/mp4"})
    assert r.status_code == status, r.text
    if status == 200:
        assert hints == [sheet.strip()]
    else:
        assert hints == [] and "error" in r.json()


# =====================================================================================================
# ingest
# =====================================================================================================
@pytest.mark.skipif(not HAVE_DRAWINGS, reason="drawings not downloaded")
@pytest.mark.parametrize("page", [1, 4, 13])
def test_real_drawing_words_inside_page(tmp_path, page):
    (s,) = ing.ingest({"dwg": (str(PDF), 0)}, tmp_path, pages={page}, dpi=36)
    import json
    wf = json.loads((tmp_path / s["words_file"]).read_text())
    assert s["has_text_layer"] and wf["words"]
    assert len(wf["words"]) == s["word_count"]
    for w in wf["words"]:
        x0, y0, x1, y1 = w["bbox"]
        assert 0 <= x0 <= x1 <= wf["width"] and 0 <= y0 <= y1 <= wf["height"], w


@pytest.mark.skipif(not HAVE_DRAWINGS, reason="drawings not downloaded")
@pytest.mark.parametrize("page", [1, 4, 13])
def test_real_drawing_tiles_cover_page_and_are_nonempty(tmp_path, page):
    (s,) = ing.ingest({"dwg": (str(PDF), 0)}, tmp_path, pages={page}, dpi=36)
    assert_tiles_cover(s)
    for t in s["tiles"]:
        f = tmp_path / t["file"]
        assert f.stat().st_size > 1000
        assert f.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.skipif(not HAVE_DRAWINGS, reason="drawings not downloaded")
def test_real_rotated_page_words_match_rendered_ink(tmp_path):
    """Page 4 is /Rotate 270: every word box must sit on dark pixels of the rendered (rotated) page."""
    (s,) = ing.ingest({"dwg": (str(PDF), 0)}, tmp_path, pages={4}, dpi=36)
    import json
    wf = json.loads((tmp_path / s["words_file"]).read_text())
    doc = pymupdf.open(str(PDF))
    page = doc[3]
    assert page.rotation == 270
    pix = gray(page, 72)
    misses = [w["text"] for w in wf["words"] if ink(pix, w["bbox"], dpi=72) == 0]
    assert misses == [], f"{len(misses)} word boxes with no ink: {misses[:10]}"


@pytest.mark.skipif(not HAVE_DRAWINGS, reason="drawings not downloaded")
def test_real_drawing_sheet_index_marks_text_layer(tmp_path):
    sheets = ing.ingest({"dwg": (str(PDF), 0)}, tmp_path, pages={1, 4, 13}, dpi=36)
    import json
    idx = json.loads((tmp_path / "sheets.json").read_text())
    assert [x["sheet"] for x in idx["sheets"]] == [s["sheet"] for s in sheets] == ["dwg-p001", "dwg-p004", "dwg-p013"]
    assert all(x["has_text_layer"] == (x["text_length"] > 0) for x in idx["sheets"])


def test_missing_pdf_listed_while_present_source_still_ingests(tmp_path):
    good = make_pdf(tmp_path / "good.pdf")
    sheets = ing.ingest({"gone": (str(tmp_path / "nope.pdf"), 0), "ok": (str(good), 1)}, tmp_path / "out")
    import json
    idx = json.loads((tmp_path / "out" / "sheets.json").read_text())
    assert idx["sources"][0] == {"source": "gone", "path": str(tmp_path / "nope.pdf"), "error": "missing"}
    assert idx["sources"][1]["sheets"] == 1
    assert [s["sheet"] for s in sheets] == ["ok-p001"]


def test_missing_pdf_default_source_is_reported(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ing.ingest(None, tmp_path / "out", pages={1})
    import json
    idx = json.loads((tmp_path / "out" / "sheets.json").read_text())
    assert {s["source"] for s in idx["sources"]} == {"dwg", "add1", "add2"}
    assert all(s.get("error") == "missing" for s in idx["sources"]), idx["sources"]


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_synthetic_rotated_text_boxes_match_rendered_ink(tmp_path, rotation):
    pdf = make_pdf(tmp_path / "r.pdf", rotation=rotation, rotated_text=True)
    (s,) = ing.ingest({"t": (str(pdf), 0)}, tmp_path / "out", dpi=72)
    import json
    wf = json.loads((tmp_path / "out" / s["words_file"]).read_text())
    texts = [w["text"] for w in wf["words"]]
    assert "PLAIN" in texts and "WORD" in texts and "ROTATED" in texts
    doc = pymupdf.open(str(pdf))
    pix = gray(doc[0], 72)
    for w in wf["words"]:
        x0, y0, x1, y1 = w["bbox"]
        assert 0 <= x0 <= x1 <= wf["width"] + 1e-6 and 0 <= y0 <= y1 <= wf["height"] + 1e-6, w
        assert ink(pix, w["bbox"], dpi=72) > 0, w
    assert s["rotation"] == rotation
    assert_tiles_cover(s)


def test_synthetic_270_page_dimensions_are_rendered_space(tmp_path):
    pdf = make_pdf(tmp_path / "r270.pdf", rotation=270)
    (s,) = ing.ingest({"t": (str(pdf), 0)}, tmp_path / "out", dpi=72)
    assert (s["width"], s["height"]) == (792, 1224)
    assert_tiles_cover(s)


def test_synthetic_no_text_layer(tmp_path):
    pdf = make_pdf(tmp_path / "vec.pdf", text=False, drawing_only=True)
    (s,) = ing.ingest({"t": (str(pdf), 0)}, tmp_path / "out", dpi=72)
    assert s["has_text_layer"] is False and s["word_count"] == 0 and s["text_length"] == 0
    import json
    wf = json.loads((tmp_path / "out" / s["words_file"]).read_text())
    assert wf["words"] == []
    assert all((tmp_path / "out" / t["file"]).stat().st_size > 0 for t in s["tiles"])
    assert_tiles_cover(s)


def test_small_letter_pages_are_skipped_and_reported(tmp_path):
    pdf = make_pdf(tmp_path / "letter.pdf", size=(612, 792))
    sheets, skipped = ing.ingest_pdf(str(pdf), "t", 0, tmp_path / "out")
    assert sheets == [] and skipped == [1]


def test_pages_filter_beyond_document_length_is_empty(tmp_path):
    pdf = make_pdf(tmp_path / "one.pdf")
    assert ing.ingest({"t": (str(pdf), 0)}, tmp_path / "out", pages={5, 99}) == []


def test_sheet_ids_unique_across_sources_and_padded():
    ids = {ing.sheet_id(t, i) for t in ("dwg", "add1", "add2") for i in range(60)}
    assert len(ids) == 180 and ing.sheet_id("dwg", 0) == "dwg-p001" and ing.sheet_id("dwg", 999) == "dwg-p1000"
