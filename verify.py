"""Verifier (ticket T8): two independent judges run concurrently + code checks (grounding, currency,
addendum diff, dimension math).

`verify(claim, ctx)` -> {"verdict", "confidence", "reason", "evidence": [...], "checks": [...]}.
Everything here except the two model calls is deterministic code. Boxes are pt in rendered page space.
"""
import math
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


_FOLD = str.maketrans({**{c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2212"},
                       **{c: "'" for c in "\u2018\u2019\u2032"}, **{c: '"' for c in "\u201c\u201d\u2033"}})
MAX_BOX_AREA = 0.5       # a box covering more than this share of the crop points at nothing in particular


def _norm(s):
    """Whitespace-free lowercase with unicode dashes and curly quotes folded to ASCII."""
    return "".join(s.translate(_FOLD).split()).lower()


def _valid_box(b):
    return (isinstance(b, list) and len(b) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in b)
            and 0 <= b[0] < b[2] <= 1000 and 0 <= b[1] < b[3] <= 1000)


# ---- dimension math ------------------------------------------------------------------------------
_Q = "\"”″"
_UIN = r"(?:\s*[" + _Q + r"]|\s*(?:inches|inch|in)\b)"          # closing inch mark or unit word
_DIM = re.compile(
    r"(?:(?<![\w-])|(?<=[\"”″'′’]x))(?:"
    # feet with a tick (not "105's"): then a fraction, or a hyphen + inches (quote optional), or a space + inches
    # (closing quote required, so "3' 4 bars" is 3 ft)
    r"(?P<ft>\d+)\s*['′’](?![A-Za-z])(?:"
    r"\s*-?\s*(?P<n2>\d+)/(?P<d2>\d+)" + _UIN +
    r"|\s*-\s*(?P<w1>\d+(?:\.\d+)?)(?:[\s-]+(?P<n1>\d+)/(?P<d1>\d+))?(?:\s*[" + _Q + r"])?"
    r"|\s+(?P<w1b>\d+)(?:\s+(?P<n1b>\d+)/(?P<d1b>\d+))?\s*[" + _Q + r"]"
    r")?"
    r"|(?P<ft2>\d+(?:\.\d+)?)\s*(?:feet|foot|ft)\b(?:[\s-]*(?P<w5>\d+(?:\.\d+)?)\s*(?:inches|inch|in)\b)?"
    r"|(?:(?P<w3>\d+)[\s-]+)?(?P<n3>\d+)/(?P<d3>\d+)" + _UIN +
    r"|(?P<w4>\d+(?:\.\d+)?)" + _UIN + r")")


def _f(x):
    return float(x) if x else 0.0


def _frac(n, d):
    return _f(n) / _f(d) if d and _f(d) else 0.0


def find_dims(text):
    """-> [(matched_text, inches)] for 3'-0", 3'-0 1/2", 3', 36", 36 in, 3 feet, 1/2", 1 1/2". Not "105's", "WC-2's"."""
    out = []
    for m in _DIM.finditer(text):
        g = m.groupdict()
        if g["ft"] is not None:
            v = _f(g["ft"]) * 12 + _f(g["w1"]) + _f(g["w1b"]) + _frac(g["n1"], g["d1"]) + _frac(g["n1b"], g["d1b"]) \
                + _frac(g["n2"], g["d2"])
        elif g["ft2"] is not None:
            v = _f(g["ft2"]) * 12 + _f(g["w5"])
        elif g["n3"] is not None:
            if not _f(g["d3"]):
                continue
            v = _f(g["w3"]) + _frac(g["n3"], g["d3"])
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
def _alnum(s):
    return "".join(c for c in s.lower() if c.isalnum())


def grounding_report(out, crops, claim=""):
    """-> (problems, accepted). `accepted` maps each quoted token to the rule that accepted it.
    Every evidence box must cover PDF words. Quoted words (tokens) are accepted by rule:
      current_box     inside a box on the current image
      superseded_box  inside a box on the superseded image (conflict verdicts only; it explains what changed)
      claim_value     (conflict or consistent) the word is in the claim text: for a conflict it is the contradicted
                      value, not evidence; consistent verdicts may repeat the claim's words too
    and a conflict/consistent verdict must also have real evidence: at least one current box containing at least
    one quoted word that was NOT merely copied from the claim. An unclear verdict needs only that quotes sit in
    some box."""
    problems, boxes, accepted = [], [], {}
    for i, ev in enumerate(out.get("evidence") or []):
        if not isinstance(ev, dict):
            problems.append(f"evidence[{i}] is not an object")
            continue
        b, img = ev.get("box_2d"), ev.get("image")
        crop = crops.get(img) if isinstance(img, str) else None
        if crop is None or not _valid_box(b):
            problems.append(f"evidence[{i}] has a bad box or an unknown image ({ev.get('image')!r}, {b!r})")
            continue
        if (b[2] - b[0]) * (b[3] - b[1]) > MAX_BOX_AREA * 1e6:
            problems.append(f"evidence[{i}] box covers over {MAX_BOX_AREA:.0%} of the {img} crop, "
                            "so it does not point at anything")
            continue
        pt = box_to_pt(b, crop["rect"])
        boxes.append((ev["image"], pt))
        if not any(_share_inside(w["bbox"], pt) >= OVERLAP_MIN for w in crop["words"]):
            problems.append(f"evidence[{i}] box on the {ev['image']} crop covers no PDF words")
    verdict = out.get("verdict")
    quoted = [q for q in (out.get("quoted_words") or []) if isinstance(q, str) and q.strip()]
    claim_tokens = {_alnum(t) for t in claim.split()} - {""}

    def inside(token, images):
        return any(_norm(w["text"]) == _norm(token) and _share_inside(w["bbox"], pt) >= OVERLAP_MIN
                   for img, pt in boxes if img in images for w in crops[img]["words"])

    real_evidence = False
    for tok in dict.fromkeys(t for q in quoted for t in q.split()):
        in_claim = _alnum(tok) in claim_tokens
        in_cur = inside(tok, ("current",))
        if verdict == "unclear":
            ok = "any_box" if inside(tok, ("current", "superseded")) else None
        elif in_cur:
            ok = "current_box"
        elif verdict == "conflict" and inside(tok, ("superseded",)):
            ok = "superseded_box"
        elif in_claim:
            ok = "claim_value"
        else:
            ok = None
        if ok is None:
            where = "inside any evidence box" if verdict == "unclear" else (
                "inside a current evidence box" + (" or a superseded one, or in the claim" if verdict == "conflict"
                                                   else " or in the claim"))
            problems.append(f"quoted word {tok!r} is not {where}")
        else:
            accepted[tok] = ok
        # A consistent verdict is proven by the claim's own words sitting in a current box ("WC-2" on P101).
        # A conflict must point at the contradicting word (WC-3), so a quote copied from the claim does not count.
        real_evidence |= in_cur and (verdict == "consistent" or not in_claim)
    if verdict != "unclear":
        if not quoted or not any(img == "current" for img, _ in boxes):
            problems.append("a conflict/consistent verdict needs at least one quoted word and one evidence box "
                            "on the current sheet")
        elif not real_evidence and verdict == "conflict":
            problems.append("no current evidence box contains a quoted word that is not just copied from the claim")
        elif not real_evidence:
            unboxed = [t for t, rule in accepted.items() if rule == "claim_value"]
            problems.append(f"no quoted word is inside a current evidence box (only repeated from the claim: {unboxed})")
    return problems, accepted


def grounding(out, crops, claim=""):
    return grounding_report(out, crops, claim)[0]


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


MAX_DIFF_BOXES = 60


def diff_boxes(crop, only):
    """box_2d ([ymin, xmin, ymax, xmax], 0-1000 on the crop) of the words of `crop` counted in `only`
    ({text: n}): words that exist on this version and not on the other. Each text is used at most n times."""
    left, out = dict(only), []
    for w in crop["words"]:
        if left.get(w["text"], 0) > 0 and len(out) < MAX_DIFF_BOXES:
            left[w["text"]] -= 1
            out.append(pt_to_box(w["bbox"], crop["rect"]))
    return out


def addendum_diff(crops, narrative=()):
    cur, old = crops.get("current"), crops.get("superseded")
    if not old:
        return None
    a, b = Counter(w["text"] for w in old["words"]), Counter(w["text"] for w in cur["words"])
    only_old, only_cur = dict(a - b), dict(b - a)
    return {"by": "code", "check": "addendum_diff", "info": True, "pass": True,
            "only_superseded": only_old, "only_current": only_cur, "narrative": list(narrative),
            "current_sheet": cur["sheet"], "superseded_sheet": old["sheet"], "superseded_crop": old.get("path"),
            "diff_boxes": {"superseded": diff_boxes(old, only_old), "current": diff_boxes(cur, only_cur)},
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
        conf = float(out.get("confidence"))
        if not math.isfinite(conf):
            raise ValueError("non-finite confidence")
        conf = min(1.0, max(0.0, conf))
    except (TypeError, ValueError):
        conf, ok = 0.0, False
    ev = out.get("evidence")
    ev = [e if isinstance(e, dict) else {"image": None, "box_2d": None} for e in ev] if isinstance(ev, list) else []
    qw = out.get("quoted_words")
    qw = [q for q in qw if isinstance(q, str)] if isinstance(qw, list) else []
    out = {**out, "evidence": ev, "quoted_words": qw}
    return {**out, "verdict": out["verdict"] if ok else "unclear", "confidence": conf if ok else 0.0,
            "reason": str(out.get("reason") or ("unusable judge output" if not ok else ""))}


def _emit(ctx, step, status, **info):
    cb = ctx.get("progress")
    if cb:
        cb(step, status, **info)


def run_judge(model, purpose, ctx, step=None):
    """-> (cleaned output, latency seconds). A gateway failure is an unclear verdict, never an exception."""
    step = step or purpose
    _emit(ctx, step, "start", model=model)
    out, lat = _run_judge(model, purpose, ctx)
    _emit(ctx, step, "done", model=model, verdict=out["verdict"])
    return out, lat


def _run_judge(model, purpose, ctx):
    t0 = time.monotonic()
    try:
        res = llm.call(model, ctx["parts"], schema=ctx["schema"], purpose=purpose, thinking=ctx["thinking"])
        out = _clean(res.json)
    except Exception as e:   # gateway errors AND SDK errors (google.genai.errors.APIError is not an LLMError)
        out = {"verdict": "unclear", "confidence": 0.0, "quoted_words": [], "evidence": [], "needs_code": "",
               "changed_by_addendum": False, "reason": f"{model} failed: {type(e).__name__}: {e}"[:300]}
    return out, time.monotonic() - t0


def verify(claim, ctx):
    """ctx: judge, second (model names), parts, schema, thinking, crops, rows, narrative."""
    with ThreadPoolExecutor(max_workers=2) as ex:   # independent calls: latency = the slower one
        fa = ex.submit(run_judge, ctx["judge"], "judge", ctx, "judge_a")
        fb = ex.submit(run_judge, ctx["second"], "judge_second", ctx, "judge_b")
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

    _emit(ctx, "grounding", "start")
    probs, acc = [], {}
    for n, o in ((ctx["judge"], a), (ctx["second"], b)):
        pr, ac = grounding_report(o, crops, claim)
        probs += [f"{n}: {p}" for p in pr]
        acc[n] = ac
    rules = "; ".join(f"{n} accepted {dict(ac)}" for n, ac in acc.items() if ac)
    checks.append({"by": "code", "check": "grounding", "pass": not probs, "accepted": acc,
                   "detail": "; ".join(probs) or ("all boxes cover words and all quotes are grounded" + (f" ({rules})" if rules else ""))})
    hard_fail |= bool(probs)

    _emit(ctx, "grounding", "done", ok=not probs)
    _emit(ctx, "revision", "start")
    probs2 = currency([crops["current"]["sheet"]], ctx["rows"])
    checks.append({"by": "code", "check": "currency", "pass": not probs2,
                   "detail": "; ".join(probs2) or "evidence sheet is current"})
    hard_fail |= bool(probs2)

    d = addendum_diff(crops, ctx.get("narrative", ()))
    if d:
        checks.append(d)
    _emit(ctx, "revision", "done", ok=not probs2)

    # Compare dimensions only against quotes grounded in a box: a conflict may also quote the claim's own value.
    boxed = [t for n, o in ((ctx["judge"], a), (ctx["second"], b))
             for q in (o.get("quoted_words") or []) if isinstance(q, str)
             for t in q.split() if acc.get(n, {}).get(t) in ("current_box", "any_box")]
    dim = dimension_check(claim, boxed)
    claim_dims = find_dims(claim)
    if not dim and claim_dims and verdict == "consistent":
        # the claim states a dimension but no judge boxed one: nothing backs it, so it cannot stay consistent
        dim = {"by": "code", "check": "dimension", "code_verdict": "unverified",
               "detail": f"claim states {[t for t, _ in claim_dims]} but no judge quoted a boxed dimension to back it"}
        dim["pass"] = False
        checks.append(dim)
        hard_fail = True
    elif dim:
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
        if not isinstance(e, dict):
            continue
        crop = crops.get(e.get("image")) if isinstance(e.get("image"), str) else None
        if crop and _valid_box(e.get("box_2d")):
            ev.append({"image": e["image"], "bbox": box_to_pt(e["box_2d"], crop["rect"]), "label": e.get("label", "")})
    return {"verdict": verdict, "confidence": round(conf, 4), "reason": reason, "evidence": ev, "checks": checks}
