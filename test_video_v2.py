"""feat/video-v2 tests: frame evidence for video claims. No network: llm.call is faked; ffmpeg is real."""
import json
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app as appmod
import check
import llm
import video_claims as vc
import video_frames as vf
from roles import ROLES

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg absent")
# one solid colour per second: second i is COLOURS[i]
COLOURS = [("red", (255, 0, 0)), ("lime", (0, 255, 0)), ("blue", (0, 0, 255)), ("yellow", (255, 255, 0)),
           ("magenta", (255, 0, 255)), ("cyan", (0, 255, 255))]


def make_video(path, size="320x180", secs=6, gop=25):
    cmd = ["ffmpeg", "-v", "error", "-y"]
    for name, _ in COLOURS[:secs]:
        cmd += ["-f", "lavfi", "-i", f"color=c={name}:s={size}:d=1:r=25"]
    n = min(secs, len(COLOURS))
    cmd += ["-filter_complex", "".join(f"[{i}:v]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]", "-map", "[v]",
            "-pix_fmt", "yuv420p", "-g", str(gop), str(path)]
    subprocess.run(cmd, check=True)
    return path


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    return make_video(tmp_path_factory.mktemp("v") / "colours.mp4")


def mean_rgb(path):
    with Image.open(path) as im:
        px = list(im.convert("RGB").resize((8, 8)).getdata())
    return tuple(sum(p[i] for p in px) / len(px) for i in range(3))


def near(rgb, want, tol=45):
    return all(abs(a - b) <= tol for a, b in zip(rgb, want))


# ---- schema / validation ---------------------------------------------------------------------------------
def C(kind="spoken", a=0.5, b=2.0, **kw):
    return {"claim": "WC-2 in 105A", "kind": kind, "t_start_s": a, "t_end_s": b, "quote_or_observation": "q", **kw}


@pytest.fixture
def fake_call(monkeypatch):
    seen = {}

    def call(model, parts, **kw):
        seen.update(model=model, parts=parts, **kw)
        return SimpleNamespace(json=seen["payload"])

    monkeypatch.setattr(vc.llm, "call", call)
    monkeypatch.setattr(vc, "_ffprobe_duration", lambda d: None)
    return seen


def test_schema_asks_for_frame_t_s_not_boxes():
    item = vc.SCHEMA["properties"]["claims"]["items"]
    assert "frame_t_s" in item["required"] and item["properties"]["frame_t_s"] == {"type": "number"}
    assert "frame_box_2d" not in item["properties"]          # boxes are documented for images, not video
    assert "frame_t_s" in vc.PROMPT and "Do not give boxes" in vc.PROMPT


@pytest.mark.parametrize("given,want", [(1.25, 1.25), (0, 0.0), (8, 8.0),            # inside [0, dur]
                                        (99, 1.25), (-3, 1.25), ("x", 1.25), (None, 1.25), (float("nan"), 1.25)])
def test_frame_t_s_validated_else_midpoint_of_span(fake_call, given, want):
    fake_call["payload"] = {"claims": [C(a=0.5, b=2.0, frame_t_s=given)]}
    out = vc.extract_claims_from_video(b"", "video/mp4", duration=8.0)
    assert out[0]["frame_t_s"] == want


def test_missing_frame_t_s_uses_midpoint(fake_call):
    fake_call["payload"] = {"claims": [C(a=2, b=4)]}
    assert vc.extract_claims_from_video(b"", "video/mp4", duration=8.0)[0]["frame_t_s"] == 3.0


def test_boxes_from_the_video_call_are_ignored(fake_call):
    fake_call["payload"] = {"claims": [C("visual", frame_t_s=1, frame_box_2d=[1, 2, 3, 4]), C("spoken", frame_t_s=1)]}
    assert [c["frame_box_2d"] for c in vc.extract_claims_from_video(b"", "video/mp4", duration=8.0)] == [None, None]


@pytest.mark.parametrize("b,ok", [([100, 200, 300, 400], True), ([0, 0, 1000, 1000], True), ([300, 200, 100, 400], False),
                                  ([100, 200, 100, 400], False), ([-1, 0, 5, 5], False), ([0, 0, 5, 1001], False),
                                  ([1, 2, 3], False), (None, False), ("abcd", False), ([1, 2, 3, "4"], False),
                                  ([1, 2, 3, float("nan")], False), ([True, 0, 5, 5], False)])
def test_valid_box_2d(b, ok):
    assert vc.valid_box_2d(b) is ok


def test_locate_in_frame_uses_extract_role_on_an_image(monkeypatch, tmp_path):
    f = tmp_path / "f.jpg"
    Image.new("RGB", (64, 64), "red").save(f)
    seen = {}

    def call(model, parts, **kw):
        seen.update(model=model, parts=parts, **kw)
        return SimpleNamespace(json={"found": True, "box_2d": [100.4, 200, 300, 400], "text_read": "WC-3"})

    monkeypatch.setattr(vc.llm, "call", call)
    out = vc.locate_in_frame(f, "tag is WC-3", "tag on the bowl")
    assert out == {"box_2d": [100, 200, 300, 400], "text_read": "WC-3"}
    assert (seen["model"], seen["thinking"], seen["purpose"]) == (ROLES["extract"], "LOW", "video:locate")
    assert seen["parts"][0].inline_data.mime_type == "image/jpeg" and "tag on the bowl" in seen["parts"][1]


@pytest.mark.parametrize("j", [{"found": False, "box_2d": [0, 0, 0, 0], "text_read": ""},
                               {"found": True, "box_2d": [5, 5, 1, 1], "text_read": "x"}, {"found": True}, [], None])
def test_locate_in_frame_bad_answers_give_no_box(monkeypatch, tmp_path, j):
    f = tmp_path / "f.jpg"
    Image.new("RGB", (8, 8)).save(f)
    monkeypatch.setattr(vc.llm, "call", lambda *a, **k: SimpleNamespace(json=j))
    assert vc.locate_in_frame(f, "c", "o")["box_2d"] is None


# ---- frame extraction ------------------------------------------------------------------------------------
@needs_ffmpeg
@pytest.mark.parametrize("t,idx", [(0.0, 0), (0.5, 0), (1.5, 1), (2.5, 2), (3.4, 3), (4.5, 4), (5.5, 5), (5.9, 5)])
def test_extracted_frame_is_the_one_at_t(video, tmp_path, t, idx):
    r = vf.extract_frame(video, t, tmp_path / "frames")
    assert r["path"].suffix == ".jpg" and r["path"].parent == tmp_path / "frames"
    assert (r["width"], r["height"]) == (320, 180) and r["t_s"] == t
    assert near(mean_rgb(r["path"]), COLOURS[idx][1]), (t, mean_rgb(r["path"]), COLOURS[idx])


@needs_ffmpeg
def test_distinct_times_distinct_files_and_cache_hit(video, tmp_path):
    a = vf.extract_frame(video, 0.5, tmp_path)["path"]
    b = vf.extract_frame(video, 2.5, tmp_path)["path"]
    assert a != b and sorted(p.name for p in tmp_path.iterdir()) == sorted([a.name, b.name])
    m = a.stat().st_mtime_ns
    assert vf.extract_frame(video, 0.5, tmp_path)["path"] == a and a.stat().st_mtime_ns == m   # not re-extracted


@needs_ffmpeg
@pytest.mark.parametrize("size,want", [("2560x1440", (1280, 720)), ("1440x2560", (720, 1280)), ("640x360", (640, 360))])
def test_scaled_to_max_1280_never_enlarged(tmp_path, size, want):
    v = make_video(tmp_path / "big.mp4", size=size, secs=1)
    r = vf.extract_frame(v, 0.2, tmp_path / "f")
    assert (r["width"], r["height"]) == want


@needs_ffmpeg
@pytest.mark.parametrize("t", [-1, float("nan"), float("inf")])
def test_bad_time_is_value_error(video, tmp_path, t):
    with pytest.raises(ValueError):
        vf.extract_frame(video, t, tmp_path)


@needs_ffmpeg
def test_time_past_the_end_is_runtime_error_and_leaves_no_file(video, tmp_path):
    with pytest.raises(RuntimeError):
        vf.extract_frame(video, 60, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_crop_region_pads_and_clamps(tmp_path):
    src = tmp_path / "f.jpg"
    Image.new("RGB", (1000, 500), "white").save(src)
    out = vf.crop_region(src, [400, 400, 600, 600], tmp_path / "c.jpg", pad=0.3, min_px=100)   # box = 200x100 px
    with Image.open(out) as im:
        assert im.size == (320, 160)
    edge = vf.crop_region(src, [0, 0, 100, 100], tmp_path / "e.jpg")
    with Image.open(edge) as im:
        assert im.size[0] <= 1000 and im.size[1] <= 500 and im.size[0] > 0


def test_store_video_and_fmt_t(tmp_path):
    p, sha = vf.store_video(b"abc", "video/quicktime", tmp_path / "v")
    assert p.name == sha + ".mov" and p.read_bytes() == b"abc" and len(sha) == 40
    assert vf.store_video(b"abc", "video/mp4", tmp_path / "v")[0].name == sha + ".mp4"
    assert (vf.fmt_t(7.4), vf.fmt_t(0), vf.fmt_t(125.9), vf.fmt_t(-2)) == ("0:07", "0:00", "2:05", "0:00")


# ---- static serving --------------------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "BUILD", tmp_path)
    monkeypatch.setattr(llm, "run_spend", lambda rid=None: 0.0)
    (tmp_path / "secret.jpg").write_bytes(b"SECRET")
    (tmp_path / "cards.jsonl").write_text("SECRET")
    (tmp_path / "frames").mkdir()
    (tmp_path / "videos").mkdir()
    (tmp_path / "frames" / ("a" * 20 + ".jpg")).write_bytes(b"\xff\xd8jpg")
    (tmp_path / "frames" / ("b" * 20 + "_crop.jpg")).write_bytes(b"\xff\xd8crop")
    (tmp_path / "videos" / ("c" * 40 + ".mp4")).write_bytes(b"0123456789")
    (tmp_path / "frames" / "notes.txt").write_text("SECRET")
    return TestClient(appmod.app, raise_server_exceptions=False)


def test_frames_and_videos_are_served(client):
    r = client.get("/frames/" + "a" * 20 + ".jpg")
    assert r.status_code == 200 and r.content == b"\xff\xd8jpg"
    assert client.get("/frames/" + "b" * 20 + "_crop.jpg").status_code == 200
    r = client.get("/videos/" + "c" * 40 + ".mp4")
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4" and r.content == b"0123456789"
    r = client.get("/videos/" + "c" * 40 + ".mp4", headers={"Range": "bytes=2-4"})   # <video> seeking needs ranges
    assert r.status_code == 206 and r.content == b"234"


@pytest.mark.parametrize("path", ["/frames/..%2Fsecret.jpg", "/frames/../secret.jpg", "/frames/%2e%2e/secret.jpg",
                                  "/frames/..%2f..%2fcards.jsonl", "/frames/notes.txt", "/frames/" + "a" * 20 + ".png",
                                  "/frames/" + "A" * 20 + ".jpg", "/frames/" + "a" * 19 + ".jpg", "/frames/.jpg",
                                  "/frames/" + "a" * 20 + ".jpg%00.txt", "/frames/%2Fetc%2Fpasswd",
                                  "/videos/..%2Fsecret.jpg", "/videos/" + "c" * 40 + ".webm", "/videos/" + "c" * 39 + ".mp4",
                                  "/videos/" + "d" * 40 + ".mp4", "/frames/" + "e" * 20 + ".jpg"])
def test_static_rejects_traversal_and_odd_names(client, path):
    r = client.get(path)
    assert r.status_code in (404, 400) and b"SECRET" not in r.content


# ---- extra_images -> judges ------------------------------------------------------------------------------
def png(tmp_path, colour, name):
    p = tmp_path / name
    Image.new("RGB", (32, 32), colour).save(p, "JPEG")
    return p


def test_build_parts_places_field_photo_before_the_claim_text(tmp_path):
    crop = png(tmp_path, "white", "crop.png")
    crops = {"current": {"sheet": "add1-p018", "sheet_number": "P101", "png": crop, "words": [{"text": "WC-2"}]}}
    base = check.build_parts("c", crops)
    parts = check.build_parts("c", crops, [{"path": png(tmp_path, "red", "f.jpg"), "mime": "image/jpeg",
                                            "label": "FIELD PHOTO from the site video at 0:07"}])
    assert len(parts) == len(base) + 2 and parts[:-3] == base[:-1] and parts[-1] == base[-1]
    note, img = parts[-3], parts[-2]
    assert "FIELD PHOTO from the site video at 0:07" in note.text and "drawing images" in note.text
    assert img.inline_data.mime_type == "image/jpeg"
    assert check.build_parts("c", crops, None) == base == check.build_parts("c", crops, [])   # default: unchanged


def test_check_forwards_extra_images_to_both_judges(monkeypatch, tmp_path):
    seen = []

    def call(model, parts, *, schema=None, purpose, thinking=None):
        seen.append((model, parts))
        out = {"verdict": "unclear", "confidence": 0.1, "reason": "r", "quoted_words": [], "evidence": [],
               "changed_by_addendum": False, "needs_code": ""}
        return llm.Result(text=json.dumps(out), json=out)

    monkeypatch.setattr(llm, "call", call)
    extra = [{"path": png(tmp_path, "red", "f.jpg"), "mime": "image/jpeg", "label": "FIELD PHOTO XYZ"}]
    check.check("All the water closets in Toilet 105A are type WC-2.", extra_images=extra)
    assert {m for m, _ in seen} == {ROLES["judge"], ROLES["second"]}
    for _, parts in seen:
        texts = [getattr(p, "text", None) or "" for p in parts]
        assert any("FIELD PHOTO XYZ" in t for t in texts)
    seen.clear()
    check.check("All the water closets in Toilet 105A are type WC-2.")
    assert not any("FIELD PHOTO" in (getattr(p, "text", None) or "") for _, parts in seen for p in parts)


# ---- card shape ------------------------------------------------------------------------------------------
@needs_ffmpeg
def test_video_cards_carry_video_evidence_and_visual_claims_get_the_frame(tmp_path, monkeypatch, video):
    monkeypatch.setattr(appmod, "BUILD", tmp_path)
    monkeypatch.setattr(llm, "run_spend", lambda rid=None: 0.0)
    claims = [{"claim": "WC-2 in 105A", "kind": "spoken", "t_start_s": 0.5, "t_end_s": 1.5, "quote_or_observation": "q",
               "frame_t_s": 1.5, "frame_box_2d": None},
              {"claim": "Tag WC-3 on the bowl", "kind": "visual", "t_start_s": 2.0, "t_end_s": 3.0,
               "quote_or_observation": "tag", "frame_t_s": 3.5, "frame_box_2d": None},
              {"claim": "Bad time", "kind": "spoken", "t_start_s": 0, "t_end_s": 1, "quote_or_observation": "q",
               "frame_t_s": 90.0, "frame_box_2d": None}]
    monkeypatch.setattr(appmod.video_claims, "extract_claims_from_video", lambda d, m, h, du: claims)
    monkeypatch.setattr(appmod.video_claims, "locate_in_frame",
                        lambda path, claim, obs: {"box_2d": [200, 300, 600, 700], "text_read": "WC-3"})
    got = {}

    def fake_check(text, extra_images=None):
        got[text] = extra_images
        return {"claim": text, "speaker": None, "timestamp": "2026-11-21T00:00:00Z", "verdict": "unclear",
                "confidence": 0.0, "evidence": [], "spec_refs": [], "checks": [], "reason": "r", "rfi_draft": None}

    monkeypatch.setattr(appmod.checker, "check", fake_check)
    c = TestClient(appmod.app, raise_server_exceptions=False)
    r = c.post("/api/video?sheet=P101", content=video.read_bytes(), headers={"Content-Type": "video/mp4"})
    assert r.status_code == 200
    body = r.json()
    url = body["video_url"]
    assert url.startswith("/videos/") and url.endswith(".mp4") and (tmp_path / "videos" / url.split("/")[-1]).is_file()
    by = {x["claim"]: x for x in body["cards"]}
    sp, vis, bad = by["On P101, WC-2 in 105A"], by["On P101, Tag WC-3 on the bowl"], by["On P101, Bad time"]

    ve = sp["video_evidence"]
    assert set(ve) == {"frame", "t_s", "box_2d", "kind"} and ve["t_s"] == 1.5 and ve["box_2d"] is None
    assert ve["kind"] == "spoken" and sp["video_url"] == url
    frame = tmp_path / "frames" / ve["frame"].split("/")[-1]
    assert ve["frame"].startswith("/frames/") and near(mean_rgb(frame), COLOURS[1][1])      # second 1 is lime
    assert c.get(ve["frame"]).status_code == 200 and c.get(url).status_code == 200

    v = vis["video_evidence"]
    assert (v["kind"], v["t_s"], v["box_2d"], v["text_read"]) == ("visual", 3.5, [200, 300, 600, 700], "WC-3")
    assert near(mean_rgb(tmp_path / "frames" / v["frame"].split("/")[-1]), COLOURS[3][1])   # second 3 is yellow

    assert bad["video_evidence"] is None and bad["video_url"] == url      # frame past the end: card still produced
    assert got["On P101, WC-2 in 105A"] is None and got["On P101, Bad time"] is None   # spoken: no photo sent
    (x,) = got["On P101, Tag WC-3 on the bowl"]
    assert x["mime"] == "image/jpeg" and "0:03" in x["label"] and x["path"].name.endswith("_crop.jpg")
    assert near(mean_rgb(x["path"]), COLOURS[3][1])                                # the crop is of the same frame
    assert body["claims"][1]["frame_box_2d"] == [200, 300, 600, 700]
    ledger = [json.loads(ln) for ln in (tmp_path / "cards.jsonl").read_text().splitlines()]
    assert {x["video_evidence"]["kind"] if x["video_evidence"] else None for x in ledger} == {"spoken", "visual", None}


@needs_ffmpeg
def test_locate_failure_keeps_frame_and_sends_whole_frame(tmp_path, monkeypatch, video):
    monkeypatch.setattr(appmod, "BUILD", tmp_path)
    monkeypatch.setattr(llm, "run_spend", lambda rid=None: 0.0)
    monkeypatch.setattr(appmod.video_claims, "extract_claims_from_video", lambda d, m, h, du: [
        {"claim": "Tag", "kind": "visual", "t_start_s": 0, "t_end_s": 1, "quote_or_observation": "o", "frame_t_s": 0.5,
         "frame_box_2d": None}])
    monkeypatch.setattr(appmod.video_claims, "locate_in_frame",
                        lambda *a: (_ for _ in ()).throw(llm.LLMError("boom AIzaSECRET")))
    got = {}
    monkeypatch.setattr(appmod.checker, "check", lambda t, extra_images=None: got.update(extra=extra_images) or {
        "claim": t, "evidence": []})
    r = TestClient(appmod.app, raise_server_exceptions=False).post(
        "/api/video", content=video.read_bytes(), headers={"Content-Type": "video/mp4"})
    ve = r.json()["cards"][0]["video_evidence"]
    assert r.status_code == 200 and ve["box_2d"] is None and "SECRET" not in r.text
    assert got["extra"][0]["path"].name == ve["frame"].split("/")[-1]         # whole frame, no crop
