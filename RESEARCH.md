# Research log

All read 2026-11-21 by research subagents; primary sources unless marked *(snippet)* (search result only, page not opened).
Treat anything not marked VERIFIED as a lead, not a fact. Re-check prices after Dec 1 (Carbon/Argon double).

## 1. Gemini 4 models: what is safe to say

| Claim | Status | Figure | Source |
|---|---|---|---|
| IDs `gemini-4-hydrogen/neon/carbon/argon` | VERIFIED (docs) | no -preview/-latest aliases | aistudio.google.com/docs/models |
| Free tier | VERIFIED | **Hydrogen only**; Neon/Carbon/Argon need billing | aistudio.google.com/docs/pricing |
| Price $/1M in/out | VERIFIED | Hydrogen 1.50/7.50 · Neon 3/15 · Carbon & Argon 2/10 until Nov 30, **4/20 from Dec 1** | same |
| Flat pricing to 1M, cache | VERIFIED | no >200K tier; implicit cache ≈ 95% below input (our arithmetic) | same |
| Native video + audio input | VERIFIED | mp4/mov/webm…; up to 3 h at low res; 1 fps, ~100 tok/s low res; audio 32 tok/s | aistudio.google.com/docs/video-understanding |
| LVBench | VERIFIED | Argon 91.7%, Neon 89.0% (Opus 5.5 83.7%) | deepmind.google model cards (Argon Oct 2026, Neon 18 Nov 2026) |
| GraphWalks 256K–1M | VERIFIED | Carbon 96.2%, Neon 95.2%, Argon 84.2% (Opus 5.5 66.8%, GPT-6.2 Sol 80.1%) | Carbon / Neon model cards |
| Long decode | **Say carefully** | default 65,536 out; above that = client-side continuation (`automatic_continuation`) inside a **shared** 1M window. NOT "1M output in one request" | aistudio.google.com/docs/long-decode |
| Managed Agents | VERIFIED | Ubuntu sandbox; `environment` id separate from interaction id (`previous_interaction_id`); egress proxy injects credentials; allowlist or `network: "disabled"` | aistudio.google.com/docs/agent-environment |
| Tool Retrieval (`defer_loading`) | PARTLY | preview, Interactions API, launched 19 Nov | blog.google, gemini-api-tool-retrieval |
| Genie 4 | PARTLY | `genie-4`, 720p/24fps, 48 kHz, ~$0.10/s, paid key; "GA" not stated | blog.google 19 Nov; aistudio.google.com/docs/genie |
| Vending-Bench 2 | VERIFIED (undated) | Neon #1 $16,843 | andonlabs.com/evals/vending-bench-2 |
| AutomationBench | PARTLY | Neon 56.3% (top of 3 on Google's card); AA version Neon 79.3% | Neon card; artificialanalysis.ai |
| Hallucination 4–14.4% | **Do not use** | Argon 4% verified; Carbon 4% vs 11% conflict; 14.4% not found | artificialanalysis.ai/evaluations/omniscience |
| "SWE-sweep 39% in Antigravity" | **Do not use** | 39.0% is "provider harness"; no Antigravity link | Neon card |
| $5,000 Managed Agents track | **NOT FOUND** | no event, rules or criteria found | ask the organiser for the source |
| Claude Opus 5.5 limits | VERIFIED | 128K sync output (300K batch beta); text + image input only | platform.claude.com models overview |
| GPT-6.2 Astra | VERIFIED | text + image only; >272K input doubles input price | developers.openai.com |

## 2. google-genai 3.6.1 SDK facts (source at the v3.6.1 tag)
- `GOOGLE_API_KEY` beats `GEMINI_API_KEY` silently → always pass `api_key=`.
- 3.0.0 (2026-11-06): default API v1; **Live API and caches need `api_version="v1beta"`**; default HTTP client httpx2.
- JSON output: `response_mime_type="application/json"`, `response_json_schema=Model.model_json_schema()`.
- `types.Part.from_bytes(data=..., mime_type=...)`; inline request limit 20 MB.
- Boxes: `[ymin, xmin, ymax, xmax]` normalised 0–1000.
- Usage: `prompt_token_count`, `candidates_token_count`, `thoughts_token_count` (billed), `cached_content_token_count`.

## 3. Prior art (what the demo can credibly claim)
- No vendor publishes sheet-level accuracy for drawing-vs-spec checking (Procore Drawings & Specs agent, Trimble
  Document Crunch, Bluebeam Max/Firmus, Trunk Tools Cortex, InspectMind). **A stated eval with n and a CI is itself the gap.**
- DrawingVQA (arXiv 2607.15418): Gemini 2.5 Flash 66.3% with image vs **9.8% text-only**; humans 94.9%.
  → the judge must see the crop; the text layer is for grounding and search, not for judging alone.
- AECV-Bench (arXiv 2601.04819): OCR QA up to 0.95, **symbol counting 0.40–0.55**. → no count claims from a
  model alone; counts go to code or to "could not check".
- RFI cost anchor: ~$1,080 per RFI, ~800 RFIs per project (Navigant Construction Forum, *secondary sources only*).
  Say "reported", never as our result.

## 4. Eval method (n = 20 held-out)
- Wilson 95% CI on every proportion: 15/20 → [0.53, 0.89]; 18/20 → [0.70, 0.97]; 4/5 conflict precision → [0.38, 0.96].
- Report: accuracy, coverage (share not unclear), selective accuracy, **conflict precision + false-alarm count**,
  conflict recall, Hydrogen–Carbon raw agreement + kappa + marginals, single-judge baseline (Hydrogen alone),
  two-run flip count, Brier score on 2–3 confidence tiers, evidence precision (hand-labelled).
- Same-family judges share errors (Goel et al. ICML 2025, arXiv 2502.04313; Panickssery et al. NeurIPS 2024):
  agreement is a diagnostic, not proof. Hence the deterministic verifier (T8).
- Hygiene: freeze labels + prompt hash before held-out; tune on dev only; touch held-out once per prompt version.
- Refs: Miller, "Adding Error Bars to Evals" arXiv 2411.00640; Bowyer et al. arXiv 2503.01747; ALCE arXiv 2305.14627.

## 5. Our own measurements on the bid set (2026-11-21, `ingest.py` + `analyze_sheets.py`)
- 76 drawing sheets = 54 base + 17 (Add 1) + 5 (Add 2); **all 76 have a vector text layer**; 92,484 words; 2,660 tiles at 150 dpi.
- Base by discipline: Arch 14, Elec 8 (+3 demo), Civil 6, Mech 5 (+2 demo), Plumb 4 (+2 demo), General 3, Struct 3,
  Landscape 2, Arch demo 1, ME 1.
- Supersession preview: 20 distinct sheet numbers superseded; **A1100 and ED200 revised in both addenda**.
  Add 1 cover says 16 revised sheets but 17 are bound in (T5 to resolve).
- Civil/landscape sheets are `/Rotate 270`: word boxes must be rotated into render space (bug found and fixed, test added).
- Context sizing (chars/4 estimate, **not** a token count): current sheets ≈ 94K tokens; project manual ≈ 457K;
  addenda ≈ 52K; A201 ≈ 79K; **whole current bid set ≈ 680K tokens → fits one 1M context** (T17).
- Ingest time: 76 sheets in about 3.5 min before the display-list change; 3 sheets in 2.9 s after it (about 10× faster).
