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
    assert set(out[0]) == {"claim", "speaker", "t_start_s", "t_end_s", "quote"}
    assert (seen["model"], seen["thinking"], seen["purpose"], seen["schema"]) == (
        "gemini-4-hydrogen", "LOW", "audio:claims", ac.SCHEMA)
    assert seen["parts"][1] == ac.PROMPT
    assert "105A" in ac.PROMPT and '33"' in ac.PROMPT


def test_out_of_range_timestamps_dropped(monkeypatch):
    fake(monkeypatch, {"claims": [C(), C(a=1, b=99), C(a=-1, b=2), C(a=4, b=3)]})
    assert len(ac.extract_claims(wav_bytes(5))) == 1


def test_empty_for_chitchat(monkeypatch):
    fake(monkeypatch, {"claims": []})
    assert ac.extract_claims(wav_bytes(3)) == []


def test_bad_shapes_dropped(monkeypatch):
    fake(monkeypatch, {"claims": [C(claim="  "), C(quote=""), C(quote="lunch at noon"), {"claim": "x"}, "junk"]})
    assert ac.extract_claims(wav_bytes(5)) == []


def test_non_dict_json_is_empty(monkeypatch):
    fake(monkeypatch, None)
    assert ac.extract_claims(wav_bytes(5)) == []


def test_two_speakers_kept(monkeypatch):
    fake(monkeypatch, {"claims": [C(), C(claim="Knox box at 6'-0\" AFF", quote="knox box goes six feet",
                                         sp="Speaker 2", a=3, b=4.5)]})
    assert [c["speaker"] for c in ac.extract_claims(wav_bytes(5))] == ["Speaker 1", "Speaker 2"]


def test_non_wav_skips_duration(monkeypatch):
    fake(monkeypatch, {"claims": [C(a=0, b=500)]})
    assert len(ac.extract_claims(b"webm-bytes", mime="audio/webm")) == 1
