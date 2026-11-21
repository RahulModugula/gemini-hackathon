"""ui/assets/stage.js frame(): the camera window stays inside the world bounds, including the crop-only fallback where
the world does not start at (0, 0). Runs the real module under node; skipped when node is not installed."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

STAGE = Path(__file__).resolve().parent.parent / "ui" / "assets" / "stage.js"
SCRIPT = """
const {frame} = await import(process.argv[1]);
const out = [];
// crop-only world: a 520 pt crop at (1148, 630); focus near its left/top edge
out.push(frame([1150, 640, 1170, 650], 16/9, [1148, 630, 1668, 1150], 40, 340));
// full sheet world at origin; focus near the right edge
out.push(frame([3000, 100, 3020, 120], 16/9, [0, 0, 3024, 2160], 40, 340));
// focus larger than the world: clamp to it
out.push(frame([1100, 600, 1700, 1200], 16/9, [1148, 630, 1668, 1150], 40, 340));
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_frame_stays_inside_bounds():
    r = subprocess.run(["node", "--input-type=module", "-e", SCRIPT, STAGE.as_uri()], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    crop, sheet, big = json.loads(r.stdout)
    for (x0, y0, x1, y1), (bx0, by0, bx1, by1) in ((crop, (1148, 630, 1668, 1150)), (sheet, (0, 0, 3024, 2160)),
                                                     (big, (1148, 630, 1668, 1150))):
        assert bx0 - 1e-6 <= x0 < x1 <= bx1 + 1e-6 and by0 - 1e-6 <= y0 < y1 <= by1 + 1e-6
    assert crop[0] == 1148 and crop[1] == 630          # pinned to the crop's corner, not to (0, 0)
    assert abs((big[2] - big[0]) - 520) < 1e-6         # never wider than the crop
