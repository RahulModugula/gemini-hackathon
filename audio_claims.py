"""T9: audio clip -> checkable claims about the drawings (speaker + approximate timestamps)."""
import io
import re
import wave

import llm

MODEL = "gemini-4-hydrogen"

SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "speaker": {"type": "string"},
                    "t_start_s": {"type": "number"},
                    "t_end_s": {"type": "number"},
                    "quote": {"type": "string"},
                },
                "required": ["claim", "speaker", "t_start_s", "t_end_s", "quote"],
            },
        }
    },
    "required": ["claims"],
}

PROMPT = (
    "You hear a short clip from a construction site meeting. Extract atomic, checkable claims about the "
    "construction drawings only (rooms, fixtures, dimensions, mounting heights, types, quantities, notes). "
    "One fact per claim. Normalise spoken numbers and room names: 'one-oh-five A' -> '105A', "
    "'thirty-three inches' -> 33\", 'six feet' -> 6'-0\", 'W C dash two' -> 'WC-2'. "
    "Keep the speaker label (Speaker 1, Speaker 2, ...; a different voice is a different speaker) and "
    "approximate start/end timestamps in seconds from the start of the clip. "
    "'quote' is the words spoken, as heard. Ignore greetings, scheduling, small talk and anything that is not a "
    "claim about the drawings. A question that asserts a fact ('... are all WC-2, right?') counts as a claim. "
    'Return {"claims": []} if there are none.'
)

_TOK = re.compile(r"[a-z0-9]+")


def _tokens(s):
    return set(_TOK.findall(str(s).lower()))


def duration_s(wav):
    """Duration in seconds of a wav file path or bytes."""
    f = io.BytesIO(wav) if isinstance(wav, (bytes, bytearray)) else str(wav)
    with wave.open(f, "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _valid(c, dur, slack=0.5):
    if not isinstance(c, dict):
        return False
    claim, quote = str(c.get("claim") or "").strip(), str(c.get("quote") or "").strip()
    if not claim or not quote:
        return False
    try:
        a, b = float(c["t_start_s"]), float(c["t_end_s"])
    except (KeyError, TypeError, ValueError):
        return False
    if dur is not None and not (0 <= a <= b <= dur + slack):
        return False
    return bool(_tokens(claim) & _tokens(quote))


def extract_claims(wav_path_or_bytes, mime="audio/wav"):
    """Return [{claim, speaker, t_start_s, t_end_s, quote}], validated. Duration check applies to wav only."""
    dur = duration_s(wav_path_or_bytes) if mime in ("audio/wav", "audio/x-wav") else None
    res = llm.call(MODEL, [llm.audio_part(wav_path_or_bytes, mime), PROMPT], schema=SCHEMA,
                   thinking="LOW", purpose="audio:claims")
    raw = res.json.get("claims", []) if isinstance(res.json, dict) else []
    out = []
    for c in raw:
        if _valid(c, dur):
            out.append({"claim": c["claim"].strip(), "speaker": str(c.get("speaker") or "Speaker 1"),
                        "t_start_s": float(c["t_start_s"]), "t_end_s": float(c["t_end_s"]),
                        "quote": c["quote"].strip()})
    return out
