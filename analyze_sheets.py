"""Read title-block sheet numbers, titles and revisions from build/words; preview the supersession map.

Heuristic (ticket 5, not final): the sheet number is the tallest token matching a sheet-number
pattern inside the bottom-right title-block region. Unmatched sheets are listed, never guessed.

Title block layout (learned from words files; identical on base, addenda and the rotated civil sheets
because word boxes are already in rendered space): a column at x >= 0.88 * width with labels "Sheet:",
"Scale:", "Revisions: No: Date: Description:", "Date:" (issue date), "Project No.:", "Sheet No.:".
The sheet title is the large text between the "Sheet:" and "Scale:" labels; revision rows sit between the
"Description:" header and the issue-date "Date:" label (No., date, description, e.g. `1 6/12/26 ADDENDUM #01`).

Also parses each addendum narrative (pages 2+ of data/addendum-N.pdf) for the revised-sheet list and the
per-sheet REVISE/ADD/REMOVE bullets, and cross-checks that list against the sheets bound into the addendum.

Usage: python analyze_sheets.py [--build build] [--data data]
  -> writes build/sheet_index.json and build/addenda_changes.json, prints a summary
"""
import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

SHEET_RE = re.compile(r"^(?:[A-Z]{1,3})-?\d{1,4}(?:\.\d{1,2})?[A-Z]?$")
DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/(?:\d{2}|\d{4})$")
ACTION_RE = re.compile(r"^(REVISE|REVISED|ADD|ADDED|REMOVE|REMOVED)\b\s*:?\s*(.*)$", re.I)
ENTRY_RE = re.compile(r"^([A-Z]{1,3}-?\d{1,4}(?:\.\d{1,2})?[A-Z]?)\s*[-–—]\s*(.+?)\s*$")
ACTION_NORM = {"REVISE": "REVISE", "REVISED": "REVISE", "ADD": "ADD", "ADDED": "ADD",
               "REMOVE": "REMOVE", "REMOVED": "REMOVE"}
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


def _column(words, width):
    return [w for w in words if w["bbox"][0] >= width * 0.88]


def find_title(words, width, height):
    """Tall text in the title-block column between the 'Sheet:' and 'Scale:' labels; None if not found."""
    col = _column(words, width)
    sheet_l = [w for w in col if w["text"] == "Sheet:" and w["bbox"][1] > height * 0.55]
    scale_l = [w for w in col if w["text"] == "Scale:" and w["bbox"][1] > height * 0.55]
    if not sheet_l or not scale_l:
        return None
    sl, sc = sheet_l[0]["bbox"], scale_l[0]["bbox"]
    body = [w for w in col if w["bbox"][0] >= sl[0] - 5 and sl[3] <= w["bbox"][1] < sc[1]]
    if not body:
        return None
    tall = max(w["bbox"][3] - w["bbox"][1] for w in body)
    body = [w for w in body if w["bbox"][3] - w["bbox"][1] >= 0.7 * tall]  # drops scale bars, small notes
    body.sort(key=lambda w: (round(w["bbox"][1] / 10), w["bbox"][0]))
    return " ".join(w["text"] for w in body).strip() or None


def find_revisions(words, width, height):
    """(issue_date, [{no, date, description}]) from the title block. Either may be None / empty."""
    col = _column(words, width)
    low = [w for w in col if w["bbox"][1] > height * 0.55]
    desc = [w for w in low if w["text"] == "Description:"]
    dates = sorted((w for w in low if w["text"] == "Date:"), key=lambda w: w["bbox"][1])
    if not dates:
        return None, []
    d = dates[-1]["bbox"]  # lowest 'Date:' label is the issue date; the other is the revisions header
    below = [w for w in col if d[3] <= w["bbox"][1] < d[3] + 40 and w["bbox"][0] >= d[0] - 5
             and DATE_RE.match(w["text"])]
    issue = below[0]["text"] if below else None
    revs = []
    if desc and len(dates) >= 2:
        y0, y1, x0 = desc[0]["bbox"][3], d[1], desc[0]["bbox"][0] - 200
        lines = defaultdict(list)
        for w in sorted((w for w in col if w["bbox"][0] >= x0 and y0 < w["bbox"][1] < y1),
                        key=lambda w: (w["bbox"][1], w["bbox"][0])):
            key = next((k for k in lines if abs(k - w["bbox"][1]) <= 6), w["bbox"][1])
            lines[key].append(w)
        for k in sorted(lines):
            toks = [w["text"] for w in sorted(lines[k], key=lambda w: w["bbox"][0])]
            # a real revision row is: integer, date, description. Stray notes (M501 "IS FORMED") fail this.
            if len(toks) >= 3 and toks[0].isdigit() and DATE_RE.match(toks[1]):
                revs.append({"no": int(toks[0]), "date": toks[1], "description": " ".join(toks[2:])})
    return issue, revs


def parse_addendum_narrative(pdf_path):
    """Parse an addendum's narrative pages into {cover_claim, sheets: [{sheet_number, title, changes}]}.

    Sheets come from entries like `A201 - FIRST FLOOR PLAN` under the 'Drawings:' heading, until
    'End of Addendum'. Changes are REVISE/ADD/REMOVE bullets ({action, text}); wrapped lines (including
    across a page break) are joined to the previous bullet.
    """
    import pymupdf
    doc = pymupdf.open(pdf_path)
    m = re.search(r"(\d+)\s+revised\s+sheets", doc[0].get_text(), re.I)
    lines = []
    for i in range(1, len(doc)):
        if doc[i].rect.width > 1000:  # first drawing sheet: the narrative is over
            break
        lines += [ln.strip() for ln in doc[i].get_text().splitlines()]
    out, cur, change, in_drawings = [], None, None, False
    for ln in lines:
        if not ln or ln == "•" or re.fullmatch(r"\d{1,2}", ln):  # blanks, bullets, page numbers
            continue
        if re.match(r"End of Addendum", ln, re.I):
            break
        if ln.lower().startswith("drawings:"):
            in_drawings = True
            continue
        if not in_drawings:
            continue
        e = ENTRY_RE.match(ln)
        if e:
            cur = next((s for s in out if s["sheet_number"] == e.group(1)), None)
            if cur is None:
                cur = {"sheet_number": e.group(1), "title": e.group(2), "changes": []}
                out.append(cur)
            change = None
            continue
        a = ACTION_RE.match(ln)
        if a and cur is not None:
            change = {"action": ACTION_NORM[a.group(1).upper()], "text": a.group(2).strip()}
            cur["changes"].append(change)
        elif change is not None and not ln.endswith(":"):  # wrapped bullet text
            change["text"] = (change["text"] + " " + ln).strip()
    return {"cover_claim": int(m.group(1)) if m else None, "sheets": out}


def prefix(num):
    m = re.match(r"^([A-Z]+)", num or "")
    return m.group(1) if m else "?"


def main(build="build", data="data"):
    build = Path(build)
    idx = json.loads((build / "sheets.json").read_text())
    rows = []
    for s in idx["sheets"]:
        wf = json.loads((build / s["words_file"]).read_text())
        W, H = wf["width"], wf["height"]
        num = find_sheet_number(wf["words"], W, H)
        issue, revs = find_revisions(wf["words"], W, H)
        latest = max(revs, key=lambda r: r["no"]) if revs else None
        rows.append({"sheet": s["sheet"], "source": s["source"], "issue_order": s["issue_order"],
                     "sheet_number": num, "title": find_title(wf["words"], W, H),
                     "revision": f'{latest["date"]} {latest["description"]}' if latest else issue,
                     "revision_no": latest["no"] if latest else 0, "issue_date": issue,
                     "revisions": revs, "discipline": DISCIPLINE.get(prefix(num), prefix(num)),
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

    changes, crosscheck = [], {}
    for src in sorted({r["source"] for r in rows if r["source"] != "dwg"}):
        pdf = Path(data) / f"addendum-{src[-1]}.pdf"
        if not pdf.exists():  # listed, never dropped
            crosscheck[src] = {"error": f"missing {pdf}"}
            continue
        nar = parse_addendum_narrative(pdf)
        listed = [x["sheet_number"] for x in nar["sheets"]]
        bound = [r["sheet_number"] for r in rows if r["source"] == src and r["sheet_number"]]
        changes += [{"source": src, **x} for x in nar["sheets"]]
        crosscheck[src] = {
            "cover_claim_revised_sheets": nar["cover_claim"], "narrative_listed": len(listed),
            "bound": len(bound), "matched": sorted(set(listed) & set(bound)),
            "bound_not_listed": sorted(set(bound) - set(listed)),
            "listed_not_bound": sorted(set(listed) - set(bound)),
            "cover_claim_matches_listed": nar["cover_claim"] == len(listed)}
    (build / "addenda_changes.json").write_text(json.dumps(changes, indent=1))

    summary = {
        "sheets": len(rows),
        "titles_found": sum(1 for r in rows if r["title"]),
        "unmatched_titles": [r["sheet"] for r in rows if not r["title"]],
        "revisions_found": sum(1 for r in rows if r["revision"]),
        "unmatched_revisions": [r["sheet"] for r in rows if not r["revision"]],
        "addenda_crosscheck": crosscheck,
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
    ap.add_argument("--data", default="data")
    a = ap.parse_args()
    main(a.build, a.data)
