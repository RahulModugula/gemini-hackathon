"""Addendum impact digest: one long-context Neon call, verified in code. No network: llm.call is mocked and the
build dir is a tiny synthetic one (two addenda, three changed sheets)."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as appmod
import digest
import llm
from roles import ROLES

HERE = Path(__file__).parent


def W(text, x, y):
    return {"text": text, "bbox": [x, y, x + 30, y + 10]}


@pytest.fixture
def bld(tmp_path, monkeypatch):
    b = tmp_path / "build"
    (b / "words").mkdir(parents=True)
    old = [W("WC-2", 100, 100), W("NOTE", 200, 100), W("SINK", 300, 100)]
    cur = [W("WC-3", 100, 100), W("NOTE", 200, 100), W("SINK", 300, 100), W("WC-3", 100, 200), W('18"', 400, 100)]
    a2 = cur + [W("FD-9", 500, 500)]
    other = [W("X", 1, 1)]
    for name, words in (("dwg-p001", old), ("add1-p001", cur), ("add2-p001", a2), ("dwg-p002", other), ("add1-p002", other)):
        (b / "words" / f"{name}.json").write_text(json.dumps({"words": words}))
    sh = lambda s, src, o: {"sheet": s, "source": src, "issue_order": o, "page": 1, "width": 3024.0, "height": 2160.0}  # noqa: E731
    (b / "sheets.json").write_text(json.dumps({"sheets": [sh("dwg-p001", "dwg", 0), sh("add1-p001", "add1", 1), sh("add2-p001", "add2", 2),
                                                          sh("dwg-p002", "dwg", 0), sh("add1-p002", "add1", 1)],
                                               "sources": []}))
    row = lambda s, n, src, o, cur_: {"sheet": s, "sheet_number": n, "source": src, "issue_order": o, "current": cur_}  # noqa: E731
    (b / "sheet_index.json").write_text(json.dumps({"rows": [
        row("dwg-p001", "P101", "dwg", 0, False), row("add1-p001", "P101", "add1", 1, False), row("add2-p001", "P101", "add2", 2, True),
        row("dwg-p002", "A201", "dwg", 0, False), row("add1-p002", "A201", "add1", 1, True)]}))
    (b / "addenda_changes.json").write_text(json.dumps([
        {"source": "add1", "sheet_number": "P101", "title": "PLUMBING PLAN", "changes": [{"action": "REVISE", "text": "Water closets."}]},
        {"source": "add2", "sheet_number": "P101", "title": "PLUMBING PLAN", "changes": [{"action": "ADD", "text": "Floor drain."}]},
        {"source": "add1", "sheet_number": "A201", "title": "FIRST FLOOR PLAN", "changes": [{"action": "REVISE", "text": "Cubbies."}]}]))
    (b / "qa_index.json").write_text(json.dumps([{"id": "add1-q1", "source": "Addendum 1", "page": 2, "question": "Which WC?", "response": "WC-3."}]))
    monkeypatch.setattr(digest, "_render_crop", lambda sheet, rect, build: f"/crops/{sheet}.png")
    return b


def item(**kw):
    return {"sheet_number": "P101", "addendum": "add1", "what_changed": "Water closets changed.", "trades_affected": ["Plumbing"],
            "risk": "Fixture price differs.", "evidence_tokens": ["WC-3"], "qa_refs": ["add1-q1"], **kw}


def fake_llm(items, calls=None):
    def call(model, parts, *, schema=None, purpose, thinking=None):
        if calls is not None:
            calls.append({"model": model, "parts": parts, "schema": schema, "thinking": thinking, "purpose": purpose})
        return llm.Result(text=json.dumps({"items": items}), json={"items": items}, usage={"prompt_tokens": 10}, cost_usd=0.01)
    return call


# ---- context ----------------------------------------------------------------------------------------------
def test_context_has_word_diffs_with_bboxes_and_qa_and_no_images(bld):
    ctx = digest.build_context(bld)
    assert set(ctx["diffs"]) == {("add1", "P101"), ("add2", "P101"), ("add1", "A201")}
    d = ctx["diffs"][("add1", "P101")]
    assert d["only_cur"] == {"WC-3": 2, '18"': 1} and d["only_old"] == {"WC-2": 1}
    assert [b["bbox"] for b in d["cur_boxes"]["WC-3"]] == [[100, 100, 130, 110], [100, 200, 130, 210]]
    assert ctx["diffs"][("add2", "P101")]["only_cur"] == {"FD-9": 1}              # diffed against add1, not the original
    assert "[add1-q1]" in ctx["text"] and "Which WC?" in ctx["text"] and "WC-2" in ctx["text"] and "[100,200,130,210]" in ctx["text"]
    assert 0 < ctx["est_tokens"] < digest.TOKEN_BUDGET


def test_context_over_budget_is_refused(bld, monkeypatch):
    monkeypatch.setattr(digest, "TOKEN_BUDGET", 10)
    with pytest.raises(digest.DigestError):
        digest.build_context(bld)


# ---- verifier ---------------------------------------------------------------------------------------------
def kept(bld, items):
    return digest.verify_items(items, digest.build_context(bld), bld)


def test_verifier_keeps_a_grounded_item_and_attaches_boxes(bld):
    ok, dropped = kept(bld, [item()])
    assert not dropped and len(ok) == 1
    it = ok[0]
    assert it["sheet"] == "add1-p001" and it["flags"] == [] and it["evidence_tokens"] == ["WC-3"]
    assert [b["bbox"] for b in it["boxes"]] == [[100, 100, 130, 110], [100, 200, 130, 210]]
    assert it["crop"] == "/crops/add1-p001.png" and all(0 <= v <= 1000 for b in it["boxes"] for v in b["box_2d_on_crop"])


def test_verifier_drops_hallucinated_tokens(bld):
    ok, dropped = kept(bld, [item(evidence_tokens=["WC-9", "NOTE"])])           # NOTE exists but is unchanged: not in the diff
    assert ok == [] and dropped[0]["reason"] == "evidence_tokens not in the sheet diff"


def test_verifier_drops_unchanged_or_unknown_sheet_and_wrong_addendum(bld):
    ok, dropped = kept(bld, [item(sheet_number="Z999"), item(addendum="add2", sheet_number="A201"), item(addendum="add9")])
    assert ok == [] and [d["reason"] for d in dropped] == ["sheet not changed by that addendum"] * 3


def test_verifier_flags_partial_tokens_and_bad_qa_refs(bld):
    ok, dropped = kept(bld, [item(evidence_tokens=["WC-3", "FAKE"], qa_refs=["add1-q1", "add1-q99"])])
    it = ok[0]
    assert not dropped and it["evidence_tokens"] == ["WC-3"] and it["qa_refs"] == ["add1-q1"]
    assert "unverified evidence tokens removed: FAKE" in it["flags"] and "unknown qa_refs removed: add1-q99" in it["flags"]


def test_verifier_token_match_is_case_insensitive_and_removed_only_gets_estimated_box(bld):
    ok, _ = kept(bld, [item(evidence_tokens=["wc-3"]), item(evidence_tokens=["WC-2"])])
    assert ok[0]["evidence_tokens"] == ["WC-3"]
    assert "removed text only: box is estimated from the old sheet" in ok[1]["flags"] and len(ok[1]["boxes"]) == 1


def test_verifier_tolerates_garbage_items(bld):
    ok, dropped = kept(bld, [None, "x", {"sheet_number": 5}, item(evidence_tokens="WC-3"), item(trades_affected="Plumbing")])
    assert len(dropped) >= 3
    assert all(isinstance(i["trades_affected"], list) for i in ok)


# ---- run / cache ------------------------------------------------------------------------------------------
def test_run_makes_one_neon_call_and_writes_digest_json(bld, monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "call", fake_llm([item(), item(evidence_tokens=["NOPE"]), item(addendum="add2", evidence_tokens=["FD-9"], qa_refs=[])], calls))
    out = digest.run(bld)
    assert len(calls) == 1 and calls[0]["model"] == ROLES["audit"] == "gemini-4-neon" and calls[0]["thinking"] == "LOW"
    assert calls[0]["schema"]["properties"]["items"]["items"]["required"] and len(calls[0]["parts"]) == 1
    assert [i["addendum"] for i in out["items"]] == ["add1", "add2"] and len(out["dropped"]) == 1
    assert out["model"] == "gemini-4-neon" and out["est_tokens"] > 0 and out["changed_sheets"] == 3
    assert json.loads((bld / "digest.json").read_text())["items"] == out["items"]


def test_run_does_not_overwrite_digest_when_the_call_fails(bld, monkeypatch):
    (bld / "digest.json").write_text('{"items": ["keep"]}')

    def boom(*a, **k):
        raise llm.LLMError("down")
    monkeypatch.setattr(llm, "call", boom)
    with pytest.raises(llm.LLMError):
        digest.run(bld)
    assert json.loads((bld / "digest.json").read_text()) == {"items": ["keep"]}


# ---- endpoints --------------------------------------------------------------------------------------------
@pytest.fixture
def client(bld, monkeypatch):
    monkeypatch.setattr(appmod, "BUILD", bld)
    return TestClient(appmod.app, raise_server_exceptions=False), bld


def test_get_digest_404_then_serves_cached_file(client):
    c, b = client
    r = c.get("/api/digest")
    assert r.status_code == 404 and "error" in r.json()
    (b / "digest.json").write_text(json.dumps({"items": [{"sheet_number": "P101"}], "dropped": []}))
    r = c.get("/api/digest")
    assert r.status_code == 200 and r.json()["items"][0]["sheet_number"] == "P101"


def test_refresh_runs_digest_and_get_returns_it(client, monkeypatch):
    c, b = client
    monkeypatch.setattr(llm, "call", fake_llm([item()]))
    r = c.post("/api/digest/refresh")
    assert r.status_code == 200 and len(r.json()["items"]) == 1 and r.json()["model"] == "gemini-4-neon"
    assert c.get("/api/digest").json()["items"] == r.json()["items"]


@pytest.mark.parametrize("hdr", [{"sec-fetch-site": "cross-site"}, {"origin": "http://evil.example"}])
def test_refresh_is_cross_site_guarded(client, monkeypatch, hdr):
    c, b = client
    calls = []
    monkeypatch.setattr(llm, "call", fake_llm([item()], calls))
    assert c.post("/api/digest/refresh", headers=hdr).status_code == 403
    assert not calls and not (b / "digest.json").exists()


def test_refresh_failure_is_503_without_leak(client, monkeypatch):
    c, _ = client

    def boom(*a, **k):
        raise llm.LLMError("secret prompt text")
    monkeypatch.setattr(llm, "call", boom)
    r = c.post("/api/digest/refresh")
    assert r.status_code == 503 and "secret" not in r.text


# ---- UI (static) ------------------------------------------------------------------------------------------
def test_ui_has_digest_view_without_html_sinks():
    page = (HERE / "ui" / "index.html").read_text()
    for s in ('id="tabDigest"', 'id="digestView"', "Addendum digest", "/api/digest", "#digest", "function renderDigest",
              "trade-chip", "digest-risk", "zoomFig("):
        assert s in page, s
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in page, sink
