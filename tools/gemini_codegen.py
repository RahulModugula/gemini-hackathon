"""Ask Gemini 4 Argon to write ui/index.html (one-page Drawing Claim Checker). Usage: python tools/gemini_codegen.py [extra defect notes...]"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import llm  # noqa: E402

SPEC = """Write ONE self-contained file ui/index.html: the "Drawing Claim Checker" for construction superintendents (a claim spoken or typed on site is checked against the drawings). Vanilla JS + CSS only, no React, no build step. Output ONLY a single ```html fenced block, no commentary. Be compact: no comments in the code, short names, because output is HARD-capped at 8192 tokens and a truncated file is useless: aim for under 5500 tokens total (terse CSS written as compact one-line rules, short JS helpers, no redundant styling, no resets beyond box-sizing). Always close the </html> and the ```.

Libraries: only three@0.160.0 via <script type="importmap"> from cdn.jsdelivr.net/npm: "three": "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js", "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/". Use OrbitControls from three/addons/controls/OrbitControls.js in a <script type="module">.

Data: fetch('./cards.json') -> array of cards (shape below; sample card follows). Crop image paths are relative to the page (e.g. crops/x.png). Sheet image for 3D: assets/p101_current.png (3024x2160 pt, 1 px per pt).

Layout and behaviour:
- Top bar: title "Drawing Claim Checker", a text input with placeholder "Say or type a claim", and a DISABLED mic button (title "Voice coming soon").
- Two tabs: "Cards" and "3D". The URL hash drives the tab: #3d shows the 3D tab, otherwise Cards. An optional hash like #3d or #card=<id> may select a card; clicking tabs updates the hash. Hash changes are handled via hashchange and on load.
- Cards tab, wide screens: two columns. LEFT claims ledger: one row per card with a verdict chip (conflict = red, consistent = green, anything else = grey, text label in the chip), a confidence bar (width = confidence*100%, with the percentage), the claim text, and a "Changed by Addendum" badge when changed_by_addendum is true. Clicking a row selects it (highlight). Select the first card on load.
- RIGHT selected card: big verdict chip + claim; a crop viewer; a "Current" / "Superseded" toggle shown ONLY when evidence has both revision !== "dwg" crops (current) and revision === "dwg" crops (superseded); the viewer shows the evidence whose revision matches the toggle (current = revision !== "dwg", superseded = revision === "dwg"), grouped by the evidence's crop image (all evidence in one view share the same crop file). The crop <img> is displayed responsively (max-width 100%, natural aspect ratio). Every evidence box is drawn as an absolutely positioned div inside a wrapper that is position:relative and exactly the size of the image, using PERCENTAGES: box_2d_on_crop is [ymin,xmin,ymax,xmax] on a 0-1000 scale, so top=ymin/10 %, left=xmin/10 %, height=(ymax-ymin)/10 %, width=(xmax-xmin)/10 %. That keeps boxes aligned on resize. Box border colour follows the card verdict (red/green/grey), 2px, semi-transparent fill. Each box has a small numbered label tag (1,2,3..) anchored at its top-left, and below the image a legend list "1  label text  (sheet)" with the sheet in monospace. Then: the checks list (each check: ✓ if check.verdict equals the card verdict, ✗ if it is "conflict"/"consistent" and differs from the card verdict, otherwise an "i" mark for info; show check.by in monospace and note), the reason paragraph, and a footer line "cost $X.XXXX · latency Ys" from cost_usd and latency_s.
- Narrow screens (<=700px): single column, ledger first then the card below it; the tabs and top bar wrap; the input takes the full width; no horizontal page scroll; works at 390px wide.
- 3D tab: three.js scene filling the available area (min height 60vh, resize with the container). Floor = PlaneGeometry textured with assets/p101_current.png, sized 3024 x 2160 world units multiplied by 0.1 scale (world 302.4 x 216), lying flat (rotate -PI/2), sRGBColorSpace. For every evidence item of every card whose sheet is "add1-p018" (current sheet only; skip other sheets): one pin = thin CylinderGeometry stem (height 12) + SphereGeometry head (radius 3) at x = (bbox[0]+bbox[2])/2*0.1 - 151.2, z = (bbox[1]+bbox[3])/2*0.1 - 108 (so the sheet's top-left maps to world (-151.2,-108)); coloured by the card's verdict (red 0xe5484d / green 0x30a46c / grey 0x8b8d98). Perspective camera tilted looking down at the plane from ~z 160, y 180 and OrbitControls with damping. Raycaster hover over pins shows a floating HTML label (card claim + evidence label) near the cursor; click on a pin selects that card and switches to the Cards tab (via hash). Render loop with requestAnimationFrame; build the scene lazily the first time the 3D tab is shown and call renderer.setSize / camera.aspect update when shown and on window resize (the canvas container is display:none while hidden so measure after showing). Preserve the drawing's aspect.
- Style: blueprint: deep navy background (#0b1f3a), panels slightly lighter navy (#12305a), white/very light text, cyan-ish accent lines (#7fb8ff), 1px light borders, monospace (ui-monospace, Menlo, monospace) for sheet numbers/ids, system sans for text, HIGH contrast (WCAG AA), visible focus outlines, no gradients needed. Chips: red #e5484d, green #30a46c, grey #8b8d98 with dark/white text of sufficient contrast.
- Handle fetch failure by showing an error message in the ledger area. Escape card text when inserting into HTML (use textContent).

Card JSON shape (array of): {id, claim, speaker, timestamp, verdict: "conflict"|"consistent"|"unclear", confidence: 0-1, reason, evidence: [{sheet, revision: "add1"|"dwg"|..., bbox:[x0,y0,x1,y1] in pt on the sheet, box_2d_on_crop:[ymin,xmin,ymax,xmax] 0-1000, crop: "crops/...png", crop_rect_pt, label}], spec_refs, checks: [{by, verdict, confidence?, note}], changed_by_addendum: bool, rfi_draft, cost_usd, latency_s}
"""


def main():
    args = [a for a in sys.argv[1:] if a != "--force"]
    if (ROOT / "ui" / "index.html").exists() and "--force" not in sys.argv[1:]:
        raise SystemExit("ui/index.html has human edits (API wiring); refusing to overwrite. Re-run with --force.")
    notes = " ".join(args)
    cards = json.loads((ROOT / "ui" / "cards.json").read_text())
    prompt = SPEC + "\nSample card:\n" + json.dumps(cards[0], indent=1)
    if notes:
        prompt += "\n\nDEFECTS FROM THE PREVIOUS GENERATION TO FIX (keep everything else): " + notes
    res = llm.call("gemini-4-argon", [prompt], thinking="MEDIUM", purpose="codegen:ui")
    (ROOT / "build").mkdir(exist_ok=True)
    (ROOT / "build" / "ui_raw.txt").write_text(res.text)
    m = re.findall(r"```html\s*\n(.*?)```", res.text, re.S)
    if len(m) != 1:
        raise SystemExit(f"expected exactly one ```html block, got {len(m)}; text[:300]={res.text[:300]!r}")
    out = ROOT / "ui" / "index.html"
    out.write_text(m[0])
    print(f"wrote {out} ({len(m[0])} chars) cost ${res.cost_usd:.4f} latency {res.latency_s}s usage {res.usage}")


if __name__ == "__main__":
    main()
