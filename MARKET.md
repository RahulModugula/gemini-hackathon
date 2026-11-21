# Construction problems → Gemini 4 → product

Research read 2026-11-21 by two research agents. Most hard numbers are 2013–2021 and several come from secondary
citations (marked *sec*). We use them as context, never as our results. Full source list at the bottom.

## 1. The problems, ranked by evidence × cost

| # | Problem | Best evidence | Strength |
|---|---|---|---|
| 1 | **Hunting for current project information** | ~5.5 h/person/week; ~14 h/week of "non-optimal" work (FMI/PlanGrid *Construction Disconnected*, 2018, n=599) | survey, *sec* |
| 2 | **Rework from miscommunication and poor project data** | 48% of US rework; $31.3B in 2018 (same study). Direct rework commonly ~5% of cost (Hwang et al. 2009, 359 CII projects; abstract) | survey + peer-reviewed |
| 3 | **RFI volume and waste** | ~800 RFIs/project, ~$1,080 each, median 9.7 days, ~22% never answered, **~13% already answered in the documents** (Navigant 2013, ~1,362 projects) | study, *sec* |
| 4 | **Design errors → change orders** | 36–42% of change-order/variation cost in academic samples | small samples, *sec* |
| 5 | **Cross-trade conflict resolution** | ~5 h/person/week (FMI/PlanGrid 2018) | survey, *sec* |
| 6 | **Building from a superseded sheet** | no independent frequency study; vendor anecdotes (£15–25k per wrong-revision install); this bid set itself had **22 revised sheets in 3 weeks**, two revised twice (our measurement) | anecdote + our data |
| 7 | **Undocumented verbal directives** | qualitative (Stanford CIFE WP109); disputes become credibility contests | qualitative |
| 8 | **Field verification of installed work** (heights, tags, ADA) | counter/surface heights 21% of alleged CA accessibility violations, Jul–Dec 2021 (complaint data, not an inspection error rate) | indirect |

**Contract fact that makes this the GC's problem:** AIA A201 §3.2.2 requires the contractor to promptly report errors,
inconsistencies or omissions it discovers, as an RFI, and to bear the avoidable costs if it doesn't (*sec*).

## 2. Who buys, and what blocks them
- **Wedge:** project teams (PM, project engineer, superintendent) at mid-size general contractors on commercial jobs,
  at the step where they check a claim before issuing an RFI or a change-order response.
- **Top adoption blockers:** accuracy (57%) and data security (54%) (Dodge/CMiC, Sept–Oct 2025, n=235, via Construction
  Dive); 17% "don't trust AI results"; only 26% rate their own data quality high.
- **Pricing norms:** Procore by annual construction volume (no per-seat fee); Bluebeam per seat, ~$260–440/yr
  (*sec*). No public per-project price for an AI check: an open pricing lane.
- **Category is live:** Bluebeam bought Firmus (drawing review, Sept 2025); Trimble buying Document Crunch (contracts,
  not drawings, Apr 2026); LightTable $22M Series A (design errors, May 2026); Procore's RFI-drafting agent (Oct 2025).
  **Nobody publishes sheet-level accuracy.**

## 3. Map: problem → Gemini 4 capability → feature → ticket

| Problem | Gemini 4 capability (verified) | Feature | Trust check (code) | Ticket |
|---|---|---|---|---|
| #1 hunting info | vision + grounding (Argon, Carbon) | ask in words → the exact sheet crop with the answer boxed | boxes must land on PDF words | T7/T8 ✓ built |
| #3 RFIs already answered | Hydrogen confirm + retrieval | "Blocked: already answered, Add 1 p.2 Q6" with the exact quote | quote must be an exact substring of the addendum | T11 ✓ built |
| #6 superseded sheet | vision over two revisions | side-by-side superseded vs current with changed words highlighted | word diff between revisions; currency check | T5/T8 ✓, UI v2 in progress |
| #7 verbal directives | **native audio** (Hydrogen) | meeting audio → claims with speaker + timestamp → cards; a written record of what was said and whether it matches | timestamps validated vs clip length | T9 ✓ built |
| #8 field verification | **native video** (Argon, LVBench 91.7%) | phone clip of the room → visual + spoken claims → cards | counts > 3 go to code or unclear | T21 ✓ backend, iOS app built |
| #2/#4 spec vs drawing | **1M context** (Carbon GraphWalks 96.2%) | whole-set audit: ~680K tokens of current sheets + 692-page manual in one call → ranked conflicts | every finding re-grounded; "could not check" list | T17 next |
| #5 cross-trade | long context + vision | arch vs MEP sheet pairs for the same room (P101 vs A201) | both crops grounded | T17 extension |
| all | **Managed Agents** | standing agent: each new addendum re-checks affected cards | verifier is our code, run in the sandbox | T22 |

## 4. Must-have product properties (from the buyer research)
1. Every verdict cites **sheet number, revision, issue date and addendum**, and shows the superseding sheet. *(built)*
2. **Never answers "new" when a prior answer exists:** shows the RFI/Q&A reference and quote. *(built)*
3. Explicit **"unclear / not found"** and confidence, never a silent guess. *(built: disagreement or failed grounding → unclear)*
4. **No training on customer data**; per-project access control and deletion. *(public data only now; required before any real pilot)*
5. Works in the field and **exports to the RFI log**: phone capture, offline replay, and an RFI draft with the evidence attached. *(capture + draft built; offline sync later)*

## 5. Honest gaps
- No independent study measures how often work is built from superseded sheets; we show our own set's churn instead.
- Superintendent-specific time studies and inspection error rates: not found.
- Rework baselines conflict (0.4–0.8% in one contractor's data vs 2.4–12.4% CII): we don't quote a single number.

## Sources (read 2026-11-21)
FMI/PlanGrid 2018 press release: prnewswire.com/news-releases/…300689826.html · Navigant 2013 RFI study (via
procore.com/jobsite/lower-the-cost-of-your-rfis, smartsheet.com, esub.com) · Hwang et al. 2009 JCEM 135(3) (trid.trb.org/view/884832) ·
Dodge/CMiC 2025 (constructiondive.com/news/builders-ai-transform-businesses-survey/807555/) · Bluebeam–Firmus (enr.com/articles/61413) ·
Trimble–Document Crunch (constructiondive.com/news/trimble-acquire-document-crunch-contech/816630/) · Procore Groundbreak 2025
(businesswire.com/news/home/20251015796723/en/) · CA CCDA top-ten violations 2021 (dgs.ca.gov) · Stanford CIFE WP109 · Procore pricing (procore.com/pricing).
