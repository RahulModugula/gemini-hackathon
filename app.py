"""FastAPI backend + static UI for the claim checker (ticket T10).  Run: uvicorn app:app --port 8000"""
import asyncio
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

import audio_claims
import check as checker
import llm
import rfi

try:
    from google.genai import errors as genai_errors
except Exception:  # pragma: no cover - SDK absent
    genai_errors = None

HERE = Path(__file__).parent
BUILD = HERE / "build"          # tests monkeypatch
UI = HERE / "ui"
MAX_CLAIM = 500
MAX_AUDIO = 10 * 1024 * 1024
AUDIO_MIMES = {"audio/wav", "audio/x-wav", "audio/webm", "audio/ogg"}
AUDIO_CONCURRENCY = 3

app = FastAPI(title="Drawing Claim Checker")
_lock = threading.Lock()


# Only the exception type is returned: never the message (may hold prompt text) or a stack trace.
@app.exception_handler(llm.LLMError)
async def _llm_error(request, exc):
    return JSONResponse({"error": type(exc).__name__}, status_code=503)


if genai_errors is not None:
    @app.exception_handler(genai_errors.APIError)
    async def _api_error(request, exc):
        return JSONResponse({"error": "APIError"}, status_code=503)


def _bad(msg, code=400):
    return JSONResponse({"error": msg}, status_code=code)


# ---- ledger ---------------------------------------------------------------------------------------------
def _ledger_path():
    return Path(BUILD) / "cards.jsonl"


def _append(card):
    p = _ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock, p.open("a") as f:
        f.write(json.dumps(card) + "\n")


def _read_ledger():
    p = _ledger_path()
    if not p.exists():
        return []
    out = []
    for ln in p.read_text().splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out[::-1]


def _fixtures():
    try:
        data = json.loads((UI / "cards.json").read_text())
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _find_card(card_id):
    return next((c for c in _read_ledger() + _fixtures() if c.get("id") == card_id), None)


def _box_2d(bbox, rect):
    """PDF-pt box -> [ymin, xmin, ymax, xmax] on a 0..1000 scale of the crop."""
    w, h = (rect[2] - rect[0]) or 1.0, (rect[3] - rect[1]) or 1.0
    cl = lambda v: max(0, min(1000, round(v)))  # noqa: E731
    return [cl((bbox[1] - rect[1]) / h * 1000), cl((bbox[0] - rect[0]) / w * 1000),
            cl((bbox[3] - rect[1]) / h * 1000), cl((bbox[2] - rect[0]) / w * 1000)]


def _finish(card, claim, spend0=None, t0=None):
    """Add id, rewrite crop paths to /crops/<file>, add the UI's box_2d_on_crop."""
    card = dict(card)
    seed = f"{claim}|{card.get('timestamp')}|{time.time_ns()}"
    card["id"] = "c_" + hashlib.sha1(seed.encode()).hexdigest()[:10]
    evs = []
    for e in card.get("evidence") or []:
        e = dict(e)
        if e.get("crop"):
            e["crop"] = "/crops/" + Path(e["crop"]).name
        if e.get("crop_rect_pt") and e.get("bbox") and "box_2d_on_crop" not in e:
            e["box_2d_on_crop"] = _box_2d(e["bbox"], e["crop_rect_pt"])
        evs.append(e)
    card["evidence"] = evs
    if t0 is not None:
        card["latency_s"] = round(time.time() - t0, 1)
    if spend0 is not None:
        card["cost_usd"] = round(max(0.0, llm.run_spend() - spend0), 4)
    return card


# ---- API ------------------------------------------------------------------------------------------------
@app.post("/api/check")
async def api_check(request: Request):
    try:
        body = await request.json()
    except ValueError:
        return _bad("body must be JSON {claim}")
    claim = body.get("claim") if isinstance(body, dict) else None
    claim = claim.strip() if isinstance(claim, str) else ""
    if not claim:
        return _bad("claim is empty")
    if len(claim) > MAX_CLAIM:
        return _bad(f"claim longer than {MAX_CLAIM} characters")
    spend0, t0 = llm.run_spend(), time.time()
    card = _finish(await run_in_threadpool(checker.check, claim), claim, spend0, t0)
    _append(card)
    return card


@app.get("/api/cards")
async def api_cards():
    return _read_ledger() or _fixtures()


@app.post("/api/audio")
async def api_audio(request: Request):
    mime = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if mime not in AUDIO_MIMES:
        return _bad("Content-Type must be audio/wav, audio/webm or audio/ogg", 415)
    try:
        if int(request.headers.get("content-length") or 0) > MAX_AUDIO:
            return _bad("audio larger than 10 MB", 413)
    except ValueError:
        return _bad("bad Content-Length")
    data = await request.body()
    if not data:
        return _bad("empty body")
    if len(data) > MAX_AUDIO:
        return _bad("audio larger than 10 MB", 413)
    claims = await run_in_threadpool(audio_claims.extract_claims, data, mime)
    sem = asyncio.Semaphore(AUDIO_CONCURRENCY)

    async def one(c):
        async with sem:
            t0 = time.time()
            card = await run_in_threadpool(checker.check, c["claim"])
        card = _finish(card, c["claim"], t0=t0)
        card["speaker"], card["t_start_s"], card["t_end_s"] = c.get("speaker"), c.get("t_start_s"), c.get("t_end_s")
        _append(card)
        return card

    cards = await asyncio.gather(*(one(c) for c in claims))
    return {"claims": claims, "cards": list(cards)}


@app.post("/api/rfi")
async def api_rfi(request: Request):
    try:
        body = await request.json()
    except ValueError:
        return _bad("body must be JSON {card_id}")
    cid = body.get("card_id") if isinstance(body, dict) else None
    card = _find_card(cid) if isinstance(cid, str) else None
    if card is None:
        return _bad("unknown card_id", 404)
    r = await run_in_threadpool(rfi.draft_rfi, card)   # drafts only; nothing is ever sent
    return {k: r.get(k) for k in ("status", "text", "answered_by", "quote")}


@app.get("/api/health")
async def api_health():
    return {"ok": True, "cache_mode": os.environ.get("LLM_CACHE", "readwrite"), "run_spend": llm.run_spend()}


# ---- static ---------------------------------------------------------------------------------------------
@app.get("/crops/{name}")
async def crops(name: str):
    if Path(name).name != name or name.startswith("."):
        return _bad("not found", 404)
    for d in (Path(BUILD) / "crops", UI / "crops"):
        if (d / name).is_file():
            return FileResponse(d / name)
    return _bad("not found", 404)


@app.get("/")
async def index():
    return FileResponse(UI / "index.html")


app.mount("/assets", StaticFiles(directory=UI / "assets"), name="assets")
