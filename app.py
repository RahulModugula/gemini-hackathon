"""FastAPI backend + static UI for the claim checker (ticket T10).  Run: uvicorn app:app --port 8000"""
import asyncio
import hashlib
import json
import os
import queue
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi import Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.gzip import GZipMiddleware

import ask
import audio_claims
import check as checker
import digest
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
# JSON and the UI page compress well; Starlette already skips text/event-stream (SSE stays unbuffered) and images/video.
app.add_middleware(GZipMiddleware, minimum_size=1024)
_lock = threading.Lock()
IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}   # content-addressed names never change content
SHORT = {"Cache-Control": "public, max-age=300"}


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


def _cross_site(request):
    """403 response when the request is cross-site, else None. Browsers send Sec-Fetch-Site (and Origin on POST/CORS);
    plain clients (curl, TestClient) send neither and pass. Guards every endpoint that spends model money, so an
    <img src=...> on another site cannot trigger a check."""
    site = request.headers.get("sec-fetch-site")
    if site is not None and site.strip().lower() not in ("same-origin", "none"):
        return _bad("cross-site", 403)
    origin = request.headers.get("origin")
    if origin is not None and urlsplit(origin).netloc.lower() != (request.headers.get("host") or "").lower():
        return _bad("cross-site", 403)
    return None


def _encodable(obj):
    """False when obj holds a lone surrogate (valid in a Python str, not encodable as UTF-8 for a response)."""
    try:
        json.dumps(obj, ensure_ascii=False).encode()
        return True
    except UnicodeEncodeError:
        return False


async def _json_body(request):
    """-> (body, error response). Requires Content-Type application/json: a cross-site text/plain form POST is a
    CORS 'simple request' that needs no preflight, so it must not reach the checker (LLM spend, ledger writes)."""
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        # form posts keep their long-standing 400 (test_check_form_encoded_body_is_400); either way nothing runs
        code = 400 if ctype == "application/x-www-form-urlencoded" else 415
        return None, _bad("Content-Type must be application/json", code)
    try:
        return await request.json(), None
    except (ValueError, RecursionError):      # RecursionError: absurdly nested JSON
        return None, _bad("body must be valid JSON")


# ---- ledger ---------------------------------------------------------------------------------------------
def _ledger_path():
    return Path(BUILD) / "cards.jsonl"


def _append(card):
    if not _encodable(card):
        return
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
            card = json.loads(ln)
        except (ValueError, RecursionError):
            continue
        if _encodable(card):       # a line with a lone surrogate would make every response encode fail
            out.append(card)
    return out[::-1]


def _fixtures():
    try:
        data = json.loads((UI / "cards.json").read_text())
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _find_card(card_id):
    return next((c for c in _read_ledger() + _fixtures() if c.get("id") == card_id), None)


# ---- replay / demo mode -----------------------------------------------------------------------------------
def _cache_mode():
    return os.environ.get("LLM_CACHE", "readwrite")


def _norm_claim(s):
    return " ".join(str(s or "").lower().split())


def _replay_miss(card):
    """True when a judge was 'unclear' only because LLM_CACHE=replay had no recorded answer: not a real verdict."""
    return any("CacheMiss" in str(k.get("note") or "") for k in card.get("checks") or []
               if k.get("by") and k.get("by") != "code")


def _recorded(claim):
    """Newest recorded card (ledger, then UI fixtures) for exactly this claim (case/space-insensitive), skipping
    cards that only hold a replay miss."""
    n = _norm_claim(claim)
    return next((c for c in _read_ledger() + _fixtures()
                 if _norm_claim(c.get("claim")) == n and not c.get("replay_miss") and not _replay_miss(c)), None)


def _replay_card(claim):
    """In LLM_CACHE=replay, a claim that already has a recorded card is answered from it instantly (no model call,
    nothing appended). Marked replayed=True so the UI can say so. None outside replay mode or when not recorded."""
    if _cache_mode() != "replay":
        return None
    rec = _recorded(claim)
    return None if rec is None else {**rec, "replayed": True}


def _samples():
    """ui/samples.json: [{id, label, kind: claim|question, claim, why}]; malformed entries are dropped."""
    try:
        data = json.loads((UI / "samples.json").read_text())
    except (OSError, ValueError):
        return []
    out = []
    for s in data if isinstance(data, list) else []:
        if (isinstance(s, dict) and isinstance(s.get("id"), str) and isinstance(s.get("claim"), str)
                and 0 < len(s["claim"].strip()) <= MAX_CLAIM):
            out.append({"id": s["id"], "label": str(s.get("label") or s["claim"]).strip(), "claim": s["claim"].strip(),
                        "kind": "question" if s.get("kind") == "question" else "claim", "why": str(s.get("why") or "")})
    return out


def _box_2d(bbox, rect):
    """PDF-pt box -> [ymin, xmin, ymax, xmax] on a 0..1000 scale of the crop."""
    w, h = (rect[2] - rect[0]) or 1.0, (rect[3] - rect[1]) or 1.0
    cl = lambda v: max(0, min(1000, round(v)))  # noqa: E731
    return [cl((bbox[1] - rect[1]) / h * 1000), cl((bbox[0] - rect[0]) / w * 1000),
            cl((bbox[3] - rect[1]) / h * 1000), cl((bbox[2] - rect[0]) / w * 1000)]


def seal_hash(claim, evidence):
    """First 8 hex of sha256(claim + evidence boxes + sheet ids): changes if the claim, any box or any sheet changes."""
    payload = json.dumps([claim, [[e.get("sheet"), e.get("bbox")] for e in evidence or []]],
                         separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:8]


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
    card["checks"] = [{**ck, "superseded_crop": "/crops/" + Path(ck["superseded_crop"]).name}
                      if ck.get("superseded_crop") else ck for ck in card.get("checks") or []]
    card["seal"] = seal_hash(claim, evs)
    if _replay_miss(card):
        card["replay_miss"] = True
    if t0 is not None:
        card["latency_s"] = round(time.time() - t0, 1)
    if spend0 is not None:
        card["cost_usd"] = round(max(0.0, llm.run_spend() - spend0), 4)
    return card


# ---- API ------------------------------------------------------------------------------------------------
@app.post("/api/check")
async def api_check(request: Request):
    if (xs := _cross_site(request)) is not None:
        return xs
    body, err = await _json_body(request)
    if err:
        return err
    claim, err = _valid_claim(body.get("claim") if isinstance(body, dict) else None)
    if err:
        return _bad(err)
    rec = _replay_card(claim)
    if rec is not None:
        return rec
    spend0, t0 = llm.run_spend(), time.time()
    card = _finish(await run_in_threadpool(checker.check, claim), claim, spend0, t0)
    _append(card)
    return card


@app.post("/api/ask")
async def api_ask(request: Request):
    """Free text (a claim, a question, or a request for an RFI) -> card. Same guards as /api/check."""
    if (xs := _cross_site(request)) is not None:
        return xs
    body, err = await _json_body(request)
    if err:
        return err
    text, err = _valid_claim(body.get("text") if isinstance(body, dict) else None)
    if err:
        return _bad(err.replace("claim", "text"))
    rec = _replay_card(text)
    if rec is not None:
        return rec
    spend0, t0 = llm.run_spend(), time.time()
    card = _finish(await run_in_threadpool(ask.run, text), text, spend0, t0)
    _append(card)
    return card


def _valid_claim(claim):
    claim = claim.strip() if isinstance(claim, str) else ""
    if not claim:
        return None, "claim is empty"
    if len(claim) > MAX_CLAIM:
        return None, f"claim longer than {MAX_CLAIM} characters"
    if not _encodable(claim):
        return None, "claim is not valid text"
    return claim, None


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.get("/api/check/stream")
async def api_check_stream(request: Request, claim: str = Query("")):
    """Server-Sent Events: `step` events {step, status, t, ...} as check() progresses, then one `card` event with the
    finished card (same body as POST /api/check), or one `error` event {error: <exception type>} (never the message)."""
    if (xs := _cross_site(request)) is not None:
        return xs
    claim, err = _valid_claim(claim)
    if err:
        return _bad(err)
    rec = _replay_card(claim)
    if rec is not None:
        return StreamingResponse(_replay_events(rec), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    q = queue.Queue()
    t0 = time.time()
    spend0 = llm.run_spend()

    def progress(step, status, **info):
        q.put(("step", {"step": step, "status": status, "t": round(time.time() - t0, 3), **info}))

    def work():
        try:
            card = _finish(checker.check(claim, progress=progress), claim, spend0, t0)
            _append(card)
            q.put(("step", {"step": "sealed", "status": "done", "t": round(time.time() - t0, 3), "seal": card["seal"]}))
            q.put(("card", card))
        except Exception as e:  # noqa: BLE001 - only the type name leaves the server
            q.put(("error", {"error": type(e).__name__}))
        q.put(None)

    def gen():
        threading.Thread(target=work, daemon=True).start()
        while (item := q.get()) is not None:
            yield _sse(*item)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


REPLAY_STEPS = ("search", "crop", "judge_a", "judge_b", "grounding", "revision")


def _replay_events(card):
    """The same event shapes and order as a live run (both judges start before either finishes), each step flagged
    replay=True with t=0: nothing was re-run. Judge events carry the recorded card's model and verdict when known."""
    js = [k for k in card.get("checks") or [] if k.get("by") and k.get("by") != "code" and k.get("verdict")]
    judge = {st: ({"model": js[i]["by"], "verdict": js[i]["verdict"]} if i < len(js) else {})
             for i, st in enumerate(("judge_a", "judge_b"))}
    order = [("search", "start"), ("search", "done"), ("crop", "start"), ("crop", "done"),
             ("judge_a", "start"), ("judge_b", "start"), ("judge_a", "done"), ("judge_b", "done"),
             ("grounding", "start"), ("grounding", "done"), ("revision", "start"), ("revision", "done")]
    for st, status in order:
        extra = {"model": judge[st]["model"]} if st in judge and judge[st] else {}
        if status == "done" and st in judge and judge[st]:
            extra["verdict"] = judge[st]["verdict"]
        yield _sse("step", {"step": st, "status": status, "t": 0.0, "replay": True, **extra})
    yield _sse("step", {"step": "sealed", "status": "done", "t": 0.0, "replay": True, "seal": card.get("seal")})
    yield _sse("card", card)


@app.get("/api/samples")
async def api_samples():
    """One-click sample claims for the first screen. card_id is set when a recorded card already answers the claim
    (so replay mode can show it with zero network); null means the claim runs through the checker."""
    out = []
    for s in _samples():
        rec = _recorded(s["claim"])
        out.append({**s, "card_id": rec["id"] if rec else None})
    return out


@app.get("/api/metrics")
async def api_metrics():
    """Honest eval numbers from build/metrics.json; null when the file is absent or unreadable (the UI hides the strip)."""
    try:
        data = json.loads((Path(BUILD) / "metrics.json").read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


@app.get("/api/cards")
async def api_cards():
    return _read_ledger() or _fixtures()


@app.post("/api/audio")
async def api_audio(request: Request):
    if (xs := _cross_site(request)) is not None:
        return xs
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


def _crop_box(frame, box_2d):
    """Crop `box_2d` of `frame` into a file named by (frame, box), so claims on one frame with different boxes never
    share a file. Written via a unique temp file + os.replace: concurrent identical crops are atomic, never torn."""
    key = hashlib.sha1(json.dumps([frame.name, [round(float(v), 2) for v in box_2d]]).encode()).hexdigest()[:20]
    out = frame.with_name(f"{key}_crop.jpg")
    tmp = out.with_name(f"{out.stem}.{threading.get_ident()}.{os.getpid()}.{time.monotonic_ns()}.part.jpg")
    try:
        video_frames.crop_region(frame, box_2d, tmp)
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    return out


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
        try:
            img = _crop_box(fr["path"], loc["box_2d"])
        except Exception:    # crop failed: judge the whole frame and report no box
            img, ve["box_2d"] = fr["path"], None
    label = f"FIELD PHOTO from the site video at {video_frames.fmt_t(c['frame_t_s'])}"
    return ve, [{"path": img, "mime": "image/jpeg", "label": label}]


@app.post("/api/video")
async def api_video(request: Request, sheet: str | None = None):
    if (xs := _cross_site(request)) is not None:
        return xs
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
    if (xs := _cross_site(request)) is not None:
        return xs
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


# ---- sheet stage: full-sheet render + room outlines ------------------------------------------------------
_SHEET_RE = re.compile(r"^(dwg|add\d{1,2})-p\d{3}$")
SHEET_PX = 3600                # long edge of the full-sheet render the UI zooms across
_render_lock = threading.Lock()
_rooms_memo = {}


def _sheet_row(sheet):
    """(sheets.json row, source pdf path) for a known sheet id, else (None, None)."""
    if not _SHEET_RE.match(sheet or ""):
        return None, None
    try:
        idx = json.loads((Path(BUILD) / "sheets.json").read_text())
    except (OSError, ValueError):
        return None, None
    row = next((s for s in idx.get("sheets", []) if s.get("sheet") == sheet), None)
    if row is None:
        return None, None
    path = next((x.get("path") for x in idx.get("sources", []) if x.get("source") == row.get("source")), None)
    return row, path


def _room_edges(runs):
    """Boundary of a room mask given as runs [[y, x0, x1), ...] at 1 pt per px -> merged axis-aligned segments
    [[x0, y0, x1, y1], ...] in sheet pt. Pure numpy; the outline is exact to the mask, nothing is smoothed or guessed."""
    import numpy as np
    if not runs:
        return []
    ys = [r[0] for r in runs]
    xs0, xs1 = [r[1] for r in runs], [r[2] for r in runs]
    y0, x0 = min(ys), min(xs0)
    m = np.zeros((max(ys) - y0 + 3, max(xs1) - x0 + 3), bool)
    for y, a, b in runs:
        m[y - y0 + 1, a - x0 + 1:b - x0 + 1] = True
    out = []
    h = m[1:, :] != m[:-1, :]           # horizontal edges between row i and i+1
    for i in range(h.shape[0]):
        row = np.flatnonzero(np.diff(np.concatenate(([0], h[i].astype(np.int8), [0]))))
        for a, b in zip(row[::2], row[1::2]):
            yy = y0 + i
            out.append([int(x0 + a - 1), int(yy), int(x0 + b - 1), int(yy)])
    v = m[:, 1:] != m[:, :-1]           # vertical edges between column j and j+1
    for j in range(v.shape[1]):
        col = np.flatnonzero(np.diff(np.concatenate(([0], v[:, j].astype(np.int8), [0]))))
        for a, b in zip(col[::2], col[1::2]):
            xx = x0 + j
            out.append([int(xx), int(y0 + a - 1), int(xx), int(y0 + b - 1)])
    return out


def _sheet_rooms(sheet):
    """Rooms already extracted by rooms.py (build/rooms/<sheet>.json); [] when the sheet has none. Never extracts here."""
    p = Path(BUILD) / "rooms" / f"{sheet}.json"
    try:
        st = p.stat()
    except OSError:
        return []
    key = (str(p), st.st_mtime_ns)
    if key not in _rooms_memo:
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            return []
        _rooms_memo[key] = [{"number": r.get("number"), "name": r.get("name"), "bbox": r.get("bbox"),
                             "label_bbox": r.get("label_bbox"), "tags": r.get("tags") or {},
                             "edges": _room_edges(r.get("runs") or [])}
                            for r in d.get("rooms", []) if isinstance(r, dict)]
    return _rooms_memo[key]


@app.get("/api/sheet/{sheet}")
async def api_sheet(sheet: str):
    """Geometry the sheet stage needs: page size in pt, the full-sheet image URL and the room outlines."""
    row, _ = _sheet_row(sheet)
    if row is None:
        return _bad("unknown sheet", 404)
    rooms_ = await run_in_threadpool(_sheet_rooms, sheet)
    return {"sheet": sheet, "width": row.get("width"), "height": row.get("height"),
            "image": f"/sheets/{sheet}.jpg", "rooms": rooms_}


def _render_sheet(sheet, row, src):
    import pymupdf
    import pdflock
    import rooms as roomgeo
    out = Path(BUILD) / "sheet_renders" / f"{sheet}_{SHEET_PX}.jpg"
    if out.is_file():
        return out
    with _render_lock, pdflock.LOCK:
        if out.is_file():
            return out
        page = pymupdf.open(roomgeo._pdf(src))[int(row["page"]) - 1]
        z = SHEET_PX / max(page.rect.width, page.rect.height)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f"{out.stem}.{threading.get_ident()}.part.jpg")
        pix.save(str(tmp), jpg_quality=82)
        os.replace(tmp, out)
    return out


@app.get("/sheets/{name}")
async def sheet_image(name: str):
    sheet = name[:-4] if name.endswith(".jpg") else ""
    row, src = _sheet_row(sheet)
    if row is None or not src:
        return _bad("not found", 404)
    try:
        p = await run_in_threadpool(_render_sheet, sheet, row, src)
    except (OSError, RuntimeError, ValueError, IndexError, KeyError):
        return _bad("not found", 404)
    return FileResponse(p, media_type="image/jpeg", headers=SHORT)


_digest_lock = threading.Lock()


@app.get("/api/digest")
async def api_digest():
    """The cached addendum impact digest (build/digest.json); 404 until one has been run."""
    try:
        d = json.loads((Path(BUILD) / "digest.json").read_text())
    except (OSError, ValueError):
        return _bad("no digest yet: POST /api/digest/refresh", 404)
    return JSONResponse(d, headers={"Cache-Control": "no-cache"})


@app.post("/api/digest/refresh")
async def api_digest_refresh(request: Request):
    if (xs := _cross_site(request)) is not None:
        return xs
    if not _digest_lock.acquire(blocking=False):
        return _bad("a digest run is already in progress", 409)
    try:
        return await run_in_threadpool(digest.run, Path(BUILD))
    except digest.DigestError:
        return _bad("digest failed", 503)
    finally:
        _digest_lock.release()


@app.get("/api/health")
async def api_health():
    return {"ok": True, "cache_mode": os.environ.get("LLM_CACHE", "readwrite"), "run_spend": llm.run_spend()}


# ---- static ---------------------------------------------------------------------------------------------
@app.get("/crops/{name}")
async def crops(name: str):
    if Path(name).name != name or name.startswith("."):
        return _bad("not found", 404)
    for d in (Path(BUILD) / "crops", UI / "crops"):
        if (d / name).is_file():   # build crops are named by sha1(sheet, rect, dpi); fixture names are not
            return FileResponse(d / name, headers=IMMUTABLE if _CROP_RE.match(name) else SHORT)
    return _bad("not found", 404)


_CROP_RE = re.compile(r"^[0-9a-f]{16}\.png$")


_FRAME_RE = re.compile(r"^[0-9a-f]{20}(_crop)?\.jpg$")
_VIDEO_RE = re.compile(r"^[0-9a-f]{40}\.(mp4|mov)$")


@app.get("/frames/{name}")
async def frames(name: str):
    if not _FRAME_RE.match(name):
        return _bad("not found", 404)
    p = Path(BUILD) / "frames" / name
    if not p.is_file():
        return _bad("not found", 404)
    # <sha1(video|t)>.jpg never changes; <sha1(frame|box)>_crop.jpg is named by its box (kept SHORT-cached)
    return FileResponse(p, headers=SHORT if name.endswith("_crop.jpg") else IMMUTABLE)


@app.get("/videos/{name}")
async def videos(name: str):
    if not _VIDEO_RE.match(name):
        return _bad("not found", 404)
    p = Path(BUILD) / "videos" / name
    if not p.is_file():
        return _bad("not found", 404)
    return FileResponse(p, media_type="video/quicktime" if name.endswith(".mov") else "video/mp4", headers=IMMUTABLE)


@app.get("/")
async def index():
    return FileResponse(UI / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/assets", StaticFiles(directory=UI / "assets"), name="assets")
