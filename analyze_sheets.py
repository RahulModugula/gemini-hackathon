"""Read title-block sheet numbers from build/words and preview the supersession map.

Heuristic (ticket 5 seed, not final): the sheet number is the tallest token matching a sheet-number
pattern inside the bottom-right title-block region. Unmatched sheets are listed, never guessed.

Usage: python analyze_sheets.py [--build build]  -> writes build/sheet_index.json, prints a summary
"""
import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

SHEET_RE = re.compile(r"^(?:[A-Z]{1,3})-?\d{1,4}(?:\.\d{1,2})?[A-Z]?$")
DISCIPLINE = {"G": "General", "C": "Civil", "L": "Landscape", "S": "Structural", "A": "Architectural",
              "AD": "Arch demo", "I": "Interiors", "P": "Plumbing", "PD": "Plumbing demo",
              "M": "Mechanical", "MD": "Mech demo", "E": "Electrical", "ED": "Elec demo",
              "FP": "Fire protection", "FA": "Fire alarm", "T": "Technology", "Q": "Equipment"}


def find_sheet_number(words, width, height):
    cands = []
    for w in words:
        x0, y0, x1, y1 = w["bbox"]
        if x0 < width * 0.80 or y0 < height * 0.70:
            continue
        t = w["text"].strip().upper()
        if SHEET_RE.match(t) and any(c.isdigit() for c in t):
            cands.append((y1 - y0, y1, t))  # tallest, then lowest on the page
    return max(cands)[2] if cands else None


def prefix(num):
    m = re.match(r"^([A-Z]+)", num or "")
    return m.group(1) if m else "?"


def main(build="build"):
    build = Path(build)
    idx = json.loads((build / "sheets.json").read_text())
    rows = []
    for s in idx["sheets"]:
        wf = json.loads((build / s["words_file"]).read_text())
        num = find_sheet_number(wf["words"], wf["width"], wf["height"])
        rows.append({"sheet": s["sheet"], "source": s["source"], "issue_order": s["issue_order"],
                     "sheet_number": num, "discipline": DISCIPLINE.get(prefix(num), prefix(num)),
                     "word_count": s["word_count"], "tiles": len(s["tiles"])})
    by_num = defaultdict(list)
    for r in rows:
        if r["sheet_number"]:
            by_num[r["sheet_number"]].append(r)
    conflicts = []
    for num, rs in by_num.items():
        top = max(r["issue_order"] for r in rs)
        tops = [r for r in rs if r["issue_order"] == top]
        if len(tops) > 1:  # same number twice in one issue: list it, don't guess
            conflicts.append({"sheet_number": num, "sheets": [r["sheet"] for r in tops]})
            for r in rs:
                r["current"], r["superseded_by"] = None, None
            continue
        cur = tops[0]
        for r in rs:
            r["current"] = r is cur
            r["superseded_by"] = None if r is cur else cur["sheet"]
    for r in rows:
        r.setdefault("current", None)
        r.setdefault("superseded_by", None)
    unmatched = [r["sheet"] for r in rows if not r["sheet_number"]]
    base_nums = {r["sheet_number"] for r in rows if r["source"] == "dwg" and r["sheet_number"]}
    add_only = sorted({r["sheet_number"] for r in rows if r["source"] != "dwg"
                       and r["sheet_number"] and r["sheet_number"] not in base_nums})
    summary = {
        "sheets": len(rows),
        "by_source": Counter(r["source"] for r in rows),
        "by_discipline_base": Counter(r["discipline"] for r in rows if r["source"] == "dwg"),
        "superseded": sorted(r["sheet_number"] for r in rows if r.get("superseded_by")),
        "revised_twice": sorted(n for n, rs in by_num.items()
                                if len({r["issue_order"] for r in rs}) >= 3),
        "conflicts": conflicts,
        "new_in_addenda": add_only,
        "unmatched": unmatched,
        "words_total": sum(r["word_count"] for r in rows),
        "tiles_total": sum(r["tiles"] for r in rows),
    }
    (build / "sheet_index.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="build")
    main(ap.parse_args().build)
