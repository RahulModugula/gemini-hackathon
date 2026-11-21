"""_frame_evidence: per-box crop files, atomic writes, and crop failure degrading to the whole frame."""
import threading

from PIL import Image

import app as appmod
import video_frames as vf


def claim(t=1.0, kind="visual"):
    return {"claim": "Tag WC-3", "kind": kind, "frame_t_s": t, "quote_or_observation": "tag"}


def setup(monkeypatch, tmp_path, box):
    frame = tmp_path / "frames" / "f1.jpg"
    frame.parent.mkdir()
    Image.new("RGB", (640, 360), (10, 200, 30)).save(frame)
    monkeypatch.setattr(appmod, "BUILD", tmp_path)
    monkeypatch.setattr(appmod.video_frames, "extract_frame", lambda *a, **k: {"path": frame, "width": 640, "height": 360, "t_s": 1.0})
    state = {"box": box}
    monkeypatch.setattr(appmod.video_claims, "locate_in_frame",
                        lambda *a: {"box_2d": state["box"], "text_read": ""})
    return frame, state


def test_distinct_boxes_on_one_frame_get_distinct_crop_files(monkeypatch, tmp_path):
    frame, state = setup(monkeypatch, tmp_path, [100, 100, 300, 300])
    _, a = appmod._frame_evidence(tmp_path / "v.mp4", "sha", claim())
    state["box"] = [500, 500, 800, 800]
    _, b = appmod._frame_evidence(tmp_path / "v.mp4", "sha", claim())
    state["box"] = [100, 100, 300, 300]
    _, a2 = appmod._frame_evidence(tmp_path / "v.mp4", "sha", claim())
    assert a[0]["path"] != b[0]["path"] and a[0]["path"].name.endswith("_crop.jpg")
    assert a[0]["path"] == a2[0]["path"]
    assert a[0]["path"].is_file() and b[0]["path"].is_file()


def test_same_box_is_deterministic_and_concurrent_writes_are_safe(monkeypatch, tmp_path):
    frame, _ = setup(monkeypatch, tmp_path, [100, 100, 300, 300])
    paths, errs = [], []

    def one():
        try:
            _, extra = appmod._frame_evidence(tmp_path / "v.mp4", "sha", claim())
            paths.append(extra[0]["path"])
            with Image.open(extra[0]["path"]) as im:
                im.load()
        except Exception as e:  # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=one) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and len(set(paths)) == 1
    assert not [p for p in frame.parent.iterdir() if ".part" in p.name]       # no temp files left behind


def test_crop_failure_degrades_to_full_frame_without_box(monkeypatch, tmp_path):
    frame, _ = setup(monkeypatch, tmp_path, [100, 100, 300, 300])
    monkeypatch.setattr(appmod.video_frames, "crop_region", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    ve, extra = appmod._frame_evidence(tmp_path / "v.mp4", "sha", claim())
    assert ve["box_2d"] is None and ve["frame"] == "/frames/f1.jpg"
    assert extra[0]["path"] == frame


def test_extract_frame_temp_file_is_unique_per_call(monkeypatch, tmp_path):
    tmps = []

    def run(cmd, **kw):
        Image.new("RGB", (8, 8)).save(cmd[-1], "JPEG")
        tmps.append(cmd[-1])
        return type("R", (), {"returncode": 0, "stderr": ""})()
    monkeypatch.setattr(vf.subprocess, "run", run)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    for _ in range(2):
        out = vf.extract_frame(video, 1.0, tmp_path / "o", "sha")["path"]
        out.unlink()
    assert len(set(tmps)) == 2 and all(".part" in t for t in tmps)
