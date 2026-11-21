import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.genai import errors, types

import llm


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, **kw):
        self.calls.append(kw)
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


def resp(text="ok", p=1000, o=200, t=100, c=0):
    u = SimpleNamespace(prompt_token_count=p, candidates_token_count=o, thoughts_token_count=t,
                        cached_content_token_count=c)
    return SimpleNamespace(text=text, usage_metadata=u)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "BUILD", tmp_path / "build")
    monkeypatch.setattr(llm, "_sleep", lambda s: None)
    monkeypatch.setattr(llm, "_today", lambda: dt.date(2026, 11, 21))
    monkeypatch.setattr(llm, "_client", None)
    for k in ("LLM_CACHE", "LLM_RUN_CAP_USD", "GOOGLE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "  secret-key-123 \n")
    monkeypatch.setenv("LLM_RUN_ID", "run-test")

    def install(*outcomes):
        fc = FakeClient(outcomes)
        monkeypatch.setattr(llm, "_client", fc)
        return fc
    return install


def rows(tmp=None):
    return [json.loads(x) for x in (llm.BUILD / "calls.jsonl").read_text().splitlines()]


def test_import_needs_no_key_and_client_gets_stripped_explicit_key(env, monkeypatch):
    seen = {}
    monkeypatch.setattr(llm.genai, "Client", lambda **kw: seen.update(kw) or FakeClient([resp()]))
    assert llm._client is None  # lazy: nothing built at import
    llm.call("gemini-4-hydrogen", ["hi"], purpose="t")
    assert seen == {"api_key": "secret-key-123"}


def test_missing_key_errors_clearly(env, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY")
    with pytest.raises(llm.LLMError, match="GEMINI_API_KEY"):
        llm.call("gemini-4-hydrogen", ["hi"], purpose="t")


def test_structured_output_config_and_parse(env):
    fc = env(resp('{"a": 1}'))
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    r = llm.call("gemini-4-hydrogen", ["hi"], schema=schema, purpose="t", thinking="LOW")
    cfg = fc.calls[0]["config"]
    assert cfg.response_mime_type == "application/json" and cfg.response_json_schema == schema
    assert cfg.thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert r.json == {"a": 1}


def test_invalid_json_raises_and_is_not_cached(env):
    env(resp("not json"))
    with pytest.raises(llm.LLMError, match="invalid JSON"):
        llm.call("gemini-4-hydrogen", ["hi"], schema={"type": "object"}, purpose="t")
    assert not (llm.BUILD / "cache").exists()
    assert rows()[-1]["error"].startswith("model returned invalid JSON")
    assert rows()[-1]["cost_usd"] > 0  # the tokens were still billed


def test_log_fields_cost_and_no_secrets(env):
    env(resp("SECRET PROMPT ANSWER", p=1_000_000, o=100_000, t=50_000, c=7))
    r = llm.call("gemini-4-neon", ["very private prompt"], purpose="p1")
    # 3.00*1 + 15*(0.1+0.05) = 5.25 ; thinking billed at output rate
    assert r.cost_usd == pytest.approx(5.25) and r.cached is False
    row = rows()[0]
    for k in ("ts", "model", "purpose", "prompt_tokens", "output_tokens", "thinking_tokens", "cached_tokens",
              "latency_s", "cost_usd", "cache_hit", "run_id", "error"):
        assert k in row
    assert (row["prompt_tokens"], row["output_tokens"], row["thinking_tokens"], row["cached_tokens"]) == (
        1_000_000, 100_000, 50_000, 7)
    raw = (llm.BUILD / "calls.jsonl").read_text()
    assert "secret-key-123" not in raw and "very private prompt" not in raw
    assert len(row["request_sha256"]) == 64


def test_none_usage_fields_are_zero(env):
    env(SimpleNamespace(text="x", usage_metadata=SimpleNamespace(
        prompt_token_count=5, candidates_token_count=None, thoughts_token_count=None, cached_content_token_count=None)))
    r = llm.call("gemini-4-hydrogen", ["hi"], purpose="t")
    assert r.usage == {"prompt_tokens": 5, "output_tokens": 0, "thinking_tokens": 0, "cached_tokens": 0}


def test_pricing_date_aware_and_unknown_model(env):
    assert llm.price("gemini-4-carbon", dt.date(2026, 11, 30)) == (2.00, 10.00)
    assert llm.price("gemini-4-argon", dt.date(2026, 12, 1)) == (4.00, 20.00)
    assert llm.price("gemini-4-hydrogen", dt.date(2027, 1, 1)) == (1.50, 7.50)
    fc = env()
    with pytest.raises(llm.UnknownModel):
        llm.call("gemini-9-mystery", ["hi"], purpose="t")
    assert fc.calls == [] and rows()[0]["error"].startswith("UnknownModel")


def test_cap_refuses_without_calling_client(env, monkeypatch):
    fc = env(resp(), resp())
    monkeypatch.setenv("LLM_RUN_CAP_USD", "0.05")
    # worst case on hydrogen: 8192 * 7.5/1e6 = 0.0614 > 0.05
    with pytest.raises(llm.CapExceeded):
        llm.call("gemini-4-hydrogen", ["hi"], purpose="t")
    assert fc.calls == []
    assert rows()[0]["error"].startswith("CapExceeded")


def test_cap_counts_prior_spend_of_this_run_only(env, monkeypatch):
    fc = env(resp(p=1_000_000, o=0, t=0), resp())
    llm.call("gemini-4-hydrogen", ["a"], purpose="t")  # $1.50 spent
    monkeypatch.setenv("LLM_RUN_CAP_USD", "1.55")
    with pytest.raises(llm.CapExceeded):
        llm.call("gemini-4-hydrogen", ["b"], purpose="t")
    monkeypatch.setenv("LLM_RUN_ID", "other-run")  # fresh run has its own budget
    llm.call("gemini-4-hydrogen", ["b"], purpose="t")
    assert len(fc.calls) == 2


def test_retries_on_429_5xx_then_succeeds_with_backoff(env, monkeypatch):
    sleeps = []
    monkeypatch.setattr(llm, "_sleep", sleeps.append)
    fc = env(errors.APIError(429, {}), errors.APIError(503, {}), errors.APIError(500, {}), resp())
    assert llm.call("gemini-4-hydrogen", ["hi"], purpose="t").text == "ok"
    assert len(fc.calls) == 4 and len(sleeps) == 3 and sleeps[2] > sleeps[0] * 1.0


def test_gives_up_after_4_retries(env):
    fc = env(*[errors.APIError(503, {}) for _ in range(5)])
    with pytest.raises(errors.APIError):
        llm.call("gemini-4-hydrogen", ["hi"], purpose="t")
    assert len(fc.calls) == 5
    assert rows()[-1]["error"] == "APIError: 503"


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_no_retry_on_other_4xx(env, code):
    fc = env(errors.APIError(code, {}), resp())
    with pytest.raises(errors.APIError):
        llm.call("gemini-4-hydrogen", ["hi"], purpose="t")
    assert len(fc.calls) == 1


def test_cache_roundtrip_and_hit_is_logged_free(env):
    fc = env(resp("A"))
    img = llm.image_part(b"\x89PNGdata", "image/png")
    r1 = llm.call("gemini-4-hydrogen", ["q", img], purpose="t")
    r2 = llm.call("gemini-4-hydrogen", ["q", img], purpose="t")
    assert (r1.cached, r2.cached, r2.text, r2.cost_usd) == (False, True, "A", 0.0)
    assert len(fc.calls) == 1
    assert [r["cache_hit"] for r in rows()] == [False, True]
    assert r2.usage == r1.usage


def test_cache_key_hashes_bytes_and_covers_all_inputs():
    k = llm.cache_key
    a, b = llm.image_part(b"aaa", "image/png"), llm.image_part(b"aab", "image/png")
    assert k("m", [a], None, None) == k("m", [llm.image_part(b"aaa", "image/png")], None, None)
    assert len({k("m", [a], None, None), k("m", [b], None, None), k("n", [a], None, None),
                k("m", [a], {"x": 1}, None), k("m", [a], None, "LOW")}) == 5


def test_replay_mode_makes_zero_client_calls(env, monkeypatch):
    fc = env(resp("A"))
    llm.call("gemini-4-hydrogen", ["q"], purpose="t")  # record
    monkeypatch.setenv("LLM_CACHE", "replay")
    assert llm.call("gemini-4-hydrogen", ["q"], purpose="t").text == "A"
    with pytest.raises(llm.CacheMiss):
        llm.call("gemini-4-hydrogen", ["other"], purpose="t")
    assert len(fc.calls) == 1  # only the recording call


def test_cache_off_never_reads_or_writes(env, monkeypatch):
    fc = env(resp("A"), resp("B"))
    monkeypatch.setenv("LLM_CACHE", "off")
    llm.call("gemini-4-hydrogen", ["q"], purpose="t")
    assert llm.call("gemini-4-hydrogen", ["q"], purpose="t").text == "B"
    assert len(fc.calls) == 2 and not (llm.BUILD / "cache").exists()


def test_part_helpers(tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(b"xyz")
    for part in (llm.image_part(p, "image/png"), llm.image_part(b"xyz", "image/png"),
                 llm.audio_part(b"xyz", "audio/wav")):
        assert part.inline_data.data == b"xyz"
    assert llm.audio_part(b"x", "audio/wav").inline_data.mime_type == "audio/wav"
    assert llm.text_part("hi").text == "hi"


def test_cli_price_table_only():
    d = Path(llm.__file__).parent
    ok = subprocess.run([sys.executable, "llm.py", "--price-table"], cwd=d, capture_output=True, text=True)
    assert ok.returncode == 0 and "gemini-4-carbon" in ok.stdout and "2026-11-21" in ok.stdout
    bad = subprocess.run([sys.executable, "llm.py", "--nope"], cwd=d, capture_output=True, text=True)
    assert bad.returncode != 0
