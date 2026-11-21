"""Ingest drawing sheets: words+bboxes, page tiles, and sheets.json.

Covers the base drawing set and the revised sheets bound into each addendum.
Only drawing-size pages are ingested (letter/legal narrative and spec pages are skipped).

Usage: python ingest.py [--out build] [--pages 1,2,3] [--dpi 150] [--source dwg=path.pdf ...]
"""
import argparse
import json
from pathlib import Path

import pymupdf

# tag -> (path, issue order). Order 0 = base set; higher numbers supersede lower.
SOURCES = {
    "dwg": ("data/outagamie-ymca-childcare-bid-set-drawings.pdf", 0),
    "add1": ("data/addendum-1.pdf", 1),
    "add2": ("data/addendum-2.pdf", 2),
}
DEFAULT_PDF = SOURCES["dwg"][0]
TILE = 1024  # tile edge in rendered pixels
OVERLAP = 64
MIN_DRAWING_EDGE_PT = 17 * 72 * 0.95  # long edge >= ~11x17 counts as a drawing sheet


def sheet_id(tag: str, index: int) -> str:
    """Source + page based id: unique and stable. Title-block numbers are mapped later (ticket 5)."""
    return f"{tag}-p{index + 1:03d}"


def is_drawing(rect) -> bool:
    return max(rect.width, rect.height) >= MIN_DRAWING_EDGE_PT


def _tiles(page, sid, out, dpi):
    scale = 72 / dpi
    pw, ph = round(page.rect.width / scale), round(page.rect.height / scale)
    step = TILE - OVERLAP
    tiles = []
    dl = page.get_displaylist()  # parse page content once, render many clips from it
    mat = pymupdf.Matrix(dpi / 72, dpi / 72)
    for ty, y in enumerate(range(0, max(ph - OVERLAP, 1), step)):
        for tx, x in enumerate(range(0, max(pw - OVERLAP, 1), step)):
            # last tile in a row/column ends exactly on the page edge (no float/rounding strip)
            x1 = page.rect.x1 if x + TILE >= pw else (x + TILE) * scale
            y1 = page.rect.y1 if y + TILE >= ph else (y + TILE) * scale
            clip = pymupdf.Rect(x * scale, y * scale, x1, y1) & page.rect
            name = f"{sid}_r{ty}c{tx}.png"
            dl.get_pixmap(matrix=mat, clip=clip).save(out / "tiles" / name)
            tiles.append({"file": f"tiles/{name}", "row": ty, "col": tx,
                          "bbox_pt": [round(v, 2) for v in clip]})
    return tiles


def ingest_pdf(pdf_path, tag, order, out, pages=None, dpi=150):
    out = Path(out)
    (out / "words").mkdir(parents=True, exist_ok=True)
    (out / "tiles").mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(pdf_path)
    sheets, skipped = [], []
    for i in range(len(doc)):
        if pages and (i + 1) not in pages:
            continue
        page = doc[i]
        if not is_drawing(page.rect):
            skipped.append(i + 1)
            continue
        sid = sheet_id(tag, i)
        w, h = page.rect.width, page.rect.height
        # get_text returns unrotated coordinates; map them into the rendered (rotated) space the
        # tiles use, so boxes and crops agree on rotated sheets (the civil set is /Rotate 270).
        rot = page.rotation_matrix
        words = []
        for x0, y0, x1, y1, t, b, ln, n in page.get_text("words"):
            r = pymupdf.Rect(x0, y0, x1, y1) * rot
            words.append({"text": t, "bbox": [round(r.x0, 2), round(r.y0, 2),
                                              round(r.x1, 2), round(r.y1, 2)],
                          "block": b, "line": ln, "word": n})
        (out / "words" / f"{sid}.json").write_text(json.dumps(
            {"sheet": sid, "source": tag, "page": i + 1, "width": w, "height": h,
             "unit": "pt", "origin": "top-left", "words": words}))
        text_len = sum(len(wd["text"]) for wd in words)
        sheets.append({"sheet": sid, "source": tag, "issue_order": order, "page": i + 1,
                       "width": w, "height": h, "rotation": page.rotation,
                       "text_length": text_len, "word_count": len(words),
                       "has_text_layer": text_len > 0,
                       "words_file": f"words/{sid}.json",
                       "tiles": _tiles(page, sid, out, dpi)})
    return sheets, skipped


def ingest(sources=None, out="build", pages=None, dpi=150):
    """sources: {tag: (path, order)}. Missing files are reported in sheets.json, never silently dropped."""
    sources = sources or SOURCES
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    all_sheets, report = [], []
    for tag, (path, order) in sources.items():
        if not Path(path).exists():
            report.append({"source": tag, "path": str(path), "error": "missing"})
            continue
        sheets, skipped = ingest_pdf(path, tag, order, out, pages, dpi)
        all_sheets += sheets
        report.append({"source": tag, "path": str(path), "issue_order": order,
                       "sheets": len(sheets), "skipped_non_drawing_pages": skipped})
    (out / "sheets.json").write_text(json.dumps(
        {"dpi": dpi, "tile_px": TILE, "overlap_px": OVERLAP,
         "sources": report, "sheets": all_sheets}, indent=1))
    return all_sheets


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="build")
    ap.add_argument("--pages", help="comma-separated 1-based pages (applied to every source)")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--source", action="append", metavar="TAG=PATH",
                    help="override sources; order follows the flags")
    a = ap.parse_args()
    pg = {int(p) for p in a.pages.split(",")} if a.pages else None
    src = ({t: (p, n) for n, (t, p) in enumerate(s.split("=", 1) for s in a.source)}
           if a.source else None)
    s = ingest(src, a.out, pg, a.dpi)
    no_text = [x["sheet"] for x in s if not x["has_text_layer"]]
    print(f"{len(s)} sheets; {len(s) - len(no_text)} with text layer; no text: {no_text or 'none'}")
