"""Video -> still frame evidence. `extract_frame` pulls the single frame at t_s with ffmpeg into build/frames/<sha>.jpg;
`crop_region` cuts the boxed region (for the judge); `store_video` keeps the upload under build/videos/<sha>.<ext>."""
import hashlib
import subprocess
from pathlib import Path

from PIL import Image

MAX_PX = 1280
_EXT = {"video/mp4": ".mp4", "video/quicktime": ".mov"}


def _scale(max_px):
    """ffmpeg filter: shrink the long edge to max_px, never enlarge; -2 keeps the other edge even and the aspect."""
    return f"scale=w='if(gte(iw,ih),min({max_px},iw),-2)':h='if(gte(iw,ih),-2,min({max_px},ih))'"


def sha_of(data):
    return hashlib.sha1(data).hexdigest()


def store_video(data, mime, out_dir):
    """Write the upload to <out_dir>/<sha1><.mp4|.mov> (idempotent). Returns (path, sha)."""
    sha = sha_of(data)
    p = Path(out_dir) / f"{sha}{_EXT.get(mime, '.mp4')}"
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        tmp = p.with_suffix(p.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(p)
    return p, sha


def extract_frame(video_path, t_s, out_dir, video_sha=None, max_px=MAX_PX):
    """-> {"path": Path, "width": int, "height": int, "t_s": float}: the frame at t_s as a JPEG under out_dir, named by
    sha1(video sha | t in ms). -ss goes before -i (fast keyframe seek, then an exact decode up to t_s). Raises
    ValueError for a negative/non-finite t_s and RuntimeError when ffmpeg yields no frame (t_s past the end)."""
    t_s = float(t_s)
    if not (0 <= t_s < float("inf")):
        raise ValueError(f"bad frame time {t_s!r}")
    video_path = Path(video_path)
    vsha = video_sha or sha_of(video_path.read_bytes())
    out = Path(out_dir) / (hashlib.sha1(f"{vsha}|{round(t_s * 1000)}|{max_px}".encode()).hexdigest()[:20] + ".jpg")
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        tmp = out.with_name(out.stem + ".part.jpg")
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t_s:.3f}", "-i", str(video_path), "-frames:v", "1",
                            "-vf", _scale(max_px), "-q:v", "3", str(tmp)], capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg produced no frame at {t_s}s: {r.stderr.strip()[-200:]}")
        tmp.replace(out)
    with Image.open(out) as im:
        w, h = im.size
    return {"path": out, "width": w, "height": h, "t_s": t_s}


def crop_region(frame_path, box_2d, out_path, pad=0.3, min_px=256):
    """Crop the [ymin, xmin, ymax, xmax] (0-1000) region of a frame, grown by `pad` of its size on each side (context
    for a tape reading) and to at least min_px, into out_path (JPEG). Returns the path."""
    with Image.open(frame_path) as im:
        W, H = im.size
        ymin, xmin, ymax, xmax = box_2d
        x0, x1, y0, y1 = xmin / 1000 * W, xmax / 1000 * W, ymin / 1000 * H, ymax / 1000 * H
        w, h = max(x1 - x0, min_px) * (1 + 2 * pad), max(y1 - y0, min_px) * (1 + 2 * pad)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        box = (max(0, round(cx - w / 2)), max(0, round(cy - h / 2)), min(W, round(cx + w / 2)), min(H, round(cy + h / 2)))
        im.crop(box).convert("RGB").save(out_path, "JPEG", quality=90)
    return Path(out_path)


def fmt_t(t_s):
    """7.4 -> '0:07'."""
    s = int(max(0, t_s))
    return f"{s // 60}:{s % 60:02d}"
