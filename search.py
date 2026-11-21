"""BM25 text search over drawing words (ticket T3b).

Lines are rebuilt from (block, line) keys per sheet; 2-line windows (adjacent lines in the same block) are
indexed too so phrases split across lines still match. Boxes are PDF points in rendered page space.

    python search.py "grab bar mounting height"
"""
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

BUILD = Path(__file__).parent / "build"
K1, B = 1.2, 0.75
SUPERSEDED_PENALTY = 0.5      # multiplicative, applied to superseded sheets
SHEET_NUM_BOOST = 2.0         # multiplicative, for sheets named in the query
SYN_WEIGHT = 0.5              # expanded synonym terms count half

STOP = {"a", "an", "the", "of", "in", "on", "at", "to", "and", "or", "for", "is", "are", "be", "with", "what", "where"}

SYNONYMS = {
    "wc": ["water", "closet", "toilet"],
    "toilet": ["water", "closet", "wc"],
    "gb": ["grab", "bar"],
    "aff": ["above", "finished", "floor"],
    "clg":["ceiling"],
    "typ": ["typical"],
    "rcp": ["reflected", "ceiling", "plan"],
    "fa": ["fire", "alarm"],
    "hm": ["hollow", "metal"],
    "cubbies": ["cubby", "cubbie"],
    "cubby": ["cubbies", "cubbie"],
    "cubbie": ["cubbies", "cubby"],
}

_FT_IN = re.compile(r"(\d+)\s*'\s*-?\s*(\d+(?:\s*\d+/\d+)?)\s*(?:\"|”|″)?")
_FT = re.compile(r"(\d+)\s*(?:'|′|’)")
_IN = re.compile(r"(\d+)\s*(?:\"|”|″)")
_ALNUM = re.compile(r"[a-z0-9]+")


def tokenize(text):
    """Lowercase tokens. 3'-0" -> 3ft, 0in; 36" -> 36in; WC-1 -> wc, 1, wc1; 105A.2 -> 105a, 2, 105a2."""
    t = text.lower()
    t = _FT_IN.sub(lambda m: f" {m.group(1)}ft {re.sub(r'[^0-9]', '', m.group(2))}in ", t)
    t = _FT.sub(r" \1ft ", t)
    t = _IN.sub(r" \1in ", t)
    out = []
    for chunk in t.split():
        parts = _ALNUM.findall(chunk)
        if not parts:
            continue
        out.extend(p for p in parts if p not in STOP)
        if len(parts) > 1 and re.search(r"[-./]", chunk):
            joined = "".join(parts)
            if len(joined) <= 12:
                out.append(joined)
    return out


def merge_lines(words):
    """words (contract shape) -> list of dict(key=(block,line), text, bbox) ordered by (block, line)."""
    groups = defaultdict(list)
    for w in words:
        groups[(w["block"], w["line"])].append(w)
    lines = []
    for key in sorted(groups):
        ws = sorted(groups[key], key=lambda w: w["word"])
        text = " ".join(w["text"] for w in ws).strip()
        if not text:
            continue
        bb = [min(w["bbox"][0] for w in ws), min(w["bbox"][1] for w in ws),
              max(w["bbox"][2] for w in ws), max(w["bbox"][3] for w in ws)]
        lines.append({"key": key, "text": text, "bbox": bb})
    return lines


def make_docs(lines):
    """Single lines plus windows of two adjacent lines in the same block. Each doc: text, bbox, keys."""
    docs = [{"text": l["text"], "bbox": l["bbox"], "keys": {l["key"]}} for l in lines]
    for a, b in zip(lines, lines[1:]):
        if a["key"][0] == b["key"][0]:
            bb = [min(a["bbox"][0], b["bbox"][0]), min(a["bbox"][1], b["bbox"][1]),
                  max(a["bbox"][2], b["bbox"][2]), max(a["bbox"][3], b["bbox"][3])]
            docs.append({"text": a["text"] + " " + b["text"], "bbox": bb, "keys": {a["key"], b["key"]}})
    return docs


class Index:
    def __init__(self, build=BUILD):
        build = Path(build)
        sheets = json.load(open(build / "sheets.json"))["sheets"]
        rows = {r["sheet"]: r for r in json.load(open(build / "sheet_index.json"))["rows"]}
        self.meta = {}
        self.docs = []        # (sheet, text, bbox, keys)
        self.doc_len = []
        self.post = defaultdict(list)  # token -> [(doc_id, tf)]
        self.by_number = defaultdict(list)
        for s in sheets:
            sid = s["sheet"]
            r = rows.get(sid, {})
            self.meta[sid] = {"sheet_number": r.get("sheet_number"), "current": r.get("current"),
                              "superseded_by": r.get("superseded_by")}
            if r.get("sheet_number"):
                self.by_number[r["sheet_number"].lower()].append(sid)
            wf = build / s["words_file"]
            if not s.get("has_text_layer") or not wf.exists():
                continue
            lines = merge_lines(json.load(open(wf))["words"])
            for d in make_docs(lines):
                did = len(self.docs)
                toks = tokenize(d["text"])
                self.docs.append((sid, d["text"], d["bbox"], d["keys"]))
                self.doc_len.append(len(toks))
                tf = defaultdict(int)
                for t in toks:
                    tf[t] += 1
                for t, c in tf.items():
                    self.post[t].append((did, c))
        n = len(self.docs)
        self.avgdl = sum(self.doc_len) / max(n, 1)
        self.idf = {t: math.log(1 + (n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.post.items()}

    def _terms(self, query):
        base = tokenize(query)
        terms = {t: 1.0 for t in base}
        for t in base:
            for s in SYNONYMS.get(t, []):
                terms.setdefault(s, SYN_WEIGHT)
        return terms

    def search(self, query, k=10, use_idf=True):
        scores = defaultdict(float)
        for t, w in self._terms(query).items():
            idf = self.idf.get(t, 0.0) if use_idf else 1.0
            for did, tf in self.post.get(t, ()):
                norm = tf * (K1 + 1) / (tf + K1 * (1 - B + B * self.doc_len[did] / self.avgdl))
                scores[did] += w * idf * norm
        boosted = set()
        for tok in set(tokenize(query)):
            boosted.update(self.by_number.get(tok, ()))
        cand = []
        for did, sc in scores.items():
            sid = self.docs[did][0]
            if sid in boosted:
                sc *= SHEET_NUM_BOOST
            cand.append((sc * self._penalty(sid), did))
        top = max((c[0] for c in cand), default=1.0)
        for sid in boosted:  # a sheet named in the query is returned even with no text hit
            if not any(self.docs[d][0] == sid for _, d in cand):
                did = next((i for i, d in enumerate(self.docs) if d[0] == sid and d[1].lower() == self.meta[sid]["sheet_number"].lower()), None)
                if did is not None:
                    cand.append((top * self._penalty(sid), did))
        cand.sort(key=lambda c: -c[0])
        out, used = [], set()
        for sc, did in cand:
            sid, text, bbox, keys = self.docs[did]
            ks = {(sid, k_) for k_ in keys}
            if ks & used:   # a window and its member line are one hit
                continue
            used |= ks
            m = self.meta[sid]
            out.append({"sheet": sid, "sheet_number": m["sheet_number"], "current": m["current"],
                        "superseded_by": m["superseded_by"], "bbox": bbox, "score": round(sc, 4),
                        "snippet": text[:160]})
            if len(out) >= k:
                break
        return out

    def _penalty(self, sid):
        return SUPERSEDED_PENALTY if self.meta[sid]["current"] is False else 1.0

    def search_sheets(self, query, k=3, **kw):
        """Best sheets: one entry per sheet_number (current beats superseded via the penalty)."""
        hits = self.search(query, k=200, **kw)
        per = defaultdict(list)
        for h in hits:
            per[h["sheet"]].append(h)
        ranked = []
        for sid, hs in per.items():
            s = hs[0]["score"] + 0.1 * sum(h["score"] for h in hs[1:4])
            ranked.append((s, sid, hs))
        ranked.sort(key=lambda r: -r[0])
        out, seen = [], set()
        for s, sid, hs in ranked:
            num = self.meta[sid]["sheet_number"] or sid
            if num in seen:
                continue
            seen.add(num)
            out.append({**hs[0], "score": round(s, 4), "hits": len(hs),
                        "also_in": sorted(x for _, x, _ in ranked if x != sid and (self.meta[x]["sheet_number"] or x) == num)})
            if len(out) >= k:
                break
        return out


_INDEX = None


def get_index():
    global _INDEX
    if _INDEX is None:
        _INDEX = Index()
    return _INDEX


def search(query, k=10):
    return get_index().search(query, k)


def search_sheets(query, k=3):
    return get_index().search_sheets(query, k)


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "grab bar"
    for r in search(q, 5):
        cur = {True: "current", False: "SUPERSEDED by " + str(r["superseded_by"]), None: "?"}[r["current"]]
        print(f'{r["score"]:7.2f}  {r["sheet"]:9} {str(r["sheet_number"]):6} {cur:22} {[round(x) for x in r["bbox"]]}  {r["snippet"][:80]}')
