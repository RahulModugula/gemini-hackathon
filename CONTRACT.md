# Data contracts

Later pieces (checker, audit, UI) plug into these shapes. Produced by `ingest.py` unless noted.
Coordinates are PDF points, origin top-left, unless a field says otherwise.

## build/sheets.json
```json
{
  "dpi": 150, "tile_px": 1024, "overlap_px": 64,
  "sources": [
    {"source": "dwg",  "path": "data/...drawings.pdf", "issue_order": 0, "sheets": 54, "skipped_non_drawing_pages": []},
    {"source": "add1", "path": "data/addendum-1.pdf",  "issue_order": 1, "sheets": 17, "skipped_non_drawing_pages": [1,2,3,4,5,23,...]},
    {"source": "nope", "path": "...", "error": "missing"}          // missing inputs are listed, never dropped
  ],
  "sheets": [
    {
      "sheet": "dwg-p001",        // stable id = <source>-p<page>; NOT the title-block number (ticket 5 maps it)
      "source": "dwg",            // dwg | add1 | add2
      "issue_order": 0,           // higher supersedes lower for the same title-block number
      "page": 1,                  // 1-based page within that source PDF
      "width": 3024.0, "height": 2160.0, "rotation": 0,   // pt
      "text_length": 1234,        // total chars across words
      "word_count": 210,
      "has_text_layer": true,     // false => needs vision; words file will be empty; list it, don't guess
      "words_file": "words/dwg-p001.json",
      "tiles": [ {"file": "tiles/dwg-p001_r0c0.png", "row": 0, "col": 0, "bbox_pt": [x0, y0, x1, y1]} ]
    }
  ]
}
```
Only drawing-size pages (long edge >= ~17 in) are sheets; addendum narrative and spec pages are skipped
and listed in `skipped_non_drawing_pages`. Tiles are `tile_px` at `dpi` with `overlap_px` overlap;
`bbox_pt` is the tile's region on the page. pt -> tile px: `(x - bbox_pt[0]) * dpi / 72`.

**Rotation:** `width`/`height` are `page.rect` *after* `/Rotate` (the 8 civil/landscape sheets are 270°).
Word boxes and tile `bbox_pt` are in that rendered space. PyMuPDF calls that take unrotated coordinates
(`get_text(clip=)`, `search_for`) need `rect * page.derotation_matrix` first.

## build/sheet_index.json (`analyze_sheets.py`, ticket 5)
```json
{
  "summary": {"sheets": 76, "titles_found": 75, "unmatched_titles": ["dwg-p001"], "revisions_found": 76,
              "unmatched_revisions": [], "superseded": ["A201", ...], "revised_twice": ["A1100", "ED200"],
              "conflicts": [], "unmatched": [],
              "addenda_crosscheck": {"add1": {"cover_claim_revised_sheets": 16, "narrative_listed": 17, "bound": 17,
                 "matched": [...], "bound_not_listed": [], "listed_not_bound": [], "cover_claim_matches_listed": false,
                 "title_mismatches": [{"sheet_number": "E400", "narrative": "ELECTRICL ...", "title_block": "ELECTRICAL ..."}],
                 "unparsed": []},                      // drawings-section narrative lines the parser could not place
                 "add3": {"error": "missing data/addendum-3.pdf"}},   // input PDF absent: listed, never dropped
              ...},
  "rows": [
    {"sheet": "add2-p007", "source": "add2", "issue_order": 2, "sheet_number": "A1100",
     "title": "INTERIOR FINISH SCHEDULES",            // null if not found (then listed in summary.unmatched_titles)
     "revision": "6/22/26 ADDENDUM #02",              // latest revision row as printed; else the issue date; null if neither
     "revision_no": 2,                                // 0 = no revision rows (original issue)
     "issue_date": "6/02/2026",                       // the title block's own "Date:" (formats vary: 6/02/2026, 06/02/2026)
     "revisions": [{"no": 1, "date": "6/12/26", "description": "ADDENDUM #01"}, {"no": 2, "...": "..."}],
     "discipline": "Architectural", "current": true, "superseded_by": null, "word_count": 812, "tiles": 35}
  ]
}
```
`current`/`superseded_by` are null when the number is unmatched or listed in `conflicts` (same number twice
in one issue). `title` is the large text between the `Sheet:` and `Scale:` labels of the title block; `revision*`
come from the rows between the `Description:` header and the issue-date `Date:` label. Dates are strings as
printed, not normalised. `summary.superseded` is a sorted list of unique sheet numbers (a number revised in
two addenda appears once). `title_mismatches` compares narrative and title-block titles ignoring case and
punctuation; `unparsed` should be empty, anything in it needs a human look. Nothing is guessed: G000 (cover sheet, no `Sheet:`/`Scale:` labels in its text layer)
has `title: null`.

## build/addenda_changes.json (`analyze_sheets.py`, ticket 5)
Parsed from the addendum narratives (`data/addendum-N.pdf`, pages 2+, under "Drawings:"):
```json
[ {"source": "add1", "sheet_number": "A201", "title": "FIRST FLOOR PLAN",
   "changes": [ {"action": "REVISE", "text": "Alternate designation for cubbies in care rooms."},
                {"action": "ADD",    "text": "Detail 1/A1001 and reference to sheet."} ]} ]
```
`action` is `REVISE | ADD | REMOVE` (the narrative's REVISED/REMOVED variants are normalised). Join to
sheet_index rows on `(source, sheet_number)`. `summary.addenda_crosscheck` compares each narrative's sheet list
with the sheets bound into that addendum; mismatches are listed, not resolved.

**Gemini boxes:** Gemini returns `[ymin, xmin, ymax, xmax]` normalised 0–1000 on the image it saw.
Convert to pt on that tile before writing a card: `x = bbox_pt[0] + xmin/1000 * (bbox_pt[2]-bbox_pt[0])`.
Cards always store pt, never model-native boxes.

## build/words/<sheet>.json
```json
{
  "sheet": "dwg-p001", "source": "dwg", "page": 1, "width": 3024.0, "height": 2160.0,
  "unit": "pt", "origin": "top-left",
  "words": [ {"text": "DOOR", "bbox": [x0, y0, x1, y1], "block": 0, "line": 0, "word": 0} ]
}
```

## Card (checker output; consumed by UI/audit)
```json
{
  "claim": "string, one checkable statement",
  "speaker": "string or null",          // from audio; null for typed claims
  "timestamp": "2026-11-21T14:03:12Z",  // when the claim was made (clip time or typed time)
  "verdict": "conflict | consistent | unclear",
  "confidence": 0.0,
  "evidence": [
    {"sheet": "dwg-p012", "revision": "add1", "bbox": [x0, y0, x1, y1], "crop": "build/crops/<file>.png"}
  ],
  "spec_refs": ["08 71 00 2.1.A"],
  "checks": [                           // one entry per model judgement + any code math
    {"by": "gemini-4-hydrogen", "verdict": "conflict", "confidence": 0.8, "note": "..."},
    {"by": "gemini-4-carbon",   "verdict": "conflict", "confidence": 0.7, "note": "..."},
    {"by": "code",              "verdict": "conflict", "note": "3'-0\" + 2'-6\" != 6'-0\""}
  ],
  "rfi_draft": {"text": "...", "status": "draft | blocked", "answered_by": "add1 p2 Q5" } // or null
}
```
Rules:
- `confidence` in [0,1]. Final `verdict` is `unclear` unless both model checks agree (spec rule).
- `evidence[].bbox` is in pt, same space as the words files, and `sheet` must exist in sheets.json.
  `revision` is the source tag of the sheet the crop came from; it must be the current one (ticket 5).
- `rfi_draft` is null when verdict is `consistent`. `status: "blocked"` requires `answered_by`
  pointing at the addendum Q&A that already answers it (ticket 11). Nothing is sent without a human click.
- Every model call behind a card is logged (model, tokens in/out, latency, cost) — see PLAN review note 3.

## Video cards (`POST /api/video`, feat/video-v2)
Each card made from a site video also carries (beside the normal drawing `evidence`):
```json
{"source": "video", "t_start_s": 5.0, "t_end_s": 9.0, "kind": "spoken | visual",
 "video_url": "/videos/<sha1>.mp4",
 "video_evidence": {"frame": "/frames/<20 hex>.jpg", "t_s": 7.0, "box_2d": [ymin, xmin, ymax, xmax] | null,
                    "kind": "spoken | visual", "text_read": "WC-3"}}      // text_read only for visual claims; null if no frame
```
`t_s` is the model's `frame_t_s` (validated into [0, duration], else the middle of the claim's span). `box_2d` is 0-1000
on the frame, found by a second IMAGE call (boxes are documented for images, not video); null for spoken claims.
Visual claims also send the boxed frame region to both judges as a field photo (`check(..., extra_images=[...])`);
verdict semantics and drawing-only evidence boxes are unchanged. `/frames/*` and `/videos/*` serve only
hash-named files. Follow-up (not adopted): the Gemini "agentic" video processing mode.
