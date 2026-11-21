"""Single gateway for every Gemini call: client, structured output, retries, record/replay cache,
per-run spend cap and a JSONL call log. Usage: `llm.call(model, parts, purpose=..., schema=...)`.

Env: GEMINI_API_KEY (read lazily), LLM_RUN_CAP_USD (default 5.0), LLM_RUN_ID, LLM_CACHE=readwrite|replay|off.
Never logs the key or prompt contents (a sha256 of the request is logged instead).
"""
import datetime as dt
import hashlib
import json
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
# ASSUMPTION: thinking tokens are billed at the output rate.
_PROMO_END = dt.date(2026, 11, 30)
PRICES = {
    "gemini-4-hydrogen": [(None, 1.50, 7.50)],
    "gemini-4-neon": [(None, 3.00, 15.00)],
    "gemini-4-carbon": [(_PROMO_END, 2.00, 10.00), (None, 4.00, 20.00)],
    "gemini-4-argon": [(_PROMO_END, 2.00, 10.00), (None, 4.00, 20.00)],
}

_sleep = time.sleep  # injectable for tests
_today = lambda: dt.datetime.now(dt.timezone.utc).date()  # noqa: E731  injectable for tests
_PROCESS_RUN_ID = uuid.uuid4().hex
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


def text_part(s):
    return types.Part.from_text(text=s)


def _ser(p):
    """Canonical, hashable form of one part; bytes are hashed by content."""
    if isinstance(p, str):
        return {"text": p}
    if isinstance(p, (bytes, bytearray)):
        return {"bytes": hashlib.sha256(p).hexdigest()}
    blob = getattr(p, "inline_data", None)
    if blob is not None:
        return {"mime": blob.mime_type, "bytes": hashlib.sha256(blob.data or b"").hexdigest(), "n": len(blob.data or b"")}
    if getattr(p, "text", None) is not None:
        return {"text": p.text}
    raise LLMError(f"unsupported part: {type(p).__name__}")


def _est_input_tokens(parts):
    n = 0.0
    for s in map(_ser, parts):
        if "text" in s:
            n += len(s["text"]) / 4
        elif str(s.get("mime", "")).startswith("audio/"):
            n += s["n"] / 500  # ~32 tokens/s at ~16 KB/s
        else:
            n += 2000  # flat guess per image/other binary
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


# ---- log / spend -------------------------------------------------------------------------------
def run_id():
    return os.environ.get("LLM_RUN_ID") or _PROCESS_RUN_ID


def _log(**row):
    BUILD.mkdir(parents=True, exist_ok=True)
    with open(BUILD / "calls.jsonl", "a") as f:
        f.write(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "run_id": run_id(), **row}) + "\n")


def run_spend(rid=None):
    rid, total = rid or run_id(), 0.0
    try:
        lines = (BUILD / "calls.jsonl").read_text().splitlines()
    except FileNotFoundError:
        return 0.0
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("run_id") == rid:
            total += r.get("cost_usd") or 0.0
    return total


# ---- cache -------------------------------------------------------------------------------------
def cache_key(model, parts, schema, thinking):
    blob = json.dumps([model, [_ser(p) for p in parts], schema, thinking], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _cache_mode():
    m = os.environ.get("LLM_CACHE", "readwrite")
    if m not in ("readwrite", "replay", "off"):
        raise LLMError(f"LLM_CACHE must be readwrite|replay|off, got {m!r}")
    return m


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


def call(model, parts, *, schema=None, purpose, thinking=None):
    t0 = time.monotonic()
    base = {"model": model, "purpose": purpose}
    key = cache_key(model, parts, schema, thinking)
    base["request_sha256"] = key
    try:
        pin, pout = price(model)  # unknown model -> refuse, even on a cache hit
        mode = _cache_mode()
        path = BUILD / "cache" / f"{key}.json"
        if mode != "off" and path.exists():
            hit = json.loads(path.read_text())
            res = _finish(hit["text"], hit["usage"], schema, 0.0, True, time.monotonic() - t0)
            _log(**base, **hit["usage"], latency_s=res.latency_s, cost_usd=0.0, cache_hit=True, error=None)
            return res
        if mode == "replay":
            raise CacheMiss(f"LLM_CACHE=replay and no cache entry {key[:12]} for purpose {purpose!r}")
        cap = float(os.environ.get("LLM_RUN_CAP_USD", "5.0"))
        worst = run_spend() + _est_input_tokens(parts) * pin / 1e6 + MAX_OUTPUT_TOKENS * pout / 1e6
        if worst > cap:
            raise CapExceeded(f"worst-case ${worst:.4f} would exceed run cap ${cap:.2f} (spent ${run_spend():.4f})")
        cfg = {"max_output_tokens": MAX_OUTPUT_TOKENS}
        if schema is not None:
            cfg.update(response_mime_type="application/json", response_json_schema=schema)
        if thinking:
            cfg["thinking_config"] = types.ThinkingConfig(thinking_level=thinking)
        resp = _generate(model, [p if not isinstance(p, str) else text_part(p) for p in parts],
                         types.GenerateContentConfig(**cfg))
    except Exception as e:
        _log(**base, prompt_tokens=0, output_tokens=0, thinking_tokens=0, cached_tokens=0,
             latency_s=round(time.monotonic() - t0, 3), cost_usd=0.0, cache_hit=False,
             error=f"{type(e).__name__}: {getattr(e, 'code', '') or str(e)[:120]}".replace(
                 os.environ.get("GEMINI_API_KEY", "\0").strip() or "\0", "***"))
        raise
    usage, text = _usage(resp), resp.text or ""
    cost = _cost(model, usage["prompt_tokens"], usage["output_tokens"] + usage["thinking_tokens"])
    lat = time.monotonic() - t0
    try:
        res = _finish(text, usage, schema, cost, False, lat)
    except LLMError as e:
        _log(**base, **usage, latency_s=round(lat, 3), cost_usd=cost, cache_hit=False, error=str(e)[:200])
        raise
    if mode == "readwrite":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"text": text, "usage": usage}))
    _log(**base, **usage, latency_s=round(lat, 3), cost_usd=cost, cache_hit=False, error=None)
    return res


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
