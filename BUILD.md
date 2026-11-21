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

## What the UI should show (judges look for these)
1. **The sheet, not a chat box.** Crop on the left with the evidence box drawn, card on the right.
2. **The verifier visibly working:** `checks[]` as ticks: grounding ✓, current revision ✓, math ✓, judges agree ✓.
3. **Superseded sheet trap:** a card that says "you're looking at A201 from the base set; Add 1 replaced it" with both crops.
4. **RFI blocked:** "already answered in Addendum 1, p.2, Q5" with the quoted answer.
5. **Honest numbers:** the T13 table with n and CIs, and the cost per card from `calls.jsonl`.
6. **Unreadable listed:** a "could not check" panel instead of guesses.
