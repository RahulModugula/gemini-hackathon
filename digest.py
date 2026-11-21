"""Addendum impact digest: ONE long-context Neon request (text only) over every addendum's narrative, bidder Q&A and,
for each changed sheet, the deterministic word diff (old vs new tokens with bboxes). The model writes the digest;
code verifies it (evidence tokens must be in that sheet's diff, the sheet must be changed by that addendum, Q&A
refs must exist) and attaches the boxes from the diff, never from the model.

Usage: python digest.py            -> build/digest.json (record/replay through llm's cache; LLM_CACHE applies)
"""
import datetime as dt
import json
import os
import sys
import uuid
from collections import Counter
from pathlib import Path

import llm
import verify
from roles import ROLES, THINKING

BUILD = Path(__file__).parent / "build"
TOKEN_BUDGET = 200_000          # input tokens
BYTES_PER_TOKEN = 1.25          # measured 2026-11-21: 96.6 KB of this diff text = 77,034 prompt tokens (bytes/3 said 32K)
MAX_BOXES = 12                  # boxes kept per digest item
MAX_BOXES_IN_PROMPT = 6         # bboxes listed per token in the prompt
LABEL = {"add1": "Addendum 1", "add2": "Addendum 2"}


class DigestError(Exception):
    pass


SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "sheet_number": {"type": "string"},
            "addendum": {"type": "string", "enum": ["add1", "add2"]},
            "what_changed": {"type": "string"},
            "trades_affected": {"type": "array", "items": {"type": "string"}},
            "risk": {"type": "string"},
            "evidence_tokens": {"type": "array", "items": {"type": "string"}},
            "qa_refs": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["sheet_number", "addendum", "what_changed", "trades_affected", "risk", "evidence_tokens", "qa_refs"],
    }}},
    "required": ["items"],
}

INSTRUCTIONS = """You are a preconstruction estimator reading the addenda to a bid set. Write an ADDENDUM IMPACT DIGEST.

Below you get, for each addendum: the narrative list of revised sheets, the bidder Q&A (each with an id like
[add1-q3]), and for every changed sheet the exact word-level diff between that sheet and its previous issue:
"+" lines are words that appear on the new sheet and not the old one (with their boxes on the new sheet, in PDF
points [x0,y0,x1,y1]); "-" lines are words that were removed. The diff is computed by code and is the ground truth.

Produce one item per meaningful change (group trivial edits of one sheet together; skip pure title-block or
revision-date churn). Each item has:
- sheet_number and addendum ("add1" or "add2") exactly as in the section header;
- what_changed: plain words a superintendent understands, one or two sentences;
- trades_affected: trade names (for example Plumbing, Electrical, Framing, Roofing, Finishes);
- risk: why it matters on site or for pricing, one sentence, concrete;
- evidence_tokens: 1 to 4 words copied EXACTLY from that sheet's "+" or "-" lines that prove the change. Never use
  a word that is not in that sheet's diff;
- qa_refs: ids of bidder Q&As that bear on this change (empty list when none). Use only ids that appear below.
Do not invent changes the narrative and the diff do not support. Be concise: at most 20 items in total, what_changed
under 25 words, risk under 20 words, at most 4 evidence_tokens per item. Your whole answer must stay short."""


def _norm(s):
    return verify._norm(s)


def _read(build, name):
    return json.loads((Path(build) / name).read_text())


def _rb(b):
    return [round(v, 1) if round(v, 1) != int(round(v, 1)) else int(round(v, 1)) for v in b]


def _boxes(words, only):
    """{text: [{bbox}]}: the first n words of each text, in reading order (same rule as verify.diff_boxes)."""
    left, out = dict(only), {}
    for w in words:
        if left.get(w["text"], 0) > 0:
            left[w["text"]] -= 1
            out.setdefault(w["text"], []).append({"bbox": _rb(w["bbox"])})
    return out


def _offset(old_words, cur_words):
    import check
    dx, dy, n = check.page_offset(old_words, cur_words)
    return (dx, dy) if n >= 3 else (0.0, 0.0)


def build_context(build=None):
    """-> {text, est_tokens, diffs: {(addendum, sheet_number): {...}}, qa_ids}. Raises DigestError over the budget."""
    build = Path(build or BUILD)
    changes = _read(build, "addenda_changes.json")
    rows = _read(build, "sheet_index.json")["rows"]
    try:
        qa = _read(build, "qa_index.json")
    except FileNotFoundError:
        qa = []
    sheets = {s["sheet"]: s for s in _read(build, "sheets.json")["sheets"]}
    by_num = {}
    for r in rows:
        if r.get("sheet_number"):
            by_num.setdefault(r["sheet_number"], []).append(r)

    def words(sheet):
        return _read(build, f"words/{sheet}.json")["words"]

    parts, diffs = [INSTRUCTIONS, ""], {}
    for src in sorted({c["source"] for c in changes}):
        parts.append(f"##### {LABEL.get(src, src)}: narrative (revised sheets)")
        for c in (c for c in changes if c["source"] == src):
            vs = sorted(by_num.get(c["sheet_number"], []), key=lambda r: r["issue_order"])
            cur = next((r for r in vs if r["source"] == src), None)
            if cur is None or cur["sheet"] not in sheets:
                parts.append(f"{c['sheet_number']} {c['title']}: narrative lists it but no sheet was found; no diff available")
                continue
            prev = [r for r in vs if r["issue_order"] < cur["issue_order"]]
            prev = prev[-1] if prev else None
            cw = words(cur["sheet"])
            ow = words(prev["sheet"]) if prev else []
            a, b = Counter(w["text"] for w in ow), Counter(w["text"] for w in cw)
            only_old, only_cur = dict(a - b), dict(b - a)
            d = {"sheet": cur["sheet"], "prev": prev["sheet"] if prev else None, "only_old": only_old, "only_cur": only_cur,
                 "cur_boxes": _boxes(cw, only_cur), "old_boxes": _boxes(ow, only_old),
                 "offset": _offset(ow, cw) if ow else (0.0, 0.0), "width": sheets[cur["sheet"]]["width"],
                 "height": sheets[cur["sheet"]]["height"], "current": cur.get("current")}
            diffs[(src, c["sheet_number"])] = d
            parts.append(f"=== {LABEL.get(src, src)} / {c['sheet_number']} {c['title']} (new sheet {cur['sheet']}"
                         f"{', compared with ' + prev['sheet'] if prev else ', no earlier issue'}) ===")
            for ch in c["changes"]:
                parts.append(f"narrative {ch['action']}: {ch['text']}")
            for sign, only, boxes in (("+", only_cur, d["cur_boxes"]), ("-", only_old, d["old_boxes"])):
                for t in sorted(only, key=lambda t: (boxes.get(t) or [{"bbox": [0, 0]}])[0]["bbox"][:2][::-1]):
                    bb = " ".join("[" + ",".join(str(v) for v in x["bbox"]) + "]" for x in boxes.get(t, [])[:MAX_BOXES_IN_PROMPT])
                    parts.append(f"{sign} {t} x{only[t]} {bb}")
            parts.append("")
    parts.append("##### Bidder Q&A (both addenda)")
    for q in qa:
        parts.append(f"[{q['id']}] ({q['source']}) Q: {q['question']} A: {q['response']}")
    text = "\n".join(parts)
    est = int(len(text.encode()) / BYTES_PER_TOKEN)
    if est > TOKEN_BUDGET:
        raise DigestError(f"digest context ~{est} tokens exceeds the {TOKEN_BUDGET} budget")
    return {"text": text, "est_tokens": est, "diffs": diffs, "qa_ids": {q["id"] for q in qa}, "qa": {q["id"]: q for q in qa}}


# ---- verification ------------------------------------------------------------------------------------------
def _render_crop(sheet, rect, build):
    """Render a crop of `sheet` (pt rect) into build/crops with check's renderer; -> '/crops/<name>.png'."""
    import check
    sheets, src, rows, _ = check._load()
    crop = check._crop(sheet, rect, sheets, src, rows)
    check.render(crop, src)
    return "/crops/" + Path(crop["path"]).name


def _union(bs):
    return [min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs)]


def _drop(dropped, it, reason):
    g = it if isinstance(it, dict) else {}
    dropped.append({"reason": reason, "sheet_number": g.get("sheet_number") if isinstance(g.get("sheet_number"), str) else None,
                    "addendum": g.get("addendum") if isinstance(g.get("addendum"), str) else None,
                    "what_changed": str(g.get("what_changed") or "")[:200]})


def _strs(v):
    if isinstance(v, str):
        v = [v]
    return [x.strip() for x in v if isinstance(x, str) and x.strip()] if isinstance(v, list) else None


def verify_items(items, ctx, build=None):
    """-> (kept, dropped). Everything the model says is checked against the code-computed diff."""
    import check
    kept, dropped, n = [], [], Counter()
    for it in items if isinstance(items, list) else []:
        if (not isinstance(it, dict) or not isinstance(it.get("sheet_number"), str) or not isinstance(it.get("addendum"), str)
                or not isinstance(it.get("what_changed"), str) or not it["what_changed"].strip()
                or not isinstance(it.get("risk"), str) or _strs(it.get("trades_affected")) is None
                or not isinstance(it.get("evidence_tokens"), list) or _strs(it.get("qa_refs") or []) is None):
            _drop(dropped, it, "malformed item")
            continue
        d = ctx["diffs"].get((it["addendum"], it["sheet_number"].strip()))
        if d is None:
            _drop(dropped, it, "sheet not changed by that addendum")
            continue
        cur_by, old_by = {_norm(t): t for t in d["only_cur"]}, {_norm(t): t for t in d["only_old"]}
        asked = _strs(it["evidence_tokens"]) or []
        good, bad, seen = [], [], set()
        for t in asked:
            k = _norm(t)
            real = cur_by.get(k) or old_by.get(k)
            if real is None:
                bad.append(t)
            elif k not in seen:
                seen.add(k)
                good.append((k, real))
        if not good:
            _drop(dropped, it, "evidence_tokens not in the sheet diff")
            continue
        flags = []
        if bad:
            flags.append("unverified evidence tokens removed: " + ", ".join(bad))
        refs = _strs(it.get("qa_refs") or [])
        okrefs = [r for r in refs if r in ctx["qa_ids"]]
        if len(okrefs) != len(refs):
            flags.append("unknown qa_refs removed: " + ", ".join(r for r in refs if r not in ctx["qa_ids"]))
        boxes = [{"token": real, "bbox": b["bbox"]} for k, real in good if real in d["only_cur"] for b in d["cur_boxes"].get(real, [])]
        if not boxes:       # only removed text: show where it used to be, shifted onto the new sheet
            dx, dy = d["offset"]
            boxes = [{"token": real, "bbox": _rb([b["bbox"][0] + dx, b["bbox"][1] + dy, b["bbox"][2] + dx, b["bbox"][3] + dy])}
                     for k, real in good for b in d["old_boxes"].get(real, [])]
            flags.append("removed text only: box is estimated from the old sheet")
        boxes = boxes[:MAX_BOXES]
        n[(it["addendum"], it["sheet_number"])] += 1
        out = {"id": f"{it['addendum']}-{it['sheet_number'].strip()}-{n[(it['addendum'], it['sheet_number'])]}",
               "sheet_number": it["sheet_number"].strip(), "addendum": it["addendum"], "addendum_label": LABEL.get(it["addendum"], it["addendum"]),
               "sheet": d["sheet"], "sheet_is_current": d["current"], "what_changed": it["what_changed"].strip(),
               "trades_affected": _strs(it["trades_affected"]), "risk": it["risk"].strip(),
               "evidence_tokens": [real for _, real in good], "qa_refs": okrefs, "flags": flags, "boxes": boxes,
               "qa": [{k: ctx["qa"][r][k] for k in ("id", "source", "question", "response")} for r in okrefs],
               "crop": None, "crop_rect_pt": None}
        if boxes:
            u = _union([b["bbox"] for b in boxes])
            half = max(check.HALF_PT, min(900.0, max(u[2] - u[0], u[3] - u[1]) / 2 + 60))
            rect = check.crop_rect(u, d["width"], d["height"], half)
            try:
                out["crop"] = _render_crop(d["sheet"], rect, build)
                out["crop_rect_pt"] = [round(v, 1) for v in rect]
                for b in boxes:
                    b["box_2d_on_crop"] = verify.pt_to_box(b["bbox"], rect)
                    b["box_2d_on_crop"] = [max(0, min(1000, v)) for v in b["box_2d_on_crop"]]
            except Exception as e:     # a crop that cannot be drawn must not lose the item
                out["flags"].append(f"crop not rendered: {type(e).__name__}")
        kept.append(out)
    return kept, dropped


# ---- run ---------------------------------------------------------------------------------------------------
def run(build=None):
    """Build the context, make the single Neon call, verify, and write build/digest.json atomically."""
    build = Path(build or BUILD)
    ctx = build_context(build)
    print(f"digest: ~{ctx['est_tokens']} input tokens (bytes/1.25 estimate, budget {TOKEN_BUDGET}), "
          f"{len(ctx['diffs'])} changed sheets, {len(ctx['qa_ids'])} Q&As", file=sys.stderr)
    res = llm.call(ROLES["audit"], [ctx["text"]], schema=SCHEMA, purpose="digest", thinking=THINKING)
    raw = (res.json or {}).get("items") if isinstance(res.json, dict) else None
    if not isinstance(raw, list):
        raise DigestError("model returned no items list")
    kept, dropped = verify_items(raw, ctx, build)
    order = {k: i for i, k in enumerate(ctx["diffs"])}
    kept.sort(key=lambda i: (i["addendum"], order.get((i["addendum"], i["sheet_number"]), 0)))
    out = {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "model": ROLES["audit"],
           "thinking": THINKING, "est_tokens": ctx["est_tokens"], "usage": res.usage, "cost_usd": res.cost_usd,
           "cached": res.cached, "latency_s": res.latency_s, "changed_sheets": len(ctx["diffs"]), "qa_count": len(ctx["qa_ids"]),
           "raw_items": len(raw), "items": kept, "dropped": dropped}
    p = build / "digest.json"
    tmp = p.with_name(f"digest.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    os.replace(tmp, p)
    return out


def load(build=None):
    p = Path(build or BUILD) / "digest.json"
    return json.loads(p.read_text()) if p.exists() else None


if __name__ == "__main__":
    o = run()
    print(json.dumps({k: o[k] for k in ("model", "est_tokens", "usage", "cost_usd", "cached", "raw_items")}
                     | {"kept": len(o["items"]), "dropped": len(o["dropped"])}, indent=1))
