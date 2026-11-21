"""Verifier (ticket T8): two independent judges run concurrently + code checks (grounding, currency,
addendum diff, dimension math).

`verify(claim, ctx)` -> {"verdict", "confidence", "reason", "evidence": [...], "checks": [...]}.
Everything here except the two model calls is deterministic code. Boxes are pt in rendered page space.
"""
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import llm

MODEL_CAP = 0.5          # confidence ceiling when judges disagree or a code check fails
OVERLAP_MIN = 0.5        # a word counts as "inside" a box when at least this share of its area is in the box


# ---- geometry ------------------------------------------------------------------------------------
def box_to_pt(box_2d, rect):
    """[ymin, xmin, ymax, xmax] on 0-1000 of the crop -> [x0, y0, x1, y1] pt on the sheet."""
    ymin, xmin, ymax, xmax = box_2d
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    return [rect[0] + xmin / 1000 * w, rect[1] + ymin / 1000 * h, rect[0] + xmax / 1000 * w, rect[1] + ymax / 1000 * h]


def pt_to_box(bbox, rect):
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    return [round((bbox[1] - rect[1]) / h * 1000), round((bbox[0] - rect[0]) / w * 1000),
            round((bbox[3] - rect[1]) / h * 1000), round((bbox[2] - rect[0]) / w * 1000)]


def _share_inside(wb, box):
    ix = max(0.0, min(wb[2], box[2]) - max(wb[0], box[0]))
    iy = max(0.0, min(wb[3], box[3]) - max(wb[1], box[1]))
    area = max((wb[2] - wb[0]) * (wb[3] - wb[1]), 1e-9)
    return ix * iy / area


def _norm(s):
    return "".join(s.split()).lower()


def _valid_box(b):
    return (isinstance(b, list) and len(b) == 4 and all(isinstance(v, (int, float)) for v in b)
            and 0 <= b[0] < b[2] <= 1000 and 0 <= b[1] < b[3] <= 1000)


# ---- dimension math ------------------------------------------------------------------------------
_Q = "\"”″"
_DIM = re.compile(
    r"(?:(?P<ft>\d+)\s*['′’](?:\s*-?\s*(?P<n2>\d+)/(?P<d2>\d+)\s*[" + _Q + r"]"
    r"|\s*-?\s*(?P<w1>\d+)(?:[\s-]+(?P<n1>\d+)/(?P<d1>\d+))?\s*[" + _Q + r"]?)?"
    r"|(?:(?P<w3>\d+)[\s-]+)?(?P<n3>\d+)/(?P<d3>\d+)\s*[" + _Q + r"]"
    r"|(?P<w4>\d+(?:\.\d+)?)\s*[" + _Q + r"])")


def _f(x):
    return float(x) if x else 0.0


def find_dims(text):
    """-> [(matched_text, inches)] for 3'-0", 3'-0 1/2", 3', 36", 1/2", 1 1/2"."""
    out = []
    for m in _DIM.finditer(text):
        g = m.groupdict()
        if g["ft"] is not None:
            v = _f(g["ft"]) * 12 + _f(g["w1"])
            if g["d1"] and _f(g["d1"]):
                v += _f(g["n1"]) / _f(g["d1"])
            if g["d2"] and _f(g["d2"]):
                v += _f(g["n2"]) / _f(g["d2"])
        elif g["n3"] is not None:
            if not _f(g["d3"]):
                continue
            v = _f(g["w3"]) + _f(g["n3"]) / _f(g["d3"])
        else:
            v = _f(g["w4"])
        out.append((m.group(0).strip(), v))
    return out


def parse_dim(s):
    """One dimension string -> inches, or None if it is not exactly one dimension."""
    d = find_dims(s)
    return d[0][1] if len(d) == 1 and d[0][0] == s.strip() else None


def dimension_check(claim, quoted_words):
    """None if the claim or the quotes hold no dimension. Else a check dict; code_verdict is consistent when every
    claim dimension equals some quoted dimension, conflict otherwise."""
    cd = find_dims(claim)
    qd = find_dims(" ".join(quoted_words))
    if not cd or not qd:
        return None
    vals = [v for _, v in qd]
    missing = [t for t, v in cd if not any(abs(v - q) < 0.01 for q in vals)]
    detail = f"claim {[t for t, _ in cd]} = {[v for _, v in cd]} in; quoted {[t for t, _ in qd]} = {vals} in"
    return {"by": "code", "check": "dimension", "code_verdict": "conflict" if missing else "consistent",
            "detail": detail + (f"; not found in quotes: {missing}" if missing else "")}


# ---- code checks ---------------------------------------------------------------------------------
def grounding(out, crops):
    """Every evidence box covers PDF words; every quoted word sits inside some evidence box."""
    problems, boxes = [], []
    for i, ev in enumerate(out.get("evidence") or []):
        b, crop = ev.get("box_2d"), crops.get(ev.get("image"))
        if crop is None or not _valid_box(b):
            problems.append(f"evidence[{i}] has a bad box or an unknown image ({ev.get('image')!r}, {b!r})")
            continue
        pt = box_to_pt(b, crop["rect"])
        boxes.append((ev["image"], pt))
        if not any(_share_inside(w["bbox"], pt) >= OVERLAP_MIN for w in crop["words"]):
            problems.append(f"evidence[{i}] box on the {ev['image']} crop covers no PDF words")
    quoted = [q for q in (out.get("quoted_words") or []) if isinstance(q, str) and q.strip()]
    if out.get("verdict") != "unclear" and (not quoted or not boxes):
        problems.append("a conflict/consistent verdict needs at least one quoted word and one evidence box")
    for q in quoted:
        for tok in q.split():
            hit = any(_norm(w["text"]) == _norm(tok) and _share_inside(w["bbox"], pt) >= OVERLAP_MIN
                      for img, pt in boxes for w in crops[img]["words"])
            if not hit:
                problems.append(f"quoted word {tok!r} is not inside any evidence box")
    return problems


def currency(sheets, rows):
    """sheets: evidence sheet ids. Fails on a superseded sheet (names its replacement) or an unknown one."""
    problems = []
    for s in dict.fromkeys(sheets):
        r = rows.get(s)
        if r is None:
            problems.append(f"{s} is not in sheet_index")
        elif r.get("current") is False:
            problems.append(f"{s} ({r.get('sheet_number')}) is superseded by {r.get('superseded_by')}")
    return problems


def addendum_diff(crops, narrative=()):
    cur, old = crops.get("current"), crops.get("superseded")
    if not old:
        return None
    a, b = Counter(w["text"] for w in old["words"]), Counter(w["text"] for w in cur["words"])
    only_old, only_cur = dict(a - b), dict(b - a)
    return {"by": "code", "check": "addendum_diff", "info": True, "pass": True,
            "only_superseded": only_old, "only_current": only_cur, "narrative": list(narrative),
            "detail": f"{cur['sheet']} vs {old['sheet']}: -{sum(only_old.values())} +{sum(only_cur.values())} words"}


# ---- model calls ---------------------------------------------------------------------------------
def _model_entry(name, out, latency):
    return {"by": name, "verdict": out["verdict"], "confidence": out["confidence"], "note": out["reason"],
            "quoted_words": out.get("quoted_words", []), "needs_code": out.get("needs_code", ""),
            "changed_by_addendum": out.get("changed_by_addendum", False), "latency_s": round(latency, 3)}


def _clean(out):
    """Coerce a judge output into safe types; anything unusable becomes an unclear verdict."""
    ok = isinstance(out, dict) and out.get("verdict") in ("conflict", "consistent", "unclear")
    out = out if isinstance(out, dict) else {}
    try:
        conf = min(1.0, max(0.0, float(out.get("confidence"))))
    except (TypeError, ValueError):
        conf, ok = 0.0, False
    return {**out, "verdict": out["verdict"] if ok else "unclear", "confidence": conf if ok else 0.0,
            "reason": str(out.get("reason") or ("unusable judge output" if not ok else ""))}


def run_judge(model, purpose, ctx):
    """-> (cleaned output, latency seconds). A gateway failure is an unclear verdict, never an exception."""
    t0 = time.monotonic()
    try:
        res = llm.call(model, ctx["parts"], schema=ctx["schema"], purpose=purpose, thinking=ctx["thinking"])
        out = _clean(res.json)
    except llm.LLMError as e:
        out = {"verdict": "unclear", "confidence": 0.0, "quoted_words": [], "evidence": [], "needs_code": "",
               "changed_by_addendum": False, "reason": f"{model} failed: {type(e).__name__}: {e}"[:300]}
    return out, time.monotonic() - t0


def verify(claim, ctx):
    """ctx: judge, second (model names), parts, schema, thinking, crops, rows, narrative."""
    with ThreadPoolExecutor(max_workers=2) as ex:   # independent calls: latency = the slower one
        fa = ex.submit(run_judge, ctx["judge"], "judge", ctx)
        fb = ex.submit(run_judge, ctx["second"], "judge_second", ctx)
        (a, la), (b, lb) = fa.result(), fb.result()
    crops = ctx["crops"]
    checks = [_model_entry(ctx["judge"], a, la), _model_entry(ctx["second"], b, lb)]
    agree = a["verdict"] == b["verdict"]
    verdict = a["verdict"] if agree else "unclear"
    conf = min(a["confidence"], b["confidence"])
    notes, hard_fail = [], False
    if not agree:
        notes.append(f"judges disagree ({a['verdict']} vs {b['verdict']})")
        conf = min(conf, MODEL_CAP)

    probs = [f"{n}: {p}" for n, o in ((ctx["judge"], a), (ctx["second"], b)) for p in grounding(o, crops)]
    checks.append({"by": "code", "check": "grounding", "pass": not probs,
                   "detail": "; ".join(probs) or "all boxes cover words and all quotes are inside boxes"})
    hard_fail |= bool(probs)

    probs2 = currency([crops["current"]["sheet"]], ctx["rows"])
    checks.append({"by": "code", "check": "currency", "pass": not probs2,
                   "detail": "; ".join(probs2) or "evidence sheet is current"})
    hard_fail |= bool(probs2)

    d = addendum_diff(crops, ctx.get("narrative", ()))
    if d:
        checks.append(d)

    dim = dimension_check(claim, a.get("quoted_words") or [])
    if dim:
        dim["pass"] = verdict == "unclear" or dim["code_verdict"] == verdict
        dim["note"] = dim["detail"]
        checks.append(dim)
        hard_fail |= not dim["pass"]

    if hard_fail:
        verdict, conf = "unclear", min(conf, MODEL_CAP)
        notes += [c["detail"] for c in checks if c.get("by") == "code" and c.get("pass") is False]
    reason = a["reason"] + (" [Downgraded: " + "; ".join(notes) + "]" if notes else "")
    ev = []
    for e in a.get("evidence") or []:
        crop = crops.get(e.get("image"))
        if crop and _valid_box(e.get("box_2d")):
            ev.append({"image": e["image"], "bbox": box_to_pt(e["box_2d"], crop["rect"]), "label": e.get("label", "")})
    return {"verdict": verdict, "confidence": round(conf, 4), "reason": reason, "evidence": ev, "checks": checks}
