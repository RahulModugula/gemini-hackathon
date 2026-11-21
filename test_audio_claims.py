import io
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

import audio_claims as ac

AUDIO = Path(__file__).parent / "tests" / "audio"


def fake(monkeypatch, payload):
    seen = {}

    def call(model, parts, **kw):
        seen.update(model=model, parts=parts, **kw)
        return SimpleNamespace(json=payload)

    monkeypatch.setattr(ac.llm, "call", call)
    monkeypatch.setattr(ac.llm, "audio_part", lambda b, m: ("audio", m))
    return seen


def wav_bytes(secs, rate=16000):
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
        w.writeframes(b"\0\0" * int(secs * rate))
    return b.getvalue()


def C(claim="Toilet 105A water closets are WC-2", quote="toilet one-oh-five A are all WC-2", a=0.5, b=4.0,
      sp="Speaker 1"):
    return dict(claim=claim, speaker=sp, t_start_s=a, t_end_s=b, quote=quote)


def test_duration_path_and_bytes():
    assert ac.duration_s(wav_bytes(2.5)) == pytest.approx(2.5)
    assert ac.duration_s(AUDIO / "clip3.wav") == pytest.approx(2.35, abs=0.1)


def test_call_args_and_normalised_numbers_preserved(monkeypatch):
    seen = fake(monkeypatch, {"claims": [C(claim='Grab bar in toilet 109A is at 33"',
                                           quote="grab bar in toilet 109A at thirty-three inches")]})
    out = ac.extract_claims(wav_bytes(5))
    assert out[0]["claim"] == 'Grab bar in toilet 109A is at 33"'
    assert set(out[0]) == {"claim", "speaker", "t_start_s", "t_end_s", "quote", "unverified_quote"}
    assert out[0]["unverified_quote"] is False
    assert (seen["model"], seen["thinking"], seen["purpose"], seen["schema"]) == (
        "gemini-4-hydrogen", "LOW", "audio:claims", ac.SCHEMA)
    assert seen["parts"][1] == ac.PROMPT
    assert "105A" in ac.PROMPT and '33"' in ac.PROMPT


def test_timestamps_clamped_or_dropped_with_reasons(monkeypatch):
    fake(monkeypatch, {"claims": [C(), C(a=1, b=99), C(a=-1, b=2), C(a=4, b=3), C(a=30, b=31)]})
    out = ac.extract_claims(wav_bytes(5))
    assert [(c["t_start_s"], c["t_end_s"]) for c in out] == [(0.5, 4.0), (1.0, 5.0), (0.0, 2.0)]
    assert [d["reason"] for d in out.dropped] == ["end_before_start", "start_after_clip_end"]


def test_end_within_slack_kept_as_is(monkeypatch):
    fake(monkeypatch, {"claims": [C(a=1, b=5.8)]})
    assert ac.extract_claims(wav_bytes(5))[0]["t_end_s"] == 5.8


def test_ms_timestamps_rescaled(monkeypatch):
    fake(monkeypatch, {"claims": [C(a=0, b=3000), C(a=3500, b=4500)]})
    out = ac.extract_claims(wav_bytes(5))
    assert [(c["t_start_s"], c["t_end_s"]) for c in out] == [(0, 3.0), (3.5, 4.5)]
    assert out.meta["rescaled_ms"] is True and out.dropped == []


def test_empty_for_chitchat(monkeypatch):
    fake(monkeypatch, {"claims": []})
    assert ac.extract_claims(wav_bytes(3)) == []


def test_bad_shapes_dropped_with_reasons(monkeypatch):
    fake(monkeypatch, {"claims": [C(claim="  "), {"claim": "x"}, "junk"]})
    out = ac.extract_claims(wav_bytes(5))
    assert out == []
    assert [d["reason"] for d in out.dropped] == ["empty_claim", "bad_timestamps", "not_an_object"]


def test_normalised_claim_with_disjoint_quote_survives(monkeypatch):
    fake(monkeypatch, {"claims": [C(claim='Grab bar at 33"', quote="thirty three inches"),
                                  C(claim="Toilet 105A is WC-2", quote="one oh five a, w c dash two")]})
    out = ac.extract_claims(wav_bytes(5))
    assert [c["unverified_quote"] for c in out] == [False, False]


def test_stopword_only_overlap_flagged_not_dropped(monkeypatch):
    fake(monkeypatch, {"claims": [C(claim="Grab bar is at 33 inches", quote="lunch is at noon")]})
    out = ac.extract_claims(wav_bytes(5))
    assert len(out) == 1 and out[0]["unverified_quote"] is True and out.dropped == []


def test_normalize_tokens():
    assert ac.normalize_tokens("one-oh-five A") == ["105a"]
    assert ac.normalize_tokens("thirty-three inches") == ["33", "inch"]
    assert ac.normalize_tokens('33"') == ["33", "inch"]
    assert ac.normalize_tokens("W C dash two") == ["wc", "dash", "2"]
    assert ac.normalize_tokens("six feet") == ["6", "feet"]


def test_caps(monkeypatch):
    long = "x" * 500
    fake(monkeypatch, {"claims": [C(claim="toilet " + long, quote="toilet " + long, sp="S" * 99)] + [C()] * 12})
    out = ac.extract_claims(wav_bytes(5))
    assert len(out) == 10 and len(out[0]["claim"]) == 300 and len(out[0]["quote"]) == 300
    assert len(out[0]["speaker"]) == 40
    assert [d["reason"] for d in out.dropped] == ["over_limit"] * 3


def test_stereo_24bit_duration():
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(2), w.setsampwidth(3), w.setframerate(44100)
        w.writeframes(b"\0" * 6 * 44100 * 2)
    assert ac.duration_s(b.getvalue()) == pytest.approx(2.0)


def test_unreadable_wav_skips_duration_with_flag(monkeypatch):
    fake(monkeypatch, {"claims": [C(a=0, b=500)]})
    out = ac.extract_claims(b"RIFF not really a wav", mime="audio/x-wav")
    assert len(out) == 1 and out.meta["duration_checked"] is False and "duration_error" in out.meta


@pytest.mark.parametrize("mime", ["audio/wav", "audio/wave", "audio/x-wav"])
def test_wav_mimes_checked(monkeypatch, mime):
    fake(monkeypatch, {"claims": [C(a=40, b=50)]})
    out = ac.extract_claims(wav_bytes(5), mime=mime)
    assert out == [] and out.meta["duration_checked"] is True


def test_non_dict_json_is_empty(monkeypatch):
    fake(monkeypatch, None)
    assert ac.extract_claims(wav_bytes(5)) == []


def test_two_speakers_kept(monkeypatch):
    fake(monkeypatch, {"claims": [C(), C(claim="Knox box at 6'-0\" AFF", quote="knox box goes six feet",
                                         sp="Speaker 2", a=3, b=4.5)]})
    assert [c["speaker"] for c in ac.extract_claims(wav_bytes(5))] == ["Speaker 1", "Speaker 2"]


def test_non_wav_skips_duration(monkeypatch):
    fake(monkeypatch, {"claims": [C(a=0, b=500)]})
    out = ac.extract_claims(b"webm-bytes", mime="audio/webm")
    assert len(out) == 1 and out.meta["duration_checked"] is False
