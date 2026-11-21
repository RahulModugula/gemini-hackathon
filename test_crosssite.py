"""Cross-site guard: money-spending endpoints reject cross-site requests (Sec-Fetch-Site / Origin) with 403."""
import pytest

from test_app import CARD, env  # noqa: F401  (env is a fixture)

STREAM = ("get", "/api/check/stream?claim=Toilet+105A+is+WC-2", {})
CHECK = ("post", "/api/check", {"json": {"claim": "x"}})
AUDIO = ("post", "/api/audio", {"content": b"abc", "headers": {"content-type": "audio/wav"}})
VIDEO = ("post", "/api/video", {"content": b"abc", "headers": {"content-type": "video/mp4"}})
RFI = ("post", "/api/rfi", {"json": {"card_id": "nope"}})
ALL = [STREAM, CHECK, AUDIO, VIDEO, RFI]


def go(c, spec, headers):
    method, url, kw = spec
    kw = dict(kw)
    kw["headers"] = {**kw.get("headers", {}), **headers}
    return getattr(c, method)(url, **kw)


@pytest.mark.parametrize("spec", ALL)
@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_cross_site_fetch_metadata_rejected(env, spec, site):  # noqa: F811
    c, _, calls = env
    r = go(c, spec, {"Sec-Fetch-Site": site})
    assert r.status_code == 403 and r.json() == {"error": "cross-site"}
    assert calls == []


@pytest.mark.parametrize("spec", ALL)
def test_foreign_origin_rejected(env, spec):  # noqa: F811
    c, _, calls = env
    for origin in ("https://evil.example", "null"):
        r = go(c, spec, {"Origin": origin})
        assert r.status_code == 403 and r.json() == {"error": "cross-site"}
    assert calls == []


@pytest.mark.parametrize("spec", [STREAM, CHECK])
def test_same_origin_and_plain_clients_work(env, spec):  # noqa: F811
    c, _, calls = env
    assert go(c, spec, {}).status_code == 200                                    # TestClient: no such headers
    assert go(c, spec, {"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert go(c, spec, {"Sec-Fetch-Site": "none"}).status_code == 200            # address-bar navigation
    assert go(c, spec, {"Origin": "http://testserver"}).status_code == 200       # == Host
    assert len(calls) >= 1 or spec is STREAM
