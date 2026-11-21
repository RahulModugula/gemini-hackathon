# Demo and product story

## The problem (why anyone pays)
On a commercial job, the superintendent, the PM and the trades argue from memory about what the drawings say, and
the drawings keep changing. This bid set alone has **54 sheets, then 22 revised sheets in two addenda within
three weeks**. Two sheets (A1100, ED200) changed twice. Whoever builds from the superseded sheet pays for it.
RFIs are the industry's way of asking "what do the drawings actually say?" A widely cited industry study reports
about **$1,080 per RFI** and about 800 per project (Navigant Construction Forum, reported; not our measurement), and many
of them were already answered in an addendum nobody re-read.

**User:** superintendent / project engineer on site, phone in hand, in a meeting or a walk.
**Job to be done:** "Tell me in seconds whether what I just heard matches the *current* drawings, show me where,
and stop me filing an RFI that's already answered."

## Why only now, and why Gemini 4
| Moment in the product | Gemini 4 capability | Why it matters |
|---|---|---|
| Hears the site meeting and pulls out claims | native audio (Hydrogen) | no transcription step, speakers kept |
| Watches a phone walk of the room | native video (Argon; LVBench 91.7%) | checks installed work, not just talk |
| Reads the sheet like an engineer and boxes the evidence | vision + spatial grounding (Argon, Carbon) | the answer comes with *where* |
| Second opinion that isn't a rubber stamp | two different frontier models (Argon + Carbon) in parallel | disagreement → "unclear", never a guess |
| Knows the whole bid set at once | ~680K tokens in one context (Carbon GraphWalks 96.2%) | spec vs sheet conflicts across 692 pages |

**The trust layer is ours, not the model's.** Every card is sealed by code: the evidence boxes must land on the
PDF's real vector text, the sheet must be the current revision, the dimensions are compared in code, and an
addendum word-diff shows exactly what changed. "Consistent isn't correct."

## The 3-minute demo (open on the result)
1. **0:00, the punchline first.** The screen shows one card: *CONFLICT: "All the water closets in Toilet 105A are WC-2."*
   Side by side: the bid sheet says WC-2; Addendum 1's cloud changed two to WC-3. The evidence seal shows boxes on PDF words ✓,
   current revision ✓, Argon ✓ and Carbon ✓ agree, addendum diff WC-2 → WC-3.
   Say: *"That claim was true three weeks ago. Building from it now is a change order."*
2. **0:30, live voice.** Press the mic and say: *"The grab bar in toilet 109A is at thirty-three inches, and the knox box
   goes six feet up."* The agent trace streams search → crop → Argon/Carbon → grounding → sealed. Two cards appear in
   about 6–8 s.
3. **1:15, the phone.** On the iPhone app, record a 10 s clip of the room or the printed sheet, and the cards come back with
   timestamps. *(If the venue Wi-Fi is bad: replay mode, the same cards from the recorded cache, with zero network.)*
4. **1:45, the RFI that didn't need sending.** Ask *"do the playground gates need maglocks?"* →
   **Blocked: already answered in Addendum 1, p.2, Q6: "Provide padlock latches and standard hinges."**
5. **2:15, where in the building.** The 3D tab shows the plan as a floor, with conflict pins at the rooms.
6. **2:35, honest numbers.** Held-out accuracy with a 95% confidence interval, n, cost per card and median latency.
   No vendor in this space publishes sheet-level accuracy; we do.
7. **2:50, close.** *"Every claim on site, checked against the current drawings, with the evidence, in seconds."*

## Numbers we may say (fill from `build/metrics.json` after the held-out run)
- Held-out: __/20 correct, 95% CI [__, __]; conflict precision __; n = 20 claims written blind by separate agents and label-audited.
- Median latency __ s per typed claim; __ s per 6 s audio clip; $__ per card.
- Grounding: __% of evidence boxes land on the PDF words they cite.
- Never say: vendor accuracy numbers, "1M output in one request", or any number without n and a date.

## Judge questions to be ready for
- *"What if both models are wrong the same way?"* The code checks (grounding, revision, dimensions, diff) don't share their failure modes; we report agreement and accuracy separately.
- *"Raster scans?"* This set is fully vector (76/76 sheets have a text layer). For raster sheets, the grounding check would degrade to vision-only and those cards are marked unclear. Listed, not guessed.
- *"Cost at scale?"* About $0.03 per card with Argon + Carbon at intro prices (doubling Dec 1); Hydrogen-only mode is about 3× cheaper.
