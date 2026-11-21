"""T21-lite: a short site video -> checkable claims (spoken + a few visual observations), with timestamps."""
import json
import re
import struct
import subprocess
import tempfile

import llm
from roles import ROLES

MAX_INLINE = 20 * 1024 * 1024
MAX_CLAIMS, MAX_VISUAL = 10, 3
KINDS = ("spoken", "visual")
SHEET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}\Z")

SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "t_start_s": {"type": "number"},
                    "t_end_s": {"type": "number"},
                    "quote_or_observation": {"type": "string"},
                    "frame_t_s": {"type": "number"},
                },
                "required": ["claim", "kind", "t_start_s", "t_end_s", "quote_or_observation", "frame_t_s"],
            },
        }
    },
    "required": ["claims"],
}

PROMPT = (
    "You watch a short video from a construction site walk. Extract atomic, checkable claims about the building "
    "that can be compared with the construction drawings (rooms, fixtures and their tags, dimensions, mounting "
    "heights, types, quantities, door swings). One fact per claim, written so it stands alone. "
    "(1) kind 'spoken': facts SPOKEN in the clip; 'quote_or_observation' is the words as heard. Normalise spoken "
    "numbers and names: 'one-oh-five A' -> '105A', 'thirty-three inches' -> 33\", 'W C dash two' -> 'WC-2'. "
    "(2) kind 'visual': at most 3 observations of INSTALLED work that can be checked against drawings, such as a "
    "fixture tag that is legible, a mounting height shown with a tape measure, or a door swing direction; "
    "'quote_or_observation' says what you see. Do not count or tally objects beyond 3, and do not guess. "
    "Give t_start_s and t_end_s in seconds from the start of the video, within its duration. "
    "For EVERY claim also give frame_t_s: the single best timestamp, in seconds from the start of the video (a "
    "number, not MM:SS), at which one still frame shows the thing the claim is about (the tag, the tape reading, "
    "the room being named). Do not give boxes or coordinates. "
    "Ignore small talk and anything that is not a checkable claim. "
    'Return {"claims": []} if there are none.'
)


# ---- duration ---------------------------------------------------------------------------------------------
def _boxes(buf, start, end):
    pos = start
    while pos + 8 <= end:
        size, typ = struct.unpack(">I4s", buf[pos:pos + 8])
        hdr = 8
        if size == 1:
            if pos + 16 > end:
                return
            size, hdr = struct.unpack(">Q", buf[pos + 8:pos + 16])[0], 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            return
        yield typ, pos + hdr, pos + size
        pos += size


def _mvhd_duration(data):
    """Duration in seconds from the moov/mvhd atom, pure Python. None if not found."""
    for typ, a, b in _boxes(data, 0, len(data)):
        if typ != b"moov":
            continue
        for t2, a2, _ in _boxes(data, a, b):
            if t2 != b"mvhd":
                continue
            try:
                if data[a2] == 1:
                    scale, dur = struct.unpack(">IQ", data[a2 + 20:a2 + 32])
                else:
                    scale, dur = struct.unpack(">II", data[a2 + 12:a2 + 20])
            except (struct.error, IndexError):
                return None
            return dur / scale if scale else None
    return None


def _ffprobe_duration(data):
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
            f.write(data)
            f.flush()
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", f.name],
                                 capture_output=True, text=True, timeout=30, check=True).stdout
        return float(json.loads(out)["format"]["duration"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None


def video_duration_s(data):
    """Seconds, via ffprobe when installed, else by parsing the MP4/MOV mvhd atom. None if unreadable."""
    d = _ffprobe_duration(data)
    if d is None:
        d = _mvhd_duration(data)
    return d if d is not None and d > 0 else None


# ---- claims -----------------------------------------------------------------------------------------------
def _valid(c, dur, slack=0.5):
    if not isinstance(c, dict) or c.get("kind") not in KINDS:
        return False
    if not all(isinstance(c.get(k), str) and c[k].strip() for k in ("claim", "quote_or_observation")):
        return False
    try:
        a, b = float(c["t_start_s"]), float(c["t_end_s"])
    except (KeyError, TypeError, ValueError):
        return False
    return 0 <= a <= b <= dur + slack


def _frame_t(c, dur):
    """The frame timestamp: the model's frame_t_s when it is a number inside the video, else the middle of the claim's
    span (clamped into the video). Always within [0, dur]."""
    try:
        t = float(c["frame_t_s"])
        if t == t and 0 <= t <= dur:
            return t
    except (KeyError, TypeError, ValueError):
        pass
    return min(max((float(c["t_start_s"]) + float(c["t_end_s"])) / 2, 0.0), dur)


def valid_box_2d(b):
    """[ymin, xmin, ymax, xmax] on 0-1000, finite numbers, ymin < ymax and xmin < xmax."""
    try:
        return (isinstance(b, (list, tuple)) and len(b) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                and v == v for v in b) and 0 <= b[0] < b[2] <= 1000 and 0 <= b[1] < b[3] <= 1000)
    except TypeError:
        return False


def extract_claims_from_video(data, mime, sheet_hint=None, duration=None):
    """Return [{claim, kind, t_start_s, t_end_s, quote_or_observation, frame_t_s, frame_box_2d}], validated, at most
    10 (3 visual). frame_box_2d is always None here: boxes are documented for images, not video, so any box the
    video call returns is ignored; locate_in_frame() finds it on the extracted frame."""
    if sheet_hint is not None and not (isinstance(sheet_hint, str) and SHEET_RE.match(sheet_hint)):
        sheet_hint = None        # never interpolate an unvalidated string into the prompt
    dur = duration if duration is not None else video_duration_s(data)
    if dur is None:
        raise ValueError("could not read video duration")
    prompt = PROMPT + (f" The walk is of the area on drawing sheet {sheet_hint}." if sheet_hint else "")
    uploaded = None
    try:
        if len(data) <= MAX_INLINE:
            part = llm.video_part(data, mime, dur)
        else:
            part = uploaded = llm.upload_video(data, mime, dur)
        res = llm.call(ROLES["judge"], [part, prompt], schema=SCHEMA, thinking="LOW", purpose="video:claims")
    finally:
        if uploaded is not None:
            llm.delete_file(uploaded)
    raw = res.json.get("claims", []) if isinstance(res.json, dict) else []
    out, visual = [], 0
    for c in raw:
        if not _valid(c, dur):
            continue
        if c["kind"] == "visual":
            if visual >= MAX_VISUAL:
                continue
            visual += 1
        out.append({"claim": c["claim"].strip(), "kind": c["kind"], "t_start_s": float(c["t_start_s"]),
                    "t_end_s": float(c["t_end_s"]), "quote_or_observation": c["quote_or_observation"].strip(),
                    "frame_t_s": round(_frame_t(c, dur), 2),
                    "frame_box_2d": None})
        if len(out) >= MAX_CLAIMS:
            break
    return out


LOCATE_SCHEMA = {
    "type": "object",
    "properties": {"found": {"type": "boolean"}, "box_2d": {"type": "array", "items": {"type": "number"}},
                   "text_read": {"type": "string"}},
    "required": ["found", "box_2d", "text_read"],
}
LOCATE_PROMPT = (
    "This is one still frame from a construction site walk. Locate the object or reading this observation is about "
    "and return box_2d = [ymin, xmin, ymax, xmax] normalised 0-1000 on THIS image, tightly around it, plus text_read: "
    "the tag text, label or tape-measure reading you can actually read there, exactly as printed ('' if none is "
    "legible). If the thing is not visible in the frame, set found=false and box_2d=[0,0,0,0]. Do not guess. "
    "Observation: {obs}")


def locate_in_frame(frame_path, claim, observation):
    """Second, IMAGE call (boxes are documented for images only): -> {"box_2d": valid box or None, "text_read": str}.
    Never raises on a bad model answer; gateway errors propagate to the caller (which treats them as 'no box')."""
    res = llm.call(ROLES["extract"], [llm.image_part(frame_path, "image/jpeg"),
                                      LOCATE_PROMPT.replace("{obs}", f"{claim} ({observation})")],
                   schema=LOCATE_SCHEMA, thinking="LOW", purpose="video:locate")
    j = res.json if isinstance(res.json, dict) else {}
    box = j.get("box_2d")
    ok = j.get("found") is True and valid_box_2d(box)
    return {"box_2d": [round(float(v)) for v in box] if ok else None, "text_read": str(j.get("text_read") or "")[:200]}


def checkable(claim, sheet_hint=None):
    """The text sent to check(): 'On P101, <claim>' when a sheet hint is given."""
    return f"On {sheet_hint}, {claim}" if sheet_hint else claim
