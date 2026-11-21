"""BM25 text search over drawing words (ticket T3b).

Three kinds of documents are indexed per sheet:
  line    one text line, rebuilt from (block, line) keys
  window  two adjacent lines in the same block (phrases split across lines)
  cell    a spatial neighbourhood: all words in a CELL x CELL pt square, squares placed every STRIDE pt
          (so any two words within STRIDE pt of each other in x and y share a cell). A room tag (105A) and
          the fixture tags beside it (WC-3) are near each other on the plan but not on one text line.
Lines+windows and cells are scored as two BM25 corpora (own idf / avg length) and merged by score.
Boxes are PDF points in rendered page space.

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
# 240 pt cells every 120 pt. 120 pt chosen once from P101: room tag 105A has fixture tags WC-3 28 pt and
# WC-2 71 pt away (centre to centre) but WC-1 154 pt away; 120 pt reaches the first two.
CELL, STRIDE = 240.0, 120.0
MIN_CELL_WORDS = 3
OVERLAP_SKIP = 0.5            # a result sharing > half its words with a better one is dropped
COVERAGE_FLOOR = 0.25         # sheet score = best hit * (floor + idf-weighted share of query terms covered)

STOP = {"a", "an", "the", "of", "in", "on", "at", "to", "and", "or", "for", "is", "are", "be", "with", "what", "where"}

SYNONYMS = {
    "wc": ["water", "closet", "toilet"],
    "toilet": ["water", "closet", "wc"],
    "gb": ["grab", "bar"],
    "aff": ["above", "finished", "floor"],
    "clg": ["ceiling"],
    "typ": ["typical"],
    "rcp": ["reflected", "ceiling", "plan"],
    "fa": ["fire", "alarm"],
    "hm": ["hollow", "metal"],
    "cubbies": ["cubby", "cubbie"],
    "cubby": ["cubbies", "cubbie"],
    "cubbie": ["cubbies", "cubby"],
}

_Q = "\"”″"
_FEET_INCH = re.compile(r"(\d+)\s*['′’]\s*-?\s*(?:(\d+)(?:[\s-]+(\d+)/(\d+))?)?\s*[" + _Q + r"]?")
_INCH = re.compile(r"(?:(\d+)[\s-]+)?(\d+)/(\d+)\s*[" + _Q + r"]|(\d+)\s*[" + _Q + "]")
_ALNUM = re.compile(r"[a-z0-9]+")
# Identifiers (room 105a, 151, sheet a201, c110) pin a location; a query naming one means that exact place.
_ID = re.compile(r"^(?:\d{3,}[a-z]?|[a-z]{1,3}\d{2,}[a-z]?)$")
ID_WEIGHT = 2.0


def _inches(whole, num, den):
    v = float(whole or 0) + (float(num) / float(den) if num and den and float(den) else 0.0)
    return f"{v:g}in"


def tokenize(text):
    """Lowercase tokens. 3'-0 1/2" -> 3ft, 0.5in; 1/2" -> 0.5in; 36" -> 36in; WC-1 -> wc, 1, wc1;
    105A.2 -> 105a, 2, 105a2."""
    t = text.lower().replace("''", '"')      # a doubled apostrophe is the typewriter inch mark
    out = []

    def feet(m):
        out.append(f"{m.group(1)}ft")
        if m.group(2) is not None:
            out.append(_inches(m.group(2), m.group(3), m.group(4)))
        return " "

    def inch(m):
        if m.group(4) is not None:
            out.append(_inches(m.group(4), None, None))
        else:
            out.append(_inches(m.group(1), m.group(2), m.group(3)))
        return " "

    t = _FEET_INCH.sub(feet, t)
    t = _INCH.sub(inch, t)
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


def norm_number(s):
    """Sheet number key: C1.10 -> c110, A201 -> a201."""
    return "".join(_ALNUM.findall(s.lower()))


def _union(bbs):
    return [min(b[0] for b in bbs), min(b[1] for b in bbs), max(b[2] for b in bbs), max(b[3] for b in bbs)]


def merge_lines(words):
    """words (contract shape) -> list of dict(key=(block,line), text, bbox, wids) ordered by (block, line).
    wids are indices into `words`."""
    groups = defaultdict(list)
    for i, w in enumerate(words):
        groups[(w["block"], w["line"])].append((i, w))
    lines = []
    for key in sorted(groups):
        ws = sorted(groups[key], key=lambda p: p[1]["word"])
        text = " ".join(w["text"] for _, w in ws).strip()
        if not text:
            continue
        lines.append({"key": key, "text": text, "bbox": _union([w["bbox"] for _, w in ws]),
                      "wids": frozenset(i for i, _ in ws)})
    return lines


def make_docs(lines):
    """Single lines plus windows of two adjacent lines in the same block. Each doc: text, bbox, keys, wids."""
    docs = [{"text": l["text"], "bbox": l["bbox"], "keys": {l["key"]}, "wids": l["wids"], "kind": "line"}
            for l in lines]
    for a, b in zip(lines, lines[1:]):
        if a["key"][0] == b["key"][0]:
            docs.append({"text": a["text"] + " " + b["text"], "bbox": _union([a["bbox"], b["bbox"]]),
                         "keys": {a["key"], b["key"]}, "wids": a["wids"] | b["wids"], "kind": "window"})
    return docs


def make_cells(words, skip=frozenset()):
    """Overlapping CELL x CELL neighbourhoods keyed by word centre. -> list of dict(text,bbox,wids)."""
    grid = defaultdict(list)
    for i, w in enumerate(words):
        if i in skip or not w["text"].strip():
            continue
        cx, cy = (w["bbox"][0] + w["bbox"][2]) / 2, (w["bbox"][1] + w["bbox"][3]) / 2
        gi, gj = int(cx // STRIDE), int(cy // STRIDE)
        for a in (gi - 1, gi):          # squares [a*STRIDE, a*STRIDE+CELL) containing the centre
            for b in (gj - 1, gj):
                grid[(a, b)].append(i)
    cells = []
    for ids in grid.values():
        if len(ids) < MIN_CELL_WORDS:
            continue
        cells.append({"text": " ".join(words[i]["text"] for i in ids),
                      "bbox": _union([words[i]["bbox"] for i in ids]), "wids": frozenset(ids), "kind": "cell"})
    return cells


class _Bm25:
    def __init__(self):
        self.docs, self.lens, self.post = [], [], defaultdict(list)

    def add(self, doc, toks):
        did = len(self.docs)
        self.docs.append(doc)
        self.lens.append(len(toks))
        tf = defaultdict(int)
        for t in toks:
            tf[t] += 1
        for t, c in tf.items():
            self.post[t].append((did, c))

    def finish(self):
        n = len(self.docs)
        self.avgdl = sum(self.lens) / max(n, 1)
        self.idf = {t: math.log(1 + (n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.post.items()}

    def score(self, terms, use_idf=True):
        scores = defaultdict(float)
        for t, w in terms.items():
            idf = self.idf.get(t, 0.0) if use_idf else 1.0
            for did, tf in self.post.get(t, ()):
                scores[did] += w * idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * self.lens[did] / self.avgdl))
        return scores


class Index:
    def __init__(self, build=BUILD):
        build = Path(build)
        sheets = json.load(open(build / "sheets.json"))["sheets"]
        rows = {r["sheet"]: r for r in json.load(open(build / "sheet_index.json"))["rows"]}
        self.meta = {}
        self.by_number = defaultdict(list)
        for s in sheets:
            r = rows.get(s["sheet"], {})
            self.meta[s["sheet"]] = {"sheet_number": r.get("sheet_number"), "current": r.get("current"),
                                     "superseded_by": r.get("superseded_by")}
            if r.get("sheet_number"):
                self.by_number[norm_number(r["sheet_number"])].append(s["sheet"])
        self.lw, self.cell = _Bm25(), _Bm25()
        for s in sheets:
            sid = s["sheet"]
            wf = build / s["words_file"]
            if not s.get("has_text_layer") or not wf.exists():
                continue
            words = json.load(open(wf))["words"]
            own = norm_number(self.meta[sid]["sheet_number"] or "")
            lines, skip = [], set()
            for l in merge_lines(words):
                # A line that is just another sheet's number (G000's sheet index, cross-references) names a
                # sheet, it is not content about it. Only a sheet's own number (title block) is kept.
                n = norm_number(l["text"])
                if n in self.by_number and n != own:
                    skip |= l["wids"]
                else:
                    lines.append(l)
            for d in make_docs(lines) + make_cells(words, skip):
                toks = tokenize(d["text"])
                d["sheet"], d["tokens"] = sid, frozenset(toks)
                d["words"] = [(i, words[i]["bbox"], frozenset(tokenize(words[i]["text"]))) for i in sorted(d["wids"])] \
                    if d["kind"] == "cell" else None
                (self.cell if d["kind"] == "cell" else self.lw).add(d, toks)
        self.lw.finish()
        self.cell.finish()

    def _terms(self, query):
        base = tokenize(query)
        terms = {t: (ID_WEIGHT if _ID.match(t) else 1.0) for t in base}
        for t in base:
            for s in SYNONYMS.get(t, []):
                terms.setdefault(s, SYN_WEIGHT)
        return terms

    def _penalty(self, sid):
        return SUPERSEDED_PENALTY if self.meta[sid]["current"] is False else 1.0

    def _candidates(self, query, use_idf=True):
        terms = self._terms(query)
        boosted = set()
        for tok in set(tokenize(query)):
            boosted.update(self.by_number.get(tok, ()))
        cand = []
        for bm in (self.lw, self.cell):
            for did, sc in bm.score(terms, use_idf).items():
                d = bm.docs[did]
                if d["sheet"] in boosted:
                    sc *= SHEET_NUM_BOOST
                cand.append((sc * self._penalty(d["sheet"]), d))
        top = max((c[0] for c in cand), default=1.0)
        have = {d["sheet"] for _, d in cand}
        for sid in boosted - have:  # a sheet named in the query is returned even with no text hit
            num = norm_number(self.meta[sid]["sheet_number"])
            d = next((d for d in self.lw.docs if d["sheet"] == sid and d["kind"] == "line"
                      and norm_number(d["text"]) == num), None)
            if d is not None:
                cand.append((top * self._penalty(sid), d))
        cand.sort(key=lambda c: -c[0])
        return cand, terms

    def _hit(self, sc, d, terms):
        bbox = d["bbox"]
        if d["kind"] == "cell":   # box only the words that matched, not the whole neighbourhood
            m = [bb for _, bb, toks in d["words"] if toks & terms.keys()]
            if m:
                bbox = _union(m)
        m = self.meta[d["sheet"]]
        return {"sheet": d["sheet"], "sheet_number": m["sheet_number"], "current": m["current"],
                "superseded_by": m["superseded_by"], "bbox": bbox, "score": round(sc, 4),
                "snippet": d["text"][:160], "kind": d["kind"]}

    def search(self, query, k=10, use_idf=True):
        if k <= 0:
            return []
        cand, terms = self._candidates(query, use_idf)
        out, used, boxes = [], set(), set()
        for sc, d in cand:
            ks = {(d["sheet"], i) for i in d["wids"]}
            if len(ks & used) > OVERLAP_SKIP * len(ks):
                continue
            used |= ks
            hit = self._hit(sc, d, terms)
            key = (hit["sheet"], tuple(hit["bbox"]))
            if key in boxes:            # a line and its cell neighbourhoods can box the same word
                continue
            boxes.add(key)
            out.append(hit)
            if len(out) >= k:
                break
        return out

    def search_sheets(self, query, k=3, use_idf=True):
        """Best sheets: one entry per sheet_number (current beats superseded via the penalty). Sheet score is
        the best hit times (COVERAGE_FLOOR + idf-weighted share of query terms found in the sheet's top hits),
        so a sheet matching every concept beats a legend that matches one term many times."""
        if k <= 0:
            return []
        cand, terms = self._candidates(query, use_idf)
        idf = {t: max(self.lw.idf.get(t, 0), self.cell.idf.get(t, 0)) * w for t, w in terms.items()}
        total = sum(idf.values()) or 1.0
        per = defaultdict(list)
        for sc, d in cand:
            if len(per[d["sheet"]]) < 20:
                per[d["sheet"]].append((sc, d))
        ranked = []
        for sid, hs in per.items():
            have = set().union(*(d["tokens"] for _, d in hs))
            cov = sum(v for t, v in idf.items() if t in have) / total
            ranked.append((hs[0][0] * (COVERAGE_FLOOR + cov), sid, hs))
        ranked.sort(key=lambda r: -r[0])
        out, seen = [], set()
        for s, sid, hs in ranked:
            num = self.meta[sid]["sheet_number"] or sid
            if num in seen:
                continue
            seen.add(num)
            out.append({**self._hit(s, hs[0][1], terms), "hits": len(hs),
                        "also_in": sorted(x for _, x, _ in ranked
                                          if x != sid and (self.meta[x]["sheet_number"] or x) == num)})
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
        print(f'{r["score"]:7.2f}  {r["sheet"]:9} {str(r["sheet_number"]):6} {cur:22} {r["kind"]:6} '
              f'{[round(x) for x in r["bbox"]]}  {r["snippet"][:70]}')
