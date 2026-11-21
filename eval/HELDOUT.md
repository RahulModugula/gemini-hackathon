# Frozen held-out sets

Frozen files never change. `eval/run_eval.py` refuses to run a claims file whose sha256 differs from its `.sha256` sidecar, and
`eval/test_label_sets.py` fails if either hash moves. Do not tune prompts, retrieval or thresholds against these files.

| file | n | frozen | sha256 (file bytes) |
|---|---|---|---|
| `eval/heldout_v1.json` | 17 | earlier freeze | `a200c8b4db2702e22704f8d829c862d684c370e0a48fc196806adce9759eaa39` |
| `eval/heldout_v2.json` | 43 | 2026-11-21 | `3a5559d25516388d6b04dd24cd186ef9f8116c5e82acc5925a47f4e732ef7c6c` |
| `eval/heldout_v3.json` | 51 | 2026-11-21 | `368fc51cd1753845841500aeb2780d26ed6d6d123a5484a3d1cf341d371a8193` |

## heldout_v2 (2026-11-21)

Method: hand-written in a separate pass from `eval/dev_v3.json`, with no model calls. Each label was derived from the PDF words in
`build/words/<sheet>.json` (exact text line, schedule row or single-word evidence) and, for 14 claims, confirmed on a pymupdf crop.
Unclear claims ask for a fact whose key tokens occur on no current sheet (checked across all 54 current sheets).
Full per-claim evidence: `eval/label_audit_v3.md`.

Composition: 13 consistent, 17 conflict, 13 unclear, over 21 sheets. 16 claims are on unchanged sheets (G000, A700, A1002, C1.10, E300, E500,
E600, M500) and the rest are on addendum-changed sheets but on rows, rooms and notes that dev_v3 does not use
(dev_v3 covers the sheet-level changes; held-out v2 uses different schedule rows, panel circuits, door rows, keynotes and notes).
Most claims do not name their sheet, so retrieval has to find it.

Overlap guard: ids and claim texts are disjoint from dev_v3 and heldout_v1 (checked in `eval/test_label_sets.py`).

To re-verify: `sha256sum eval/heldout_v1.json eval/heldout_v2.json`.

## heldout_v3 (2026-11-21)

Purpose: the single clean final measurement after tuning. Nothing has been run on it; it must be run once, with no changes made afterwards.

Method: hand-written, no model calls, in a separate pass after reading dev_v3, heldout_v1, heldout_v2 and `eval/label_audit_v3.md` only to avoid overlap. Each label was derived from the PDF words in `build/words/<sheet>.json` (exact text line or schedule row) and confirmed on a pymupdf crop of the claim bbox (41 of 51 crops viewed, including every conflict). Unclear claims name a maker or value the sheet never mentions (silence traps, e.g. a brand for an unbranded item); the key token occurs on no current sheet (checked across all 54 current sheets, case-insensitive). Full per-claim evidence: `eval/label_audit_heldout_v3.md`.

Composition: 20 conflict (39%), 18 consistent (35%), 13 unclear (25%), over 12 sheets: S900, S200, M000, ME100, M501, P102, L1.00, C1.20 (unchanged) and A1000, A1001, AD201, E100 (addendum-changed). Phrasings include questions-as-statements, "per Addendum 1/2" and unnamed-sheet claims. Targets are new notes, schedule rows and details that dev_v3, v1 and v2 do not use; AD201 and E100 are shared with v2 but on different notes.

Overlap guard: ids and claim texts are disjoint from dev_v3, heldout_v1 and heldout_v2 (checked in `eval/test_label_sets.py`).

To re-verify: `sha256sum eval/heldout_v3.json`.
