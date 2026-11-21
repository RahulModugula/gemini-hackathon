# Tickets

Each ticket: **goal · depends on · done when (measurable) · owner · risk**. IDs are stable; PLAN.md tracks state.
Design rule for every ticket: *a Gemini capability proposes, a deterministic check disposes.*
("Consistent isn't correct": two models agreeing is not evidence. The PDF's vector text layer is.)

## Critical path (MVP, freeze ~5:15)

```
T0 llm.py ─┬─> T1 models ─> T6 judge prompt ─┬─> T7 check.py ─> T8 verifier ─> T10 UI ─> T15 ship
           │                                 │                                  ▲
T2 data ─> T3 ingest ─> T3b search ──────────┘                                  │
                └─> T5 sheet index ─> T11 RFI + already-answered ───────────────┘
T12 claims.json (frozen) ─> T13 eval ─> T16 pitch          T9 audio ─> T7 (optional input)
T0 replay cache ─> T14 backup run
```

### T0 · `llm.py`: one gateway for every model call  *(new, blocks everything that calls a model)*
- Goal: one function `call(model, parts, schema, purpose)` that every piece uses.
- Does: passes `api_key=` explicitly (SDK lets `GOOGLE_API_KEY` override `GEMINI_API_KEY`); JSON output via
  `response_json_schema`; logs model, purpose, tokens in/out/thinking, latency, $ to `build/calls.jsonl`;
  **hard dollar cap per run** (refuses the call past the cap); retries on 429/5xx with backoff;
  **record/replay cache** keyed by hash(model, parts, schema), so evals rerun free and the demo can run offline.
- Done when: unit test proves the cap refuses; a replayed call makes zero network requests; `calls.jsonl` has every field.
- Owner: Claude lane. Risk: low. ~45 min.

### T1 · Model check  *(exists: `models_check.py`)*
- Done when: four `OK` lines. **Blocked on a billing-enabled key**: only Hydrogen is on the free tier.
- Fallback if Carbon/Argon/Neon refuse: Hydrogen for everything at two thinking levels, and say so in the README.

### T2 · Public data  *(done)*: `./fetch_data.sh`, `data/SHA256SUMS`.

### T3 · Ingest  *(done)* and T3b · `search.py`  *(new)*
- T3b goal: `search(query) -> [(sheet, bbox, score, snippet)]`, BM25 over words merged into lines and a sheet-number index,
  current-revision sheets ranked first.
- Done when: 10 hand-picked queries (e.g. "door hardware 105A", "WC mounting height") return the right sheet in the
  top 3 for >= 9 of 10. Pure code, no model. Risk: drawing text is fragmented; merging into lines is the fix.

### T4 · CONTRACT.md  *(done)*

### T5 · Sheet index and supersession  *(seeded by `analyze_sheets.py`: all 76 sheets numbered, 20 superseded)*
- Goal: every sheet gets `sheet_number, title, revision, current, superseded_by`. Remaining: title, revision
  date from the title block, and the cross-check below.
- Done when: every base sheet is matched (unmatched list empty or each one explained), and every sheet named in the
  addendum narratives (pp. 2–5) is marked superseded. Cross-check: the parser's revised list == the addendum's own list.
- Risk: the Add 1 cover says 16 revised sheets but 17 sheet pages are bound in; find out which is extra.

### T6 · Claim judge prompt  *(you, AI Studio)*
- Input: claim + top-k crops + the words inside each crop. Output: card JSON (CONTRACT) with model-native boxes.
- **Always send the image.** DrawingVQA: Gemini 2.5 Flash scored 66% with the image vs 10% text-only (RESEARCH §3).
- Counting claims ("4 WCs in 105A") are answered by code over the words layer, or marked unclear: VLM symbol
  counting is about 0.40–0.55 (AECV-Bench).
- Done when: >= 8 of 10 **dev** claims right. Never tune on held-out.

### T7 · `check.py`: claim to card
- claim → T3b search (top 3 sheets) → crop tiles → Hydrogen judge (T6) → convert boxes to pt → card.
- Done when: a typed claim gives a card with crop and verdict; p50 latency recorded (goal 15 s, unmeasured).

### T8 · Verifier: second opinion + deterministic checks  *(the "consistent isn't correct" piece)*
- Carbon judges independently (it never sees Hydrogen's answer). They disagree → `unclear`.
- Deterministic checks, each a `checks[]` entry with `by: "code"`:
  1. **Grounding:** the evidence box must overlap PDF words that contain the tokens the model quoted. Otherwise → `unclear`.
  2. **Currency:** the evidence sheet must be `current` (T5). Superseded → re-run on the current sheet.
  3. **Dimension math:** parse feet-inches strings, check sums and comparisons in code, never in the model.
- Done when: a seeded hallucination (model cites words not in its box) is caught 3 of 3; disagreement gives unclear.

### T9 · Audio clip to claims
- Hydrogen hears a wav/webm clip natively and extracts claims with speaker and timestamp, which feed T7.
- Done when: 3 of 3 recorded clips give the expected claims; card within 6 s goal measured, not assumed.

### T10 · One-page UI (ledger)
- FastAPI + one HTML page: the claims ledger, crop beside each card, verdict chip, checks expandable, RFI button.
- Done when: the demo path runs end to end from a cold start in under 60 s.

### T11 · RFI draft + already-answered block
- Index the addenda Q&A (Add 1 and Add 2, pp. 2–5, "Question: / Response:") and the 3 revised spec sections.
- A draft RFI is matched against the index; a hit → `status: blocked`, `answered_by: "add1 p2 Q5"`.
- Done when: one real question from the addenda, rephrased, is blocked with the right source. Nothing is sent.

### T12 · `claims.json`: 30 labelled claims, frozen  *(you)*
- 10 dev + 20 held-out; balanced across conflict/consistent/unclear and across disciplines; >= 5 that need an addendum
  to answer correctly (the supersession trap). Committed (with a sha in PLAN.md) **before** any eval run.

### T13 · Eval harness
- Runs held-out twice. Reports, with n, date and prompt hash (method in RESEARCH §4):
  accuracy (Wilson 95% CI), coverage, selective accuracy, **conflict precision + false-alarm count**, conflict recall,
  Hydrogen–Carbon agreement + kappa + marginals, **Hydrogen-alone baseline** (does the second judge earn its cost?),
  two-run flip count, Brier on 2–3 confidence tiers, grounding pass rate, evidence precision (hand-labelled),
  p50/p95 latency, $ per claim. Results table goes in the README.
- Honest framing for the pitch: at n=20, 18/20 means "[0.70, 0.97] with 95% confidence", nothing tighter.

### T14 · Backup run  *(falls out of the T0 replay cache)*: one clean recorded run plus a UI replay toggle.
### T15 · README, architecture diagram, public URL, QR.  ### T16 · Pitch x3, under 3 minutes.

## v1: Gemini-only moats, each paired with a verifier

| ID  | Moat used                          | Ticket                                                                                     | Verifier that keeps it honest                    | Gate |
|-----|------------------------------------|--------------------------------------------------------------------------------------------|--------------------------------------------------|------|
| T17 | 1M context (GraphWalks: Carbon 96.2%) | **Whole-bid-set audit in one context**: current sheets (~94K tok) + project manual (~457K) + addenda + A201 ≈ **680K tokens (estimate)** in one Carbon call, implicitly cached; finds spec-vs-sheet conflicts across documents. Report via long-decode continuation if it exceeds 65K output | every finding is re-grounded by T8; "could not check" list | measure real tokens with `count_tokens` first; ≈ $1.36 input per pass at Carbon intro price, $2.72 after Dec 1 |
| T18 | Native audio, Live API             | Rolling 8 s live clips (Live API, `api_version="v1beta"`)                                  | same T7/T8 path                                   | — |
| T19 | Vision                             | Paper markup photo lands on the right sheet (image to sheet registration)                  | registered words must match the sheet's text layer | — |
| T21 | Native video                       | **Site-walk video vs drawings**: a phone walk of a room is checked against the current sheet (door swing, fixture count, mounting heights) | timestamped frame + sheet crop side by side; human confirms | native video verified (LVBench Argon 91.7%; ~100 tok/s at low res → a 5 min walk ≈ 30K tokens) |
| T22 | Managed Agents (verified in docs)  | **Auditor → Verifier handoff in one sandbox**: Auditor agent (Neon) runs T17 and writes `findings.json` to `/workspace`; a Verifier agent attaches to the **same environment id with a fresh conversation** and runs our deterministic checks (`ingest.py` words + T8 code) inside the sandbox; a step-trace panel in the UI streams its file writes and commands. Network allowlisted to the county site only; no secrets in the sandbox (public data) | the verifier is code, not a model; ledger diff per run; T0 cost cap | v1beta preview API; the "$5K track" is **unverified**: confirm with the organisers before optimising for it |
| T20 | —                                  | Apply judge feedback (FEEDBACK.md triage)                                                  | —                                                 | — |
| T23 | Vision + vector geometry           | **3D drawing model**: three.js scene from A201's vector paths; cards as pins at real coordinates; Level A textured floor → B extruded walls → C discipline layers (BUILD.md) | geometry comes from the PDF, not a model; pins come only from verified cards | 1 h spike; ship Level A if B isn't solid |
| T24 | Native audio, image, video         | **Multimodal intake**: one input bar for text, voice, markup photo, site video, new PDF; all become claims on the T7/T8 path | one trust path for every modality                 | after T9 |

**Explicitly not now:** Genie 4 world models (no verifier exists for "the world looks right", and it does not serve a
contractor); any non-Gemini-4 model; auto-sending RFIs.

## Long horizon: the agent ladder

| Level | What runs                                                                 | Human role                     | Proof it works                         |
|-------|----------------------------------------------------------------------------|--------------------------------|----------------------------------------|
| L0    | One claim → one card (T7)                                                  | Asks and reads                 | Held-out accuracy with CI              |
| L1    | Two judges + deterministic verifier (T8)                                    | Resolves `unclear`             | Grounding pass rate, kappa             |
| L2    | Background audit of the whole set in one context (T17)                      | Triages a ranked list          | Precision@20 of audit findings         |
| L3    | Standing agent: every new addendum or RFI response re-checks affected cards (T22) | Approves re-opened cards  | Cards correctly re-opened on Add 2     |
| L4    | Field loop: site video + meeting audio become claims that are checked continuously (T21, T18) | Clicks to send an RFI | Time from claim to card; RFIs avoided |

Every level keeps the same invariants: public or owned data only, every call logged with cost, nothing sent without a
human click, and anything unreadable is listed, not guessed.
