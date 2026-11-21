# PLAN (the one place that stays current)

Last updated 2026-11-21. Details: [TICKETS.md](TICKETS.md) (acceptance criteria, dependency graph, agent ladder) ·
[CONTRACT.md](CONTRACT.md) (data shapes) · [RESEARCH.md](RESEARCH.md) (sources, safe-to-say claims, measurements).

## One line
Say or type a claim about a drawing set; get a card with the sheet crop, a verdict that two Gemini 4 models agree on
**and a deterministic check confirms against the PDF's own text layer**, plus an RFI draft that is blocked if the
addenda already answered it.

## Scope
**MVP (freeze ~5:15):** read the public set (text layer, sheet list, current revision) · claim → sheet → crop →
two-model judgement + code verifier → card · RFI draft with already-answered block · ledger UI with crops ·
scored numbers on 20 held-out claims.

**v1 (judge-driven):** whole-bid-set audit in one ~680K-token context (T17) · rolling live clips (T18) · paper markup (T19) ·
site-walk video vs drawings (T21) · Managed Agents auditor→verifier handoff (T22).

**Out of scope:** any employer's data or code · non-Gemini-4 models · sending anything without a human click · Genie 4.

## Spec
- Models (decided 2026-11-21 from `studio/compare_models.py`: all four 3/3 and fully grounded on 3 dev packs;
  Neon 11.5 s and 2.5x the cost of the others): **Argon = primary judge**, **Carbon = second judge** (run in
  parallel; disagreement → unclear) and whole-set audit, **Neon = audit/schedules/background agent**,
  **Hydrogen = basic work** (claim extraction, audio → claims, query rewriting, already-answered confirm).
  Roles live in one place (`roles.py`). Re-decide on the larger addenda dev set.
- Verdicts: conflict | consistent | unclear with confidence. `unclear` unless both models agree **and** the grounding check passes.
- Card: see CONTRACT.md (claim, speaker, timestamp, verdict, confidence, evidence with sheet/revision/box/crop,
  spec refs, checks, RFI draft).
- Targets, **unmeasured until T13**: card ≤ 6 s from a clip; typed check ≤ 15 s; hard $ cap per run (T0).
- Rules: public data only · every model call logged with cost · unreadable is listed, not guessed.

## Status
| #  | Ticket | Owner | State |
|----|--------|-------|-------|
| T0 | `llm.py` gateway: log, cost cap, replay cache | Claude lane | **next**: blocks every model call |
| T1 | Model check | Claude lane | **done 2026-11-21**: hydrogen, neon, carbon, argon all answered `models_check.py` |
| T2 | Public data | Claude lane | done: `fetch_data.sh` + `data/SHA256SUMS` |
| T3 | Ingest | Claude lane | done: 76 sheets, all with text layer, 4 tests green |
| T3b | `search.py` | Claude lane | open |
| T4 | CONTRACT.md | Claude lane | done |
| T5 | Sheet index, supersession | Antigravity | seeded: 76/76 numbered, 20 superseded; titles/revision dates open |
| T6 | Judge prompt | You, AI Studio | open: dev set only |
| T7–T11 | check.py, verifier, audio, UI, RFI | Antigravity | open |
| T12 | claims.json 10 dev + 20 held-out | You | open: **freeze before any run** |
| T13–T16 | eval, backup, ship, pitch | mixed | open |

## Decisions needed from you
1. ~~API key~~ done: all four models answer. Carbon/Argon prices double Dec 1; set the T0 $ cap accordingly.
2. **Source for the "$5K Managed Agents track"**: research found no such event or rules. Confirm before T22 drives scope.
3. Eval split: 10 dev + 20 held-out (proposed; the original said 30 labelled and 20 held-out).
4. GitHub: create a private repo and open T0–T22 as issues? (Nothing is pushed yet.)

## Pitch guardrails
Only claims marked VERIFIED in RESEARCH.md go on a slide. Never: "1M output in one request", the 4–14.4%
hallucination range, "SWE-sweep in Antigravity", or vendor accuracy numbers. The RFI cost (~$1,080) is "reported".

## Feedback loop
FEEDBACK.md: time, who, what they said, action. Ask each judge: what felt unconvincing; what would you test on a
real job; what would stop you trusting a card. Triage: bug (fix now) / missing piece (v1 ticket) / positioning (pitch).
