# AI Studio lane

Everything for Google AI Studio lives here, apart from the Python code. Two jobs happen in AI Studio:
**(A) prompt lab** for the judge, audio, video and audit prompts, and **(B) Build mode** for the visual UI.
The trust path (search → judge → code verifier → card) stays in the Python backend; AI Studio never decides a verdict
on its own in the product.

Facts below are from AI Studio docs read 2026-11-21 (RESEARCH.md). Things marked *(check in UI)* were not documented,
so confirm them on screen.

## What's here
| Path | What |
|---|---|
| `prompts/judge_v0.md` + `prompts/judge.schema.json` | The claim-judge prompt and its output schema (start here) |
| `pack.py` → `packs/<example>/` | Per example: `crop_current.png`, `crop_superseded.png`, `words.txt`, `claim.txt` (regenerate: `python studio/pack.py`) |
| `fixtures/cards.sample.json` + `fixtures/crops/` | 3 **real** cards from Hydrogen in CONTRACT.md shape, for building the UI before the backend exists |
| `make_fixtures.py` | Rebuilds the fixtures (replays the cache by default: no cost) |

## Baseline already measured (2026-11-21, judge_v0, Hydrogen, default thinking)
| Example | Expected | Got | Conf | Cost | Latency | Boxes on the right words? |
|---|---|---|---|---|---|---|
| ex1 "All WCs in 105A are WC-2" | conflict | conflict | 0.98 | $0.015 | 10.4 s | yes, 4/4 |
| ex2 "Add 1 changed two 105A WCs to WC-3" | consistent | consistent | 0.98 | $0.024 | 16.6 s | yes, 3/3 |
| ex3 "109A has a WC-3" (unlabelled) | (code diff: WC-1) | conflict (WC-1) | 0.99 | $0.015 | 8.6 s | yes, 2/2 |
n = 3 dev examples. Too small to claim accuracy; it shows the format, grounding and cost work. **Latency is the gap**
(goal 6 s per clip, 15 s typed): try thinking LOW first.

## A. Prompt lab: the 20-minute loop
1. **Open** aistudio.google.com → new prompt. **Run settings** (tune icon): model `Gemini 4 Hydrogen`; set the thinking
   level (Hydrogen/Neon: low/medium/high/xhigh, default high; Carbon/Argon: low/medium/high, default medium).
2. **System instructions** (in Run settings): paste the top half of `prompts/judge_v0.md` (everything above `---`).
3. **Structured output** (toggle in Run settings): paste `prompts/judge.schema.json` if a schema editor appears
   *(check in UI)*. The UI doesn't validate the schema, and `minLength`/`maxLength`/`uniqueItems` are ignored.
4. **Attach** for one example: `crop_current.png` then `crop_superseded.png` (PNG ≤ 20 MB inline). Type
   "IMAGE 1 = CURRENT sheet, IMAGE 2 = SUPERSEDED sheet", then paste the bottom half of the prompt with `{{claim}}`
   from `claim.txt` and `{{words}}` from `words.txt`.
5. **Run.** Check: verdict right? `quoted_words` copied exactly from words.txt? Do the boxes cover them? (No box overlay
   in the UI: paste the result to Claude Code and ask for an overlay, or run `make_fixtures.py`.)
6. **Iterate** on wording until all dev examples pass. Then try **Low thinking** for latency and **Carbon** as the
   second judge on the same inputs.
7. **Save** (Save button) and **Get code** (Python): copy the final prompt text back into `prompts/judge_v1.md` (keep v0)
   and the schema file. Commit, and note the prompt's sha256 in the PR. Claude Code wires it into `llm.py`.

**Rules:** dev examples only, never held-out claims (T12). Never paste the API key into a prompt or Build app code.

### Other prompts to develop here (same loop)
| Ticket | Prompt | Inputs to attach |
|---|---|---|
| T9 audio → claims | "Extract atomic, checkable claims with speaker + time" | a 10–20 s wav/webm you record (≤ 20 MB inline) |
| T19 markup photo | "Which sheet is this, what is marked, where" | phone photo of a printed sheet with pen marks |
| T21 site video | "List what's built vs the current sheet crop; no counts > 3" | short mp4 + the sheet crop |
| T17 whole-set audit | "Find spec-vs-sheet conflicts; cite sheet + words" | needs ~680K tokens: run from code, not the UI |

Note: there is **no Gemini 4 Live API model** (Live lists only 3.8/3.1/2.5). For the Gemini-4-only rule, live clips
(T18) = short recorded chunks sent as normal calls.

## B. Build mode: the visual UI
Build mode makes a **React frontend + Node server** app, keeps `GEMINI_API_KEY` as a server-side secret, exports a ZIP,
**syncs two-way with GitHub**, and deploys to **Cloud Run** (two free deploys for new builders).

**Keep it apart:** sync the Build app to its **own repo**, `RahulModugula/gemini-hackathon-ui`, not this one. The UI
reads cards from our backend API (T10) and, until then, from `fixtures/cards.sample.json`.

### Paste this into Build mode
```
Build a single-page React app called "Drawing Claim Checker" for construction superintendents.
Data: load cards from /cards.sample.json (I will upload it and the crops/ images). Card shape:
{id, claim, verdict: conflict|consistent|unclear, confidence, reason, changed_by_addendum,
 evidence:[{sheet, revision, label, crop, box_2d_on_crop:[ymin,xmin,ymax,xmax] in 0-1000}],
 checks:[{by, verdict, note}], cost_usd, latency_s}.
Layout: left = claims ledger (verdict chip red/green/grey, confidence bar, claim text, "Changed by Addendum" badge);
right = selected card: crop image with each evidence box drawn as a coloured rectangle from box_2d_on_crop
(scale 0-1000 to the displayed image size), label beside each box; below, the checks as a list of ticks/crosses;
"current vs superseded" toggle when a card has both crops.
Top bar: a single input "Say or type a claim" with a mic button (disabled for now) and a file drop zone (disabled).
Style: clean, high-contrast, blueprint-inspired (navy/white, monospace for sheet numbers), mobile friendly.
No backend calls yet; no API key in client code.
Later: replace the fixture load with GET {API_BASE}/api/cards and POST {API_BASE}/api/check {claim}.
```
Then, as a second step in Build mode, ask for the **3D view** (T23, Level A):
```
Add a "3D" tab using three.js (pinned version) + OrbitControls: a floor plane textured with the plan image I upload
(a201_current.png, 3024x2160 pt). Place one pin per card evidence at (bbox x,y in pt → plane coordinates), coloured
by verdict, with a hover label of the claim; clicking a pin selects the card in the ledger.
```
(Evidence `bbox` in pt is in each card's evidence; `crop_rect_pt` gives the crop's position on the sheet.)

## Handoff back to code
- Prompt changes → `prompts/*.md` + schema via PR; Claude Code adds a replay-cached test and scores the dev set.
- UI → the `-ui` repo via Build's GitHub sync; it talks to the backend only through `/api/*` (CONTRACT.md card shape).
