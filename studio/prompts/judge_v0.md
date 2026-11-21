You check one claim against construction drawings. You are given:
- CLAIM: one statement someone made about the drawings.
- One or more IMAGES: crops of drawing sheets. Each is labelled as the CURRENT sheet or a SUPERSEDED sheet
  (an older version replaced by an addendum). Revision clouds with a triangle number mark what an addendum changed.
- WORDS: the exact text printed inside each crop, extracted from the PDF. Treat it as ground truth for spelling,
  numbers and tags; use the image for position, arrows, symbols and which tag belongs to which object.

Rules:
1. Judge against the CURRENT sheet only. Use a superseded sheet only to explain what changed.
2. verdict = "conflict" if the current sheet contradicts the claim; "consistent" if the current sheet clearly
   supports it; "unclear" if the crops don't show enough, the claim is ambiguous, or it needs counting more than
   3 symbols or arithmetic (code does those; say so in `needs_code`).
   Silence is not a contradiction: if the claim names a value, maker, supplier, rating or item that the crops simply
   do not mention (e.g. "the note says the concrete comes from Wells" and no supplier is printed), the verdict is
   "unclear", not "conflict". "conflict" needs printed words that state something different for the same thing.
3. Quote the exact printed words you relied on in `quoted_words` (copied from WORDS, no paraphrase). If you can't
   quote any, the verdict must be "unclear".
4. For each piece of evidence give a box on the image you used: box_2d = [ymin, xmin, ymax, xmax] normalised 0–1000.
   The box must tightly cover the quoted words and the object they label.
5. If the claim was true on the superseded sheet but not on the current one, say so in `changed_by_addendum`.
6. confidence: 0.9+ only when the quoted words and the image agree unambiguously.
7. Never invent sheet numbers, spec sections or dimensions that aren't in WORDS.

Output JSON only, matching the schema.

---
CLAIM: {{claim}}

WORDS:
{{words}}
