# How we build

Two separate questions, often confused:
1. **What runs inside the product** → Gemini 4 only (hackathon rule). Called from our code through `llm.py` (T0).
2. **What helps us write the code** → any tool. Check the event rules once; coding assistants are normally allowed.

## Tools and who does what

| Tool | Use it for | Don't use it for |
|---|---|---|
| **Claude Code** (repo lane) | Code that must be correct and tested: `llm.py`, search, verifier, check pipeline, eval harness, API, CI, reviews, issues/PRs | Tuning Gemini prompts (needs the Gemini playground loop) |
| **Google AI Studio** (prompt lab) | Iterating prompts **on the real Gemini 4 models** with real crops/audio/video: T6 judge prompt, T9 audio extraction, T17 audit prompt, T21 video prompt. Try a JSON schema, compare Hydrogen vs Carbon on the same input, read token counts, then **Get code** and paste the prompt + schema into `prompts/` | Production code; anything with secrets |
| **Antigravity** | Fast UI scaffolding and throwaway prototypes, if its model picker has a current model. If it only offers older models, give its tickets to Claude Code | Correctness-critical tickets (T7, T8, T11, T13) |
| **Gemini 4 in the app** | Every product judgement (see the capability map below) | Arithmetic, counting, dates, supersession: code does those |

Recommended re-split: Claude Code takes T0, T3b, T7, T8, T11, T13 and the API side of T10. Antigravity (or Claude Code)
builds the T10 page. You own T6 in AI Studio, plus T12 labels, T14 backup and T16 pitch.

## The loop for any model-facing ticket (about 20 minutes per pass)
1. **AI Studio:** paste 3 dev examples (claim + crop image + crop words). Iterate the prompt and schema until the output
   is right on all 3. Note model, thinking level and tokens.
2. **Save:** `prompts/<name>.md` (prompt) + `prompts/<name>.schema.json`. Commit; record the prompt hash in the PR.
3. **Claude Code:** wire it through `llm.py`, add a replay-cached test, and run on the 10 dev claims.
4. **Score on dev only.** Held-out is touched only by T13, once per prompt version.

## Capability map: what Gemini 4 is good at → where it shows in the product
Only strengths verified in RESEARCH.md. Each row pairs a model capability with the code check that keeps it honest.

| Gemini 4 strength (verified) | Product feature | Model | Deterministic check | Demo moment |
|---|---|---|---|---|
| Image understanding + **visual grounding** (boxes 0–1000) | Evidence crop with a highlighted box on the sheet | Hydrogen → Carbon | Box must overlap the PDF words it quotes (T8) | Claim spoken → sheet crop lights up |
| **Native audio** input | Talk to the drawings: a spoken claim becomes a card | Hydrogen | Claims re-checked by the T7/T8 path | Say "the toilet grab bar is 33 inches", card appears |
| **Long context** (GraphWalks 256K–1M: Carbon 96.2%), flat price to 1M | Whole-bid-set audit: ~680K tokens in one call | Carbon | Every finding re-grounded; "could not check" list | "Audited 76 sheets + 692-page manual in one pass" |
| **Native video** (LVBench: Argon 91.7%) | Site-walk video vs current sheet | Argon | Timestamped frame + sheet crop; counts by code | Phone video of a room → mismatched door swing flagged |
| **Chart/figure reading** (Chartography: Neon 73.5%) | Read schedules and tables on sheets (door, fixture, finish schedules) | Neon | Parsed rows cross-checked against the words layer | Fixture schedule vs plan tags mismatch |
| **Managed Agents** (sandbox, environment handoff) | Background auditor → code verifier handoff | Neon (auditor) | Verifier is our Python, run in the sandbox | Live step trace: agent finds, code confirms |
| **Structured output** (JSON schema) | Every card is schema-valid JSON | all | Pydantic validation; invalid → retry or unclear | — |

**Weak spots to design around** (RESEARCH §3): symbol counting is about 0.40–0.55 for VLMs → counts from code over
the text layer. Text-only judging collapses (10% vs 66% with the image) → always send the crop.

## Multimodal intake: one input, every modality (T24)
One "drop anything" bar. Each input becomes **claims**, and every claim takes the same T7 → T8 path, so adding a
modality never adds a new trust path.

| Input | Gemini 4 does | Becomes | Ticket |
|---|---|---|---|
| Typed text | parse into atomic claims | claims | T7 |
| Voice clip / live mic | native audio → claims + speaker + time | claims | T9, T18 |
| Photo of a marked-up print | find the sheet, read the markup | claims pinned to a sheet location | T19 |
| Site photo / phone video | compare what's built with the current sheet | claims with a frame timestamp | T21 |
| Addendum or RFI response PDF | find which existing cards it changes | re-opened cards | T22 (L3) |

## 3D: the drawing set as a navigable model (T23, stretch)
**Why it's not a gimmick:** the 3D view is built *from the drawing's own vector geometry*, not generated, and every pin
is a real card at its real coordinates. It answers "where in the building are the problems?" at a glance.

**Measured feasibility (2026-11-21, A201 current = add1 p7, 62,515 vector paths):**
- The heaviest strokes (3.6 / 2.52 pt, 399 segments) are **fire-rating boundaries, not walls**: checked visually, so
  hypothesis 1 was rejected.
- Walls are **grey filled polygons** (135 paths with fill 0.55): the envelope and main walls recover cleanly. 292
  white fills are masks to drop. Some interior partitions are thin double lines and need more work.
- 29 room numbers (100–161) in the text layer → room labels and pin anchors.

**Build, in increasing ambition (stop at whichever level is solid by the freeze):**
1. **Level A (always works):** three.js scene with the current A201 rendered as a texture on the floor plane; each
   card is a pin at its pt bbox (linear pt → metres via the drawing scale); colour = verdict; click → card.
2. **Level B:** extrude the grey wall polygons to 3 m with `ExtrudeGeometry`; room labels from the text layer.
3. **Level C:** stack sheets by discipline as layers (arch / plumbing / electrical) you can toggle; superseded sheets
   shown as ghosted layers under the current one.
- Libraries: `three` + `OrbitControls` from a CDN, pinned to an exact version (e.g. `three@0.170.0`); no build step. Geometry is exported by a Python script
  (`export_3d.py` → `build/model3d.json`); the browser only renders.
- Gemini's role: none in the geometry (code is exact). Gemini supplies the cards that become pins, and (Level C+)
  classifies ambiguous filled shapes as wall / fixture / casework, verified against layer colour and fill.

## What the UI should show (judges look for these)
1. **The sheet, not a chat box.** Crop on the left with the evidence box drawn, card on the right.
2. **The verifier visibly working:** `checks[]` as ticks: grounding ✓, current revision ✓, math ✓, judges agree ✓.
3. **Superseded sheet trap:** a card that says "you're looking at A201 from the base set; Add 1 replaced it" with both crops.
4. **RFI blocked:** "already answered in Addendum 1, p.2, Q5" with the quoted answer.
5. **Honest numbers:** the T13 table with n and CIs, and the cost per card from `calls.jsonl`.
6. **Unreadable listed:** a "could not check" panel instead of guesses.
