"""Index bidder Q&A from the addenda (T11). Usage: python qa_index.py [out_dir]
Writes build/qa_index.json  [{id: "add1-q3", source, page, question, response}]  (ids are 1-based, in document order)
and build/spec_index.json   [{id, source, section, title, page, article, text}] for the revised spec sections bound
into Addendum 1 (letter-size pages after the drawings).
Narrative pages = letter-size pages before the "Drawings:" heading. Page numbers (a lone digit line at the top of
a page) and bullet glyphs are stripped; question/response whitespace is collapsed to single spaces.
`page` is the PDF page (1-based) where the Question starts.
"""
import json
import re
import sys
from pathlib import Path

import pymupdf

DATA = Path(__file__).parent / "data"
BUILD = Path(__file__).parent / "build"
ADDENDA = {"add1": "addendum-1.pdf", "add2": "addendum-2.pdf"}
ADDENDA_NAME = {"add1": "Addendum 1", "add2": "Addendum 2"}
LETTER_MAX_W = 700  # pt; drawing sheets are 3024 wide
_HEAD = re.compile(r"^(Question|Clarification|Specifications|Drawings)\s*:", re.M)


def _clean_page(text):
    out, seen = [], 0
    for ln in text.split("\n"):
        s = ln.strip()
        if seen < 10 and s:
            seen += 1
            if s.isdigit() and len(s) <= 2:  # page number near the top
                continue
        if s == "•":
            continue
        out.append(s)
    return out


def narrative(pdf_path):
    """-> list of (page_no, line) for letter-size pages up to (not incl.) the 'Drawings:' heading."""
    doc = pymupdf.open(pdf_path)
    rows = []
    for i, page in enumerate(doc):
        if page.rect.width > LETTER_MAX_W:
            break
        for ln in _clean_page(page.get_text()):
            if re.match(r"^Drawings\s*:", ln):
                return rows
            rows.append((i + 1, ln))
    return rows


def _ws(s):
    return re.sub(r"\s+", " ", s).strip()


def parse_qa(pdf_path, tag):
    rows = narrative(pdf_path)
    text, page_at, pos = "", [], 0  # page_at: (char offset, page)
    for pg, ln in rows:
        page_at.append((pos, pg))
        text += ln + "\n"
        pos += len(ln) + 1

    def page_of(off):
        p = page_at[0][1]
        for o, pg in page_at:
            if o <= off:
                p = pg
        return p

    heads = list(_HEAD.finditer(text))
    out = []
    for n, m in enumerate(heads):
        if m.group(1) != "Question":
            continue
        end = heads[n + 1].start() if n + 1 < len(heads) else len(text)
        body = text[m.end():end]
        r = re.search(r"^Response\s*:", body, re.M)
        if not r:
            raise ValueError(f"{tag}: question at page {page_of(m.start())} has no Response")
        out.append({"id": f"{tag}-q{len(out) + 1}", "source": ADDENDA_NAME[tag], "page": page_of(m.start()),
                    "question": _ws(body[:r.start()]), "response": _ws(body[r.end():])})
    return out


CHUNK_MAX = 1200
_PARA = re.compile(r"^(?=\d+\.\d+\.\d+\s*$|[A-Z]\.\s*$)", re.M)


def _pack(text):
    """Split an article at paragraph markers (A. / 1.1.2) and pack the paragraphs into chunks of <= CHUNK_MAX chars."""
    out, cur = [], ""
    for para in _PARA.split(text):
        para = _ws(para)
        if not para:
            continue
        if cur and len(cur) + len(para) + 1 > CHUNK_MAX:
            out.append(cur)
            cur = ""
        cur = (cur + " " + para).strip()
    return out + ([cur] if cur else [])


def parse_specs(pdf_path, tag="add1"):
    """Spec sections bound after the drawings: letter-size pages after the first drawing sheet."""
    doc = pymupdf.open(pdf_path)
    seen_drawing, secs = False, []  # secs: [{section, title, pages:[(pg, text)]}]
    for i, page in enumerate(doc):
        if page.rect.width > LETTER_MAX_W:
            seen_drawing = True
            continue
        if not seen_drawing:
            continue
        t = page.get_text()
        m = re.search(r"^SECTION\s+(\d\d \d\d \d\d)[^\n]*\n(.*?)(?=^PART\b|^\d+\.\d+\s*$)", t, re.M | re.S)
        if m:
            secs.append({"section": m.group(1), "title": _ws(m.group(2)).title(), "pages": []})
        if secs:
            secs[-1]["pages"].append((i + 1, t))
    chunks = []
    art = re.compile(r"^(\d+\.\d+)\s*$", re.M)
    for s in secs:
        parts = []
        for pg, t in s["pages"]:  # drop the running header (up to the copyright / addendum marker)
            body = re.sub(r"\A.*?(?:©[^\n]*\n|\(ADDENDUM #1\)\s*\n[^\n]*\n)", "", t, count=1, flags=re.S)
            parts.append((pg, body))
        cur = {"article": "", "heading": "", "page": parts[0][0], "buf": []}
        blocks = []
        for pg, body in parts:
            pos = 0
            for m in art.finditer(body):
                cur["buf"].append(body[pos:m.start()])
                blocks.append(cur)
                nxt = body[m.end():].lstrip(" \n")
                cur = {"article": m.group(1), "heading": _ws(nxt.split("\n", 1)[0]), "page": pg, "buf": []}
                pos = m.end()
            cur["buf"].append(body[pos:])
        blocks.append(cur)
        for b in blocks:
            for k, txt in enumerate(_pack("\n".join(b["buf"]))):
                chunks.append({"id": f"{tag}-s{s['section'].replace(' ', '')}-{b['article'] or '0'}"
                                     + (f"-{k + 1}" if k else ""),
                               "source": "Addendum 1", "section": s["section"], "title": s["title"],
                               "page": b["page"], "article": (b["article"] + " " + b["heading"]).strip(), "text": txt})
    return chunks


def build(out=BUILD, data=DATA):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    qa = []
    for tag, fn in ADDENDA.items():
        qa += parse_qa(Path(data) / fn, tag)
    spec = parse_specs(Path(data) / ADDENDA["add1"])
    (out / "qa_index.json").write_text(json.dumps(qa, indent=1, ensure_ascii=False))
    (out / "spec_index.json").write_text(json.dumps(spec, indent=1, ensure_ascii=False))
    return qa, spec


if __name__ == "__main__":
    qa, spec = build(*(sys.argv[1:2] or [BUILD]))
    for t in ADDENDA:
        print(t, sum(1 for q in qa if q["id"].startswith(t + "-")), "Q&A")
    print("spec chunks:", len(spec), sorted({(c["section"], c["title"]) for c in spec}))
