"""T9: audio clip -> checkable claims about the drawings (speaker + approximate timestamps)."""
import io
import logging
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

WAV_MIMES = ("audio/wav", "audio/wave", "audio/x-wav")
MAX_CLAIMS, MAX_CLAIM, MAX_QUOTE, MAX_SPEAKER = 10, 300, 300, 40
END_SLACK_S = 1.0

log = logging.getLogger("audio_claims")

_TOK = re.compile(r"[a-z0-9]+")
_ONES = {"zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
         "seven": 7, "eight": 8, "nine": 9}
_TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
          "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
         "ninety": 90}
_UNITS = {"inches": "inch", "inch": "inch", "feet": "feet", "foot": "feet", "ft": "feet"}
_STOP = frozenset("""a an the is are was were be been am at in on of to for from by with and or but so as it its
this that these those there here all any each every right ok okay yes no not then than also just goes go going
get gets got has have had will would should shall can could do does did i we you he she they them our your
their my me us about around over under up down out off into per""".split())


class Claims(list):
    """List of validated claims; `.dropped` has {reason, claim, ...} rows, `.meta` has run facts."""

    def __init__(self, items=(), dropped=None, meta=None):
        super().__init__(items)
        self.dropped = dropped if dropped is not None else []
        self.meta = meta if meta is not None else {}


def _number_words(toks):
    out, i = [], 0
    while i < len(toks):
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if t in _TENS:
            v = _TENS[t]
            if nxt in _ONES and _ONES[nxt] > 0:
                v += _ONES[nxt]
                i += 1
            out.append(str(v))
        elif t in _TEENS:
            out.append(str(_TEENS[t]))
        elif t in _ONES and not (t == "oh" and nxt not in _ONES):
            if nxt == "hundred" and _ONES[t] > 0:
                out.append(str(_ONES[t] * 100))
                i += 1
            else:
                run = [t]
                while i + 1 < len(toks) and toks[i + 1] in _ONES:
                    i += 1
                    run.append(toks[i])
                out.append("".join(str(_ONES[r]) for r in run))
        else:
            out.append(t)
        i += 1
    return out


def normalize_tokens(text):
    """Lowercase tokens with spoken numbers -> digits, spelled letters joined, units canonical."""
    s = str(text).lower().replace('"', " inch ").replace("'", " feet ").replace("″", " inch ")
    toks = _TOK.findall(re.sub(r"[-–—]", " ", s))
    toks = [_UNITS.get(t, t) for t in _number_words(toks)]
    joined = []
    for t in toks:  # "105 a" -> "105a"
        if joined and len(t) == 1 and t.isalpha() and joined[-1].isdigit():
            joined[-1] += t
        else:
            joined.append(t)
    out, run = [], []
    for t in joined + [None]:  # "w c" -> "wc" (runs of 2+ single letters)
        if t is not None and len(t) == 1 and t.isalpha():
            run.append(t)
            continue
        if run:
            out.extend(["".join(run)] if len(run) > 1 else run)
            run = []
        if t is not None:
            out.append(t)
    return out


def content_tokens(text):
    return {t for t in normalize_tokens(text) if t not in _STOP and (len(t) > 1 or t.isdigit())}


def duration_s(wav):
    """Duration in seconds of a wav file path or bytes. Raises wave.Error/EOFError if unreadable."""
    f = io.BytesIO(wav) if isinstance(wav, (bytes, bytearray)) else str(wav)
    with wave.open(f, "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def extract_claims(wav_path_or_bytes, mime="audio/wav"):
    """Return Claims([{claim, speaker, t_start_s, t_end_s, quote, unverified_quote}]) with .dropped and .meta.

    Duration-based timestamp checks apply to readable wav only (meta['duration_checked'])."""
    meta = {"duration_s": None, "duration_checked": False, "rescaled_ms": False}
    if str(mime).lower() in WAV_MIMES:
        try:
            meta["duration_s"] = duration_s(wav_path_or_bytes)
            meta["duration_checked"] = True
        except (wave.Error, EOFError) as e:  # compressed / float / not really a wav
            meta["duration_error"] = f"{type(e).__name__}: {e}"
    dur = meta["duration_s"]
    res = llm.call(MODEL, [llm.audio_part(wav_path_or_bytes, mime), PROMPT], schema=SCHEMA,
                   thinking="LOW", purpose="audio:claims")
    raw = res.json.get("claims", []) if isinstance(res.json, dict) else []
    raw = raw if isinstance(raw, list) else []
    dropped = []

    def drop(reason, c):
        dropped.append({"reason": reason, "claim": c.get("claim") if isinstance(c, dict) else c})

    rows = []
    for c in raw:
        if not isinstance(c, dict):
            drop("not_an_object", c)
            continue
        claim, quote = str(c.get("claim") or "").strip(), str(c.get("quote") or "").strip()
        a, b = _num(c.get("t_start_s")), _num(c.get("t_end_s"))
        if not claim:
            drop("empty_claim", c)
        elif a is None or b is None:
            drop("bad_timestamps", c)
        else:
            rows.append([c, claim, quote, a, b])
    if dur and rows and all(r[4] > dur * 10 for r in rows):  # model answered in milliseconds
        for r in rows:
            r[3], r[4] = r[3] / 1000.0, r[4] / 1000.0
        meta["rescaled_ms"] = True
    out = []
    for c, claim, quote, a, b in rows:
        if dur is not None:
            a = max(a, 0.0)
            if a > dur:
                drop("start_after_clip_end", c)
                continue
            if b > dur + END_SLACK_S:
                b = dur
            if b < a:
                drop("end_before_start", c)
                continue
        elif a < 0 or b < a:
            drop("bad_timestamps", c)
            continue
        out.append({"claim": claim[:MAX_CLAIM], "speaker": (str(c.get("speaker") or "").strip() or "Speaker 1")[:MAX_SPEAKER],
                    "t_start_s": a, "t_end_s": b, "quote": quote[:MAX_QUOTE],
                    "unverified_quote": not (content_tokens(claim) & content_tokens(quote))})
    for c in out[MAX_CLAIMS:]:
        drop("over_limit", c)
    out = out[:MAX_CLAIMS]
    for d in dropped:
        log.warning("audio claim dropped: %s (%r)", d["reason"], d["claim"])
    return Claims(out, dropped, meta)
