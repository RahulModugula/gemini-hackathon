"""Single gateway for every Gemini call: client, structured output, retries, record/replay cache,
per-run spend cap and a JSONL call log. Usage: `llm.call(model, parts, purpose=..., schema=...)`.

Env: GEMINI_API_KEY (read lazily), LLM_RUN_CAP_USD (default 5.0), LLM_RUN_ID, LLM_CACHE=readwrite|replay|off.
Run id: LLM_RUN_ID, else "day-YYYY-MM-DD" (UTC), so the cap accumulates across processes/scripts that day.
Cap: before a live call a reservation (worst-case cost) is appended to build/calls.jsonl under an flock on
build/calls.lock; the call's own log row settles it. Spend = settled costs + unsettled reservations.
Never logs the key or prompt contents (a sha256 of the request is logged instead).
"""
import datetime as dt
import fcntl
import hashlib
import io
import json
import math
import os
import random
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from google import genai
from google.genai import errors, types

BUILD = Path(__file__).parent / "build"  # tests monkeypatch this
MAX_OUTPUT_TOKENS = 8192
RETRIES = 4
PRICES_AS_OF = "2026-11-21"
# USD per 1M tokens. Entries: (last_day_inclusive or None, input, output). First match by call date wins.
# ASSUMPTION: thinking tokens are billed at the output rate (and are not limited by max_output_tokens).
_PROMO_END = dt.date(2026, 11, 30)
PRICES = {
    "gemini-4-hydrogen": [(None, 1.50, 7.50)],
    "gemini-4-neon": [(None, 3.00, 15.00)],
    "gemini-4-carbon": [(_PROMO_END, 2.00, 10.00), (None, 4.00, 20.00)],
    "gemini-4-argon": [(_PROMO_END, 2.00, 10.00), (None, 4.00, 20.00)],
}
# Worst-case thinking tokens per level, for the cap estimate. thinking=None -> the model's assumed default level.
THINK_BUDGET = {"MINIMAL": 1024, "LOW": 4096, "MEDIUM": 16384, "HIGH": 32768, "XHIGH": 65536}
THINK_DEFAULT = {"gemini-4-hydrogen": "HIGH", "gemini-4-neon": "HIGH", "gemini-4-carbon": "MEDIUM",
                 "gemini-4-argon": "MEDIUM"}

_sleep = time.sleep  # injectable for tests
_today = lambda: dt.datetime.now(dt.timezone.utc).date()  # noqa: E731  injectable for tests
_client = None


class LLMError(Exception):
    pass


class CapExceeded(LLMError):
    pass


class UnknownModel(LLMError):
    pass


class CacheMiss(LLMError):
    pass


@dataclass
class Result:
    text: str
    json: object = None
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    cached: bool = False
    latency_s: float = 0.0


# ---- parts -------------------------------------------------------------------------------------
def _bytes(x):
    return x if isinstance(x, (bytes, bytearray)) else Path(x).read_bytes()


def image_part(path_or_bytes, mime):
    return types.Part.from_bytes(data=_bytes(path_or_bytes), mime_type=mime)


def audio_part(path_or_bytes, mime):
    return types.Part.from_bytes(data=_bytes(path_or_bytes), mime_type=mime)


VIDEO_TOKENS_PER_S = 300  # default media resolution
FILE_POLL_S, FILE_WAIT_S = 2.0, 120.0


def _video_meta(duration_s):
    # end_offset = the whole clip: harmless to the model, and it lets the cap estimate see the duration.
    return types.VideoMetadata(end_offset=f"{math.ceil(duration_s)}s") if duration_s else None


def video_part(path_or_bytes, mime, duration_s=None):
    """Inline video (keep under ~20 MB; use upload_video above that). duration_s feeds the cap estimate."""
    return types.Part(inline_data=types.Blob(data=_bytes(path_or_bytes), mime_type=mime),
                      video_metadata=_video_meta(duration_s))


def upload_video(path_or_bytes, mime, duration_s=None):
    """Files API upload for big videos; waits until ACTIVE and returns a file-uri Part. Raises LLMError."""
    f = _get_client().files.upload(file=io.BytesIO(_bytes(path_or_bytes)), config={"mime_type": mime})
    waited = 0.0
    while getattr(f.state, "name", f.state) == "PROCESSING":
        if waited >= FILE_WAIT_S:
            raise LLMError("video upload still processing after %ds" % FILE_WAIT_S)
        _sleep(FILE_POLL_S)
        waited += FILE_POLL_S
        f = _get_client().files.get(name=f.name)
    if getattr(f.state, "name", f.state) != "ACTIVE":
        raise LLMError(f"video upload ended in state {getattr(f.state, 'name', f.state)}")
    return types.Part(file_data=types.FileData(file_uri=f.uri, mime_type=mime), video_metadata=_video_meta(duration_s))


def delete_file(part):
    """Best-effort removal of an uploaded file part (files also expire after 48 h)."""
    try:
        name = part.file_data.file_uri.split("/v1beta/")[-1]
        _get_client().files.delete(name=name)
    except Exception:
        pass


def text_part(s):
    return types.Part.from_text(text=s)


def _ser(p):
    """Canonical, hashable form of one part: every non-inline field, plus inline bytes hashed by content."""
    if isinstance(p, str):
        return {"text": p}
    if isinstance(p, (bytes, bytearray)):
        return {"inline": {"mime": None, "sha256": hashlib.sha256(p).hexdigest(), "n": len(p)}}
    if not isinstance(p, types.Part):
        raise LLMError(f"unsupported part: {type(p).__name__}")
    d = p.model_dump(mode="json", exclude={"inline_data"}, exclude_none=True)
    if p.inline_data is not None:
        data = p.inline_data.data or b""
        d["inline"] = {"mime": p.inline_data.mime_type, "sha256": hashlib.sha256(data).hexdigest(), "n": len(data)}
    return d


def _image_tokens(data):
    try:
        from PIL import Image
        w, h = Image.open(io.BytesIO(data)).size
    except Exception:
        return 2000  # dims unreadable: flat guess
    return max(258, math.ceil(w / 768) * math.ceil(h / 768) * 258)  # ~258 tokens per 768x768 tile


def _video_tokens(p, nbytes):
    """~300 tokens/s from the duration in video_metadata; without it, assume a low 20 KB/s bitrate (over-estimates)."""
    vm = getattr(p, "video_metadata", None)
    end = str(getattr(vm, "end_offset", "") or "")
    if end.endswith("s"):
        try:
            return float(end[:-1]) * VIDEO_TOKENS_PER_S
        except ValueError:
            pass
    return max(2000, (nbytes or 0) / 20000 * VIDEO_TOKENS_PER_S)


def _est_input_tokens(parts):
    n = 0.0
    for p in parts:
        s = _ser(p)
        if isinstance(p, types.Part) and p.file_data is not None:
            n += _video_tokens(p, None)
            continue
        if "text" in s:
            n += len(s["text"].encode()) / 3  # conservative
        if "inline" in s:
            mime = s["inline"]["mime"] or ""
            raw = p.inline_data.data if isinstance(p, types.Part) else bytes(p)
            if mime.startswith("audio/"):
                n += s["inline"]["n"] / 500  # ~32 tokens/s at ~16 KB/s
            elif mime.startswith("video/"):
                n += _video_tokens(p, s["inline"]["n"])
            elif mime.startswith("image/"):
                n += _image_tokens(raw)
            else:
                n += 2000
    return n


# ---- pricing / cost ----------------------------------------------------------------------------
def price(model, day=None):
    if model not in PRICES:
        raise UnknownModel(f"no price for {model!r}; refusing (known: {sorted(PRICES)})")
    day = day or _today()
    for last, pin, pout in PRICES[model]:
        if last is None or day <= last:
            return pin, pout
    raise UnknownModel(f"no price for {model!r} on {day}")


def _cost(model, prompt, out_and_thinking):
    pin, pout = price(model)
    return (prompt * pin + out_and_thinking * pout) / 1e6


def worst_case_usd(model, parts, thinking):
    pin, pout = price(model)
    level = (thinking or THINK_DEFAULT[model]).upper()
    if level not in THINK_BUDGET:
        raise LLMError(f"unknown thinking level {thinking!r}; use one of {sorted(THINK_BUDGET)}")
    return (_est_input_tokens(parts) * pin + (MAX_OUTPUT_TOKENS + THINK_BUDGET[level]) * pout) / 1e6


# ---- log / spend / reservations ----------------------------------------------------------------
def run_id():
    return os.environ.get("LLM_RUN_ID") or f"day-{_today().isoformat()}"


def _append(row):
    BUILD.mkdir(parents=True, exist_ok=True)
    row = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "run_id": run_id(), **row}
    with open(BUILD / "calls.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")


def _log(**row):
    _append(row)


def run_spend(rid=None):
    """Settled costs + reservations that no call row has settled yet."""
    rid, cost, held, settled = rid or run_id(), 0.0, {}, set()
    try:
        lines = (BUILD / "calls.jsonl").read_text().splitlines()
    except FileNotFoundError:
        return 0.0
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("run_id") != rid:
            continue
        if r.get("kind") == "reserve":
            held[r["reservation_id"]] = r.get("reserved_usd") or 0.0
        else:
            cost += r.get("cost_usd") or 0.0
            if r.get("reservation_id"):
                settled.add(r["reservation_id"])
    return cost + sum(v for k, v in held.items() if k not in settled)


def _cap():
    raw = os.environ.get("LLM_RUN_CAP_USD", "5.0")
    try:
        cap = float(raw)
    except ValueError:
        cap = math.nan
    if not (0 <= cap < math.inf):
        raise LLMError(f"LLM_RUN_CAP_USD must be a finite number >= 0, got {raw!r}")
    return cap


def _reserve(worst, cap):
    """Atomically check spend+worst <= cap and record the reservation. Returns the reservation id."""
    BUILD.mkdir(parents=True, exist_ok=True)
    with open(BUILD / "calls.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        spent = run_spend()
        if spent + worst > cap:
            raise CapExceeded(f"worst-case ${worst:.4f} + spent/reserved ${spent:.4f} would exceed run cap ${cap:.2f}")
        rid = uuid.uuid4().hex
        _append({"kind": "reserve", "reservation_id": rid, "reserved_usd": worst})
        return rid


# ---- cache -------------------------------------------------------------------------------------
def cache_key(model, parts, schema, thinking):
    blob = json.dumps(["v1", model, [_ser(p) for p in parts], schema, thinking, MAX_OUTPUT_TOKENS],
                      sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _cache_mode():
    m = os.environ.get("LLM_CACHE", "readwrite")
    if m not in ("readwrite", "replay", "off"):
        raise LLMError(f"LLM_CACHE must be readwrite|replay|off, got {m!r}")
    return m


def _cache_read(path):
    """-> (entry, reason). A missing or corrupt file is a miss."""
    if not path.exists():
        return None, "no entry"
    try:
        e = json.loads(path.read_text())
        if isinstance(e["text"], str) and isinstance(e["usage"], dict):
            return e, None
    except (ValueError, KeyError, TypeError, OSError):
        pass
    return None, "corrupt entry"


def _cache_write(path, entry):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(entry))
        os.replace(tmp, path)
    except OSError:
        pass  # the cache is best effort; the call already succeeded and is logged


# ---- client / call -----------------------------------------------------------------------------
def _get_client():
    global _client
    if _client is None:
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            raise LLMError("GEMINI_API_KEY not set")
        _client = genai.Client(api_key=key)  # explicit: SDK would otherwise prefer GOOGLE_API_KEY
    return _client


def _generate(model, contents, config):
    for attempt in range(RETRIES + 1):
        try:
            return _get_client().models.generate_content(model=model, contents=contents, config=config)
        except errors.APIError as e:
            code = getattr(e, "code", None)
            if not (code == 429 or (isinstance(code, int) and 500 <= code < 600)) or attempt == RETRIES:
                raise
            _sleep(min(2**attempt, 30) * (0.5 + random.random()))


def _usage(resp):
    u = getattr(resp, "usage_metadata", None)
    g = lambda n: int(getattr(u, n, None) or 0)  # noqa: E731
    return {"prompt_tokens": g("prompt_token_count"), "output_tokens": g("candidates_token_count"),
            "thinking_tokens": g("thoughts_token_count"), "cached_tokens": g("cached_content_token_count")}


def _finish_reason(resp):
    c = (getattr(resp, "candidates", None) or [None])[0]
    fr = getattr(c, "finish_reason", None)
    return getattr(fr, "name", fr)


def _scrub(msg):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    return (msg.replace(key, "***") if key else msg)[:200]  # scrub first, then truncate


def call(model, parts, *, schema=None, purpose, thinking=None):
    t0 = time.monotonic()
    base, resv, logged = {"model": model, "purpose": purpose}, None, False
    zero = {"prompt_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "cached_tokens": 0}
    try:
        key = cache_key(model, parts, schema, thinking)
        base["request_sha256"] = key
        price(model)  # unknown model -> refuse, even on a cache hit
        mode = _cache_mode()
        path = BUILD / "cache" / f"{key}.json"
        hit, why = (None, "cache off") if mode == "off" else _cache_read(path)
        if hit:
            u = hit["usage"]
            res = _finish(hit["text"], u, schema, 0.0, True, time.monotonic() - t0)
            _log(**base, **zero, cached_prompt_tokens=u.get("prompt_tokens", 0),
                 cached_output_tokens=u.get("output_tokens", 0), cached_thinking_tokens=u.get("thinking_tokens", 0),
                 latency_s=res.latency_s, cost_usd=0.0, cache_hit=True, finish_reason="STOP", error=None)
            return res
        if mode == "replay":
            raise CacheMiss(f"LLM_CACHE=replay: {why} for {key[:12]} (purpose {purpose!r})")
        cap = _cap()
        resv = _reserve(worst_case_usd(model, parts, thinking), cap)
        cfg = {"max_output_tokens": MAX_OUTPUT_TOKENS}
        if schema is not None:
            cfg.update(response_mime_type="application/json", response_json_schema=schema)
        if thinking:
            cfg["thinking_config"] = types.ThinkingConfig(thinking_level=thinking)
        resp = _generate(model, [p if not isinstance(p, str) else text_part(p) for p in parts],
                         types.GenerateContentConfig(**cfg))
        usage, text, fr = _usage(resp), resp.text or "", _finish_reason(resp)
        cost = _cost(model, usage["prompt_tokens"], usage["output_tokens"] + usage["thinking_tokens"])
        lat = round(time.monotonic() - t0, 3)
        row = dict(**base, **usage, latency_s=lat, cost_usd=cost, cache_hit=False, finish_reason=fr,
                   reservation_id=resv)
        try:
            res = _finish(text, usage, schema, cost, False, lat)
        except LLMError as e:
            _log(**row, error=f"InvalidJSON: len={len(text)} sha256={hashlib.sha256(text.encode()).hexdigest()}")
            logged = True
            raise
        _log(**row, error=None)  # log first, then cache
        resv = None
        if mode == "readwrite" and text and fr in (None, "STOP"):
            _cache_write(path, {"text": text, "usage": usage})
        return res
    except Exception as e:
        if not logged:
            _log(**base, **zero, latency_s=round(time.monotonic() - t0, 3), cost_usd=0.0, cache_hit=False,
                 finish_reason=None, reservation_id=resv,
                 error=_scrub(f"{type(e).__name__}: {getattr(e, 'code', '') or e}"))
        raise


def _finish(text, usage, schema, cost, cached, latency):
    parsed = None
    if schema is not None:
        try:
            parsed = json.loads(text)
        except ValueError as e:
            raise LLMError(f"model returned invalid JSON for a schema call: {e}; text[:200]={text[:200]!r}") from e
    return Result(text=text, json=parsed, usage=usage, cost_usd=cost, cached=cached, latency_s=round(latency, 3))


def _main(argv):
    if argv != ["--price-table"]:
        sys.exit("usage: llm.py --price-table")
    print(f"USD per 1M tokens, as of {PRICES_AS_OF} (thinking billed at output rate)")
    for m, tiers in PRICES.items():
        for last, pin, pout in tiers:
            print(f"{m:20} in {pin:5.2f}  out {pout:5.2f}  " + (f"through {last}" if last else "from then on" if len(tiers) > 1 else "always"))


if __name__ == "__main__":
    _main(sys.argv[1:])
