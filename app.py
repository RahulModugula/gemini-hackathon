"""FastAPI backend + static UI for the claim checker (ticket T10).  Run: uvicorn app:app --port 8000"""
import asyncio
import hashlib
import json
import os
import re
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
import video_claims
import video_frames

try:
    from google.genai import errors as genai_errors
except Exception:  # pragma: no cover - SDK absent
    genai_errors = None

HERE = Path(__file__).parent
BUILD = HERE / "build"          # tests monkeypatch
UI = HERE / "ui"
MAX_CLAIM = 500
MAX_AUDIO = 10 * 1024 * 1024
# Safari's MediaRecorder produces audio/mp4 (AAC); Chrome/Firefox produce webm/ogg. Gemini accepts all of these.
AUDIO_MIMES = {"audio/wav", "audio/x-wav", "audio/wave", "audio/webm", "audio/ogg",
               "audio/mp4", "audio/m4a", "audio/x-m4a", "audio/aac", "audio/mpeg", "audio/mp3"}
AUDIO_MIME_ALIASES = {"audio/x-m4a": "audio/m4a", "audio/mp3": "audio/mpeg", "audio/wave": "audio/wav"}
AUDIO_CONCURRENCY = 3
MAX_VIDEO = 50 * 1024 * 1024
VIDEO_MIMES = {"video/mp4", "video/quicktime"}
VIDEO_CONCURRENCY = 3

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
        return _bad("Unsupported audio type: use wav, webm, ogg, mp4/m4a, aac or mp3", 415)
    mime = AUDIO_MIME_ALIASES.get(mime, mime)
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


def _check_with(text, extra):
    """checker.check, passing field photos only when there are some."""
    return checker.check(text, extra_images=extra) if extra else checker.check(text)


def _frame_evidence(vpath, vsha, c):
    """Extract the claim's frame; for a VISUAL claim also locate the thing on that frame (an image call) and build the
    field photo for the judges (the boxed region, else the whole frame). -> (video_evidence or None, extra_images or
    None). Does not raise: a failure here only means the card has no video evidence."""
    frames_dir = Path(BUILD) / "frames"
    try:
        fr = video_frames.extract_frame(vpath, c["frame_t_s"], frames_dir, vsha)
    except (KeyError, TypeError, OSError, RuntimeError, ValueError):
        return None, None
    ve = {"frame": "/frames/" + fr["path"].name, "t_s": c["frame_t_s"], "box_2d": None, "kind": c["kind"]}
    if c["kind"] != "visual":
        return ve, None
    try:
        loc = video_claims.locate_in_frame(fr["path"], c["claim"], c["quote_or_observation"])
    except Exception:    # model/SDK errors: keep the frame, skip the box
        loc = {"box_2d": None, "text_read": ""}
    ve["box_2d"] = loc["box_2d"]
    if loc["text_read"]:
        ve["text_read"] = loc["text_read"]
    img = fr["path"]
    if loc["box_2d"]:
        img = video_frames.crop_region(fr["path"], loc["box_2d"], fr["path"].with_name(fr["path"].stem + "_crop.jpg"))
    label = f"FIELD PHOTO from the site video at {video_frames.fmt_t(c['frame_t_s'])}"
    return ve, [{"path": img, "mime": "image/jpeg", "label": label}]


@app.post("/api/video")
async def api_video(request: Request, sheet: str | None = None):
    mime = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if mime not in VIDEO_MIMES:
        return _bad("Content-Type must be video/mp4 or video/quicktime", 415)
    hint = (sheet or "").strip() or None
    if hint and not video_claims.SHEET_RE.match(hint):
        return _bad("bad sheet hint")
    try:
        if int(request.headers.get("content-length") or 0) > MAX_VIDEO:
            return _bad("video larger than 50 MB", 413)
    except ValueError:
        return _bad("bad Content-Length")
    chunks, n = [], 0
    async for ch in request.stream():       # stop reading once over the limit
        n += len(ch)
        if n > MAX_VIDEO:
            return _bad("video larger than 50 MB", 413)
        chunks.append(ch)
    data = b"".join(chunks)
    if not data:
        return _bad("empty body")
    dur = await run_in_threadpool(video_claims.video_duration_s, data)
    if dur is None:
        return _bad("could not read the video (expected an MP4 or MOV)")
    vpath, vsha = await run_in_threadpool(video_frames.store_video, data, mime, Path(BUILD) / "videos")
    video_url = "/videos/" + vpath.name
    claims = await run_in_threadpool(video_claims.extract_claims_from_video, data, mime, hint, dur)
    sem = asyncio.Semaphore(VIDEO_CONCURRENCY)

    async def one(c):
        async with sem:
            t0 = time.time()
            text = video_claims.checkable(c["claim"], hint)
            ve, extra = await run_in_threadpool(_frame_evidence, vpath, vsha, c)
            card = await run_in_threadpool(_check_with, text, extra)
        card = _finish(card, text, t0=t0)
        card["source"], card["t_start_s"], card["t_end_s"], card["kind"] = "video", c["t_start_s"], c["t_end_s"], c["kind"]
        card["video_url"], card["video_evidence"] = video_url, ve
        c["frame_box_2d"] = ve["box_2d"] if ve else None
        _append(card)
        return card

    cards = await asyncio.gather(*(one(c) for c in claims))
    return {"claims": claims, "cards": list(cards), "video_s": round(dur, 2), "video_url": video_url}


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


_FRAME_RE = re.compile(r"^[0-9a-f]{20}(_crop)?\.jpg$")
_VIDEO_RE = re.compile(r"^[0-9a-f]{40}\.(mp4|mov)$")


@app.get("/frames/{name}")
async def frames(name: str):
    if not _FRAME_RE.match(name):
        return _bad("not found", 404)
    p = Path(BUILD) / "frames" / name
    return FileResponse(p) if p.is_file() else _bad("not found", 404)


@app.get("/videos/{name}")
async def videos(name: str):
    if not _VIDEO_RE.match(name):
        return _bad("not found", 404)
    p = Path(BUILD) / "videos" / name
    if not p.is_file():
        return _bad("not found", 404)
    return FileResponse(p, media_type="video/quicktime" if name.endswith(".mov") else "video/mp4")


@app.get("/")
async def index():
    return FileResponse(UI / "index.html")


app.mount("/assets", StaticFiles(directory=UI / "assets"), name="assets")
