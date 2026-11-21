"""Adversarial tests for llm.py, rfi.py and qa_index.py. Tests only: no product code is changed.

No network: the genai client is replaced by a fake (as in test_llm.py), BUILD is redirected to tmp_path, and the
RFI model call is replaced by a fake. Real bugs are marked xfail(strict=True) with the failing input in the reason.
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest
from google.genai import errors, types

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import llm  # noqa: E402
import qa_index  # noqa: E402
import rfi  # noqa: E402

REAL_TODAY = llm._today  # captured before any monkeypatching
HYDROGEN = "gemini-4-hydrogen"
CARBON = "gemini-4-carbon"
ARGON = "gemini-4-argon"
UTC = dt.timezone.utc


# ---- llm helpers -------------------------------------------------------------------------------
class FakeClient:
    def __init__(self, outcomes=(), default=None, delay=0.0):
        self.outcomes, self.default, self.delay = list(outcomes), default, delay
        self.calls, self.lock = [], threading.Lock()
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, **kw):
        if self.delay:
            threading.Event().wait(self.delay)
        with self.lock:
            self.calls.append(kw)
            if self.outcomes:
                o = self.outcomes.pop(0)
            elif self.default is not None:
                o = self.default
            else:
                raise AssertionError("client called more times than scripted")
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
    for k in ("LLM_CACHE", "LLM_RUN_CAP_USD", "GOOGLE_API_KEY", "LLM_RUN_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "  secret-key-123 \n")
    monkeypatch.setenv("LLM_RUN_ID", "run-test")

    def install(*outcomes, default=None, delay=0.0):
        fc = FakeClient(outcomes, default=default, delay=delay)
        monkeypatch.setattr(llm, "_client", fc)
        return fc
    return install


def allrows():
    return [json.loads(x) for x in (llm.BUILD / "calls.jsonl").read_text().splitlines()]


def rows():
    return [r for r in allrows() if r.get("kind") != "reserve"]


def worst(model=HYDROGEN, parts=("hi",), thinking=None):
    return llm.worst_case_usd(model, list(parts), thinking)


def freeze_clock(monkeypatch, when):
    """Pin llm's notion of 'now' (the module reads dt.datetime.now(UTC) for the price day and the run id)."""
    class FrozenDT(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return when.astimezone(tz) if tz else when.replace(tzinfo=None)
    monkeypatch.setattr(llm, "dt", SimpleNamespace(datetime=FrozenDT, date=dt.date, timezone=dt.timezone))
    monkeypatch.setattr(llm, "_today", REAL_TODAY)


# ---- llm: cap boundary -------------------------------------------------------------------------
def test_cap_equal_to_worst_case_is_allowed(env, monkeypatch):
    fc = env(default=resp())
    monkeypatch.setenv("LLM_RUN_CAP_USD", repr(worst()))  # exact float round-trip
    llm.call(HYDROGEN, ["hi"], purpose="t")
    assert len(fc.calls) == 1


def test_cap_one_nanodollar_below_worst_case_is_refused(env, monkeypatch):
    fc = env(default=resp())
    monkeypatch.setenv("LLM_RUN_CAP_USD", repr(worst() - 1e-9))
    with pytest.raises(llm.CapExceeded):
        llm.call(HYDROGEN, ["hi"], purpose="t")
    assert fc.calls == [] and rows()[0]["error"].startswith("CapExceeded")


def test_cap_one_nanodollar_above_worst_case_is_allowed(env, monkeypatch):
    fc = env(default=resp())
    monkeypatch.setenv("LLM_RUN_CAP_USD", repr(worst() + 1e-9))
    llm.call(HYDROGEN, ["hi"], purpose="t")
    assert len(fc.calls) == 1


def test_cap_zero_refuses_every_live_call_as_cap_not_config(env, monkeypatch):
    fc = env(default=resp())
    monkeypatch.setenv("LLM_RUN_CAP_USD", "0")
    with pytest.raises(llm.CapExceeded):
        llm.call(HYDROGEN, ["hi"], purpose="t")
    assert fc.calls == []


@pytest.mark.parametrize("bad", ["-1", "nan", "inf", "abc", "", " ", "1e400"])
def test_invalid_cap_env_is_a_config_error_before_any_call(env, monkeypatch, bad):
    fc = env(default=resp())
    monkeypatch.setenv("LLM_RUN_CAP_USD", bad)
    with pytest.raises(llm.LLMError, match="LLM_RUN_CAP_USD"):
        llm.call(HYDROGEN, ["hi"], purpose="t")
    assert fc.calls == []


def test_fifty_threads_never_push_settled_plus_reserved_over_cap(env, monkeypatch):
    w = worst()
    fc = env(default=resp(), delay=0.5)  # calls stay in flight until every thread has tried to reserve
    monkeypatch.setenv("LLM_RUN_CAP_USD", repr(3.5 * w))  # room for exactly three in-flight worst cases
    results, lock = [], threading.Lock()

    def go(_):
        try:
            llm.call(HYDROGEN, ["q"], purpose="t")
            r = "ok"
        except llm.CapExceeded:
            r = "capped"
        with lock:
            results.append(r)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(50)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert results.count("ok") == 3 and results.count("capped") == 47 and len(fc.calls) == 3
    # Replay the log in order: at no point may settled cost + open reservations exceed the cap.
    held, settled, peak = {}, 0.0, 0.0
    for r in allrows():
        if r.get("kind") == "reserve":
            held[r["reservation_id"]] = r["reserved_usd"]
        else:
            settled += r.get("cost_usd") or 0.0
            if r.get("reservation_id"):
                held.pop(r["reservation_id"], None)
        peak = max(peak, settled + sum(held.values()))
    assert peak <= 3.5 * w


# ---- llm: corrupted calls.jsonl ----------------------------------------------------------------
def _write_log(text):
    llm.BUILD.mkdir(parents=True, exist_ok=True)
    (llm.BUILD / "calls.jsonl").write_text(text)


def test_partial_json_line_is_skipped_and_valid_reservation_still_counts(env):
    env(default=resp())
    _write_log(json.dumps({"ts": "x", "run_id": "run-test", "kind": "reserve", "reservation_id": "a",
                           "reserved_usd": 4.9}) + "\n" + '{"ts": "x", "run_id": "run-te\n')
    assert llm.run_spend() == pytest.approx(4.9)
    with pytest.raises(llm.CapExceeded):
        llm._reserve(0.5, 5.0)


@pytest.mark.parametrize("junk", ["not json at all\n", "\x00\x01\x02\n", "{\"kind\": \"reserve\"\n"])
def test_garbage_lines_do_not_crash_and_do_not_hide_reservations(env, junk):
    env(default=resp())
    _write_log(junk + json.dumps({"run_id": "run-test", "kind": "reserve", "reservation_id": "a",
                                  "reserved_usd": 4.99}) + "\n")
    assert llm.run_spend() == pytest.approx(4.99)


def test_empty_calls_log_means_zero_spend_and_calls_proceed(env, monkeypatch):
    fc = env(default=resp())
    _write_log("")
    assert llm.run_spend() == 0.0
    monkeypatch.setenv("LLM_RUN_CAP_USD", "5")
    llm.call(HYDROGEN, ["hi"], purpose="t")
    assert len(fc.calls) == 1


@pytest.mark.xfail(strict=True, reason="BUG: a valid-JSON line that is not an object (e.g. 'null') makes run_spend "
                   "raise AttributeError, so every live call fails. Input: calls.jsonl containing the single line 'null'")
def test_valid_non_object_json_line_does_not_break_spend(env):
    fc = env(default=resp())
    _write_log("null\n")
    llm.call(HYDROGEN, ["hi"], purpose="t")
    assert len(fc.calls) == 1


@pytest.mark.xfail(strict=True, reason="BUG: a crash mid-write leaves a partial last line with no newline; the next "
                   "append is glued onto it, so that reservation is invisible to run_spend and a later reservation "
                   "is admitted past the cap. Input: partial line w/o newline, then _reserve(0.5, cap=5.0), "
                   "then _reserve(4.6, cap=5.0) is admitted (total 5.1)")
def test_partial_line_without_newline_does_not_hide_next_reservation(env):
    _write_log('{"ts": "x", "run_id": "run-test", "kind": "reserve", "reservation_id": "old", "reserved_usd": 4.9')
    llm._reserve(0.5, 5.0)
    with pytest.raises(llm.CapExceeded):
        llm._reserve(4.6, 5.0)


@pytest.mark.xfail(strict=True, reason="BUG: if the success row cannot be written, the outer handler logs the call "
                   "with cost 0 and settles its reservation, so a billed call vanishes from the run cap. Input: "
                   "successful call (1000/200/100 tokens, $0.00375) whose log write raises OSError")
def test_failed_success_log_still_counts_the_billed_cost(env, monkeypatch):
    env(default=resp())
    real_append = llm._append

    def flaky(row):
        if row.get("kind") != "reserve" and row.get("error") is None:
            raise OSError("disk full")
        real_append(row)
    monkeypatch.setattr(llm, "_append", flaky)
    with pytest.raises(OSError):
        llm.call(HYDROGEN, ["hi"], purpose="t")
    assert llm.run_spend() == pytest.approx(0.00375)


# ---- llm: cache key stability ------------------------------------------------------------------
def test_cache_key_ignores_dict_insertion_order():
    a = {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "string"}}}
    b = {"properties": {"b": {"type": "string"}, "a": {"type": "integer"}}, "type": "object"}
    assert llm.cache_key(HYDROGEN, ["q"], a, None) == llm.cache_key(HYDROGEN, ["q"], b, None)


def test_cache_key_is_by_content_not_object_identity():
    assert llm.cache_key(HYDROGEN, [llm.image_part(b"".join([b"a", b"aa"]), "image/png")], None, None) == \
        llm.cache_key(HYDROGEN, [llm.image_part(b"aaa", "image/png")], None, None)
    assert llm.cache_key(HYDROGEN, [b"aaa"], None, None) == llm.cache_key(HYDROGEN, [bytearray(b"aaa")], None, None)
    assert llm.cache_key(HYDROGEN, [b"aaa"], None, None) != llm.cache_key(HYDROGEN, [b"aab"], None, None)


def test_cache_key_changes_with_mime_type():
    assert llm.cache_key(HYDROGEN, [llm.image_part(b"x", "image/png")], None, None) != \
        llm.cache_key(HYDROGEN, [llm.image_part(b"x", "image/jpeg")], None, None)


def test_cache_key_stable_across_processes_and_hash_seeds():
    code = ("import llm; print(llm.cache_key('gemini-4-hydrogen', ['q', llm.image_part(b'abc', 'image/png')], "
            "{'b': 1, 'a': [{'y': 2, 'x': 1}]}, 'LOW'))")
    keys = set()
    for seed in ("1", "2"):
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True,
                             env={**os.environ, "PYTHONHASHSEED": seed})
        assert out.returncode == 0, out.stderr
        keys.add(out.stdout.strip())
    assert len(keys) == 1 and len(keys.pop()) == 64


# ---- llm: replay with corrupt or half-written cache --------------------------------------------
CORRUPT = {
    "empty": b"", "truncated": b"{trunc", "null": b"null", "list": b"[]", "text-not-str": b'{"text": 5, "usage": {}}',
    "no-usage": b'{"text": "A"}', "usage-not-dict": b'{"text": "A", "usage": []}', "binary": b"\xff\xfe\x00",
}


@pytest.mark.parametrize("content", list(CORRUPT.values()), ids=list(CORRUPT))
def test_replay_with_corrupt_cache_file_is_cache_miss_never_network(env, monkeypatch, content):
    fc = env()
    path = llm.BUILD / "cache" / f"{llm.cache_key(HYDROGEN, ['q'], None, None)}.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    monkeypatch.setenv("LLM_CACHE", "replay")
    with pytest.raises(llm.CacheMiss):
        llm.call(HYDROGEN, ["q"], purpose="t")
    assert fc.calls == []


def test_replay_with_half_written_cache_file_is_cache_miss(env, monkeypatch):
    fc = env()
    entry = json.dumps({"text": "A considerably longer answer body", "usage": {"prompt_tokens": 1}})
    path = llm.BUILD / "cache" / f"{llm.cache_key(HYDROGEN, ['q'], None, None)}.json"
    path.parent.mkdir(parents=True)
    path.write_text(entry[: len(entry) // 2])
    monkeypatch.setenv("LLM_CACHE", "replay")
    with pytest.raises(llm.CacheMiss):
        llm.call(HYDROGEN, ["q"], purpose="t")
    assert fc.calls == []


def test_replay_with_cache_path_being_a_directory_is_cache_miss(env, monkeypatch):
    fc = env()
    (llm.BUILD / "cache" / f"{llm.cache_key(HYDROGEN, ['q'], None, None)}.json").mkdir(parents=True)
    monkeypatch.setenv("LLM_CACHE", "replay")
    with pytest.raises(llm.CacheMiss):
        llm.call(HYDROGEN, ["q"], purpose="t")
    assert fc.calls == []


# ---- llm: retries ------------------------------------------------------------------------------
def test_429_then_success_retries_once_and_settles_only_the_success(env, monkeypatch):
    sleeps = []
    monkeypatch.setattr(llm, "_sleep", sleeps.append)
    fc = env(errors.APIError(429, {}), resp())
    assert llm.call(HYDROGEN, ["hi"], purpose="t").text == "ok"
    assert len(fc.calls) == 2 and len(sleeps) == 1
    assert llm.run_spend() == pytest.approx(0.00375)  # one billed success, not two


def test_500_five_times_gives_up_after_configured_retries(env, monkeypatch):
    sleeps = []
    monkeypatch.setattr(llm, "_sleep", sleeps.append)
    fc = env(*[errors.APIError(500, {}) for _ in range(5)])
    with pytest.raises(errors.APIError):
        llm.call(HYDROGEN, ["hi"], purpose="t")
    assert len(fc.calls) == llm.RETRIES + 1 == 5 and len(sleeps) == llm.RETRIES
    assert all(s <= 30 * 1.5 for s in sleeps)  # backoff is capped
    assert rows()[-1]["error"] == "APIError: 500"
    assert llm.run_spend() == 0.0  # the failed attempt's reservation is released


def test_429_five_times_gives_up_too(env):
    fc = env(*[errors.APIError(429, {}) for _ in range(5)])
    with pytest.raises(errors.APIError):
        llm.call(HYDROGEN, ["hi"], purpose="t")
    assert len(fc.calls) == 5


def test_400_is_never_retried(env, monkeypatch):
    sleeps = []
    monkeypatch.setattr(llm, "_sleep", sleeps.append)
    fc = env(errors.APIError(400, {}), resp())
    with pytest.raises(errors.APIError):
        llm.call(HYDROGEN, ["hi"], purpose="t")
    assert len(fc.calls) == 1 and sleeps == []


# ---- llm: key scrubbing ------------------------------------------------------------------------
def test_key_echoed_in_exception_text_never_reaches_calls_log(env):
    env(RuntimeError("upstream rejected key secret-key-123 as invalid"))
    with pytest.raises(RuntimeError):
        llm.call(HYDROGEN, ["hi"], purpose="t")
    raw = (llm.BUILD / "calls.jsonl").read_text()
    assert "secret-key-123" not in raw and "***" in raw


def test_key_with_surrounding_whitespace_in_exception_text_is_scrubbed(env):
    env(RuntimeError("bad credential '  secret-key-123 \n' supplied"))
    with pytest.raises(RuntimeError):
        llm.call(HYDROGEN, ["hi"], purpose="t")
    assert "secret-key-123" not in (llm.BUILD / "calls.jsonl").read_text()


def test_key_straddling_the_200_char_truncation_is_scrubbed_first(env):
    env(RuntimeError("x" * 185 + "secret-key-123" + "z" * 40))
    with pytest.raises(RuntimeError):
        llm.call(HYDROGEN, ["hi"], purpose="t")
    raw = (llm.BUILD / "calls.jsonl").read_text()
    # Scrub happens before the 200-char cut: the key becomes '***' and is then truncated away. (Truncating first would
    # leave the leading 's' of the key in the log.)
    assert "secret" not in raw and ("RuntimeError: " + "x" * 185 + "*") in raw


def test_scrub_replaces_padded_key_and_keeps_surrounding_text(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "  secret-key-123 \n")
    assert llm._scrub("token=secret-key-123 end") == "token=*** end"


# ---- llm: price date boundary (UTC) and unknown models -----------------------------------------
def test_promo_prices_hold_through_2026_11_30_23_59_59_utc(monkeypatch):
    freeze_clock(monkeypatch, dt.datetime(2026, 11, 30, 23, 59, 59, tzinfo=UTC))
    assert llm.price(CARBON) == (2.00, 10.00) and llm.price(ARGON) == (2.00, 10.00)


def test_full_prices_start_at_2026_12_01_00_00_00_utc(monkeypatch):
    freeze_clock(monkeypatch, dt.datetime(2026, 12, 1, 0, 0, 0, tzinfo=UTC))
    assert llm.price(CARBON) == (4.00, 20.00) and llm.price(ARGON) == (4.00, 20.00)


def test_price_boundary_is_utc_not_local_time(monkeypatch):
    # 2026-12-01 01:00 at UTC+2 is 2026-11-30 23:00 UTC: still promo.
    freeze_clock(monkeypatch, dt.datetime(2026, 12, 1, 1, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2))))
    assert llm.price(CARBON) == (2.00, 10.00)


def test_live_call_is_billed_at_the_price_for_the_utc_clock(env, monkeypatch):
    fc = env(default=resp(p=1_000_000, o=0, t=0))
    freeze_clock(monkeypatch, dt.datetime(2026, 12, 1, 0, 0, 0, tzinfo=UTC))
    r = llm.call(ARGON, ["hi"], purpose="t")
    assert len(fc.calls) == 1 and r.cost_usd == pytest.approx(4.0)


def test_run_id_day_rolls_over_at_utc_midnight(monkeypatch):
    monkeypatch.delenv("LLM_RUN_ID", raising=False)
    freeze_clock(monkeypatch, dt.datetime(2026, 11, 30, 23, 59, 59, tzinfo=UTC))
    assert llm.run_id() == "day-2026-11-30"
    freeze_clock(monkeypatch, dt.datetime(2026, 12, 1, 0, 0, 0, tzinfo=UTC))
    assert llm.run_id() == "day-2026-12-01"


def test_unknown_model_refused_even_in_replay_mode_with_a_cache_file_present(env, monkeypatch):
    fc = env()
    path = llm.BUILD / "cache" / f"{llm.cache_key('gemini-9-mystery', ['q'], None, None)}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"text": "stale", "usage": {}}))
    monkeypatch.setenv("LLM_CACHE", "replay")
    with pytest.raises(llm.UnknownModel):
        llm.call("gemini-9-mystery", ["q"], purpose="t")
    assert fc.calls == []


@pytest.mark.parametrize("name", ["gemini-4-carbon-preview", "gemini-4-Carbon", "gemini-4-hydrogen ", ""])
def test_lookalike_model_names_are_refused(env, name):
    fc = env(default=resp())
    with pytest.raises(llm.UnknownModel):
        llm.call(name, ["q"], purpose="t")
    assert fc.calls == []


# ---- llm: latent video estimate ----------------------------------------------------------------
def test_file_video_with_duration_is_estimated_at_300_tokens_per_second():
    part = types.Part(file_data=types.FileData(file_uri="https://x/v1beta/files/abc", mime_type="video/mp4"),
                      video_metadata=types.VideoMetadata(end_offset="3600s"))
    assert llm._est_input_tokens([part]) == pytest.approx(3600 * llm.VIDEO_TOKENS_PER_S)


@pytest.mark.xfail(strict=True, reason="BUG (latent; video_claims always passes a duration): an uploaded file part "
                   "with no duration is estimated at a flat 2000 input tokens whatever its length, so an hour of "
                   "video under-reserves about 1.08M tokens. Input: file_data part, no video_metadata")
def test_file_video_without_duration_is_not_estimated_as_2000_tokens():
    part = types.Part(file_data=types.FileData(file_uri="https://x/v1beta/files/abc", mime_type="video/mp4"))
    assert llm._est_input_tokens([part]) >= 3600 * llm.VIDEO_TOKENS_PER_S


# ---- rfi: quote rules --------------------------------------------------------------------------
Q19 = "abcd efgh ijkl mnop"       # 19 chars, 4 words
Q20_3W = "abcdef ghijkl mnopqr"   # 20 chars, 3 words
Q20_4W = "abcd efgh ijkl mnopq"   # 20 chars, 4 words


def test_quote_boundary_fixtures_are_what_they_claim():
    assert (len(Q19), len(Q19.split())) == (19, 4)
    assert (len(Q20_3W), len(Q20_3W.split())) == (20, 3)
    assert (len(Q20_4W), len(Q20_4W.split())) == (20, 4)


def test_quote_of_19_chars_with_4_words_is_rejected():
    assert not rfi.quote_ok(Q19, f"{Q19} and then some more text.")


def test_quote_of_20_chars_with_3_words_is_rejected():
    assert not rfi.quote_ok(Q20_3W, f"{Q20_3W} and then some more text.")


def test_quote_of_20_chars_with_4_words_is_accepted():
    assert rfi.quote_ok(Q20_4W, f"{Q20_4W} and then some more text.")


def test_short_response_threshold_is_40_percent_of_its_length():
    r = "abcd efgh ijkl mnop"  # 19 chars: 40% = 7.6
    assert rfi.quote_ok("abcd efg", r)        # 8 chars
    assert not rfi.quote_ok("abcd ef", r)     # 7 chars


def test_response_of_exactly_20_chars_still_needs_4_words():
    assert not rfi.quote_ok(Q20_3W, Q20_3W)  # whole response, 20 chars, but only 3 words


def test_quote_must_be_an_exact_substring_not_a_fuzzy_match():
    r = "Provide padlock latches and standard hinges."
    assert not rfi.quote_ok("Provide padlock latches and standard hinge.", r)
    assert not rfi.quote_ok("Provide  padlock latches and standard hinges.", r)  # double space


@pytest.mark.parametrize("quote", [" Provide padlock latches and standard hinges.",
                                   "Provide padlock latches and standard hinges. ",
                                   "Provide padlock latches and standard hinges.\n"])
def test_quote_with_outer_whitespace_is_rejected_documented(quote):
    # Documented behaviour: the substring rule is strict, so padded quotes are not accepted (a false "not answered",
    # never a false "answered").
    assert not rfi.quote_ok(quote, "Provide padlock latches and standard hinges.")


def test_quote_with_different_case_is_rejected_documented():
    assert not rfi.quote_ok("provide padlock latches and standard hinges.", "Provide padlock latches and standard hinges.")


def test_smart_quote_in_response_needs_the_same_smart_quote_in_quote():
    r = "YMCA’s contractor to provide and install the play surface and playground equipment."
    assert rfi.quote_ok("YMCA’s contractor to provide and install the play surface", r)
    # Documented behaviour: a straight apostrophe from the model does not match the curly one in the source.
    assert not rfi.quote_ok("YMCA's contractor to provide and install the play surface", r)


def test_straight_quote_in_response_is_not_matched_by_curly_quote_in_quote_documented():
    r = "YMCA's contractor to provide and install the play surface and playground equipment."
    assert not rfi.quote_ok("YMCA’s contractor to provide and install the play surface", r)


# ---- rfi: model output shapes ------------------------------------------------------------------
QA = [
    {"id": "add1-q1", "source": "Addendum 1", "page": 2, "question": "Division 27 specification not included in project manual.",
     "response": "Specification is added in Addendum #01"},
    {"id": "add1-q2", "source": "Addendum 1", "page": 2, "question": "Who is responsible for playground equipment and surface installation?",
     "response": "We will work with the client to select a playground consultant who will specify the equipment and surfacing."},
    {"id": "add1-q6", "source": "Addendum 1", "page": 2,
     "question": "Do the gates need a maglock/strike setup with self-closing hinges or will padlock latches and standard hinges be ok?",
     "response": "Provide padlock latches and standard hinges."},
    {"id": "add2-q11", "source": "Addendum 2", "page": 3, "question": "Who is responsible for playground equipment and surface installation?",
     "response": "YMCA’s contractor to provide and install the play surface and playground equipment (updated from Addendum #01)."},
    {"id": "add2-q14", "source": "Addendum 2", "page": 4, "question": "Is there a generator as part of this scope?",
     "response": "There is not a generator included with this project."},
]
GATE_Q = "Should the playground gates use padlock latches with standard hinges, or maglocks with self-closing hinges?"
CARD = {
    "claim": "Door 105A is shown 2'-8\" wide on A201 but the schedule requires 3'-0\" clear.",
    "evidence": [{"sheet": "add1-p007", "revision": "add1", "bbox": [0, 0, 1, 1], "crop": "x.png"}],
    "spec_refs": ["08 71 00 2.1.A"],
    "checks": [{"by": "code", "verdict": "conflict", "note": "2'-8\" < 3'-0\""}],
}


@pytest.fixture
def isolated_build(tmp_path, monkeypatch):
    """rfi reads sheet_index.json from rfi.BUILD; point it at an empty tmp dir so no real build file is touched."""
    monkeypatch.setattr(rfi, "BUILD", tmp_path)
    return tmp_path


def fake_llm(monkeypatch, answer):
    calls = []

    def call(model, parts, *, schema=None, purpose, thinking=None):
        calls.append(parts)
        a = answer(parts[0]) if callable(answer) else answer
        return llm.Result(text=json.dumps(a), json=a)
    monkeypatch.setattr(rfi.llm, "call", call)
    return calls


@pytest.mark.parametrize("bad", [
    ["answered", "qa_id", "quote"],                                             # list instead of object
    "yes", None, 3,                                                              # scalars
    {"answered": "yes", "qa_id": "add1-q6", "quote": "Provide padlock latches and standard hinges.", "reason": "r"},
    {"answered": 1, "qa_id": "add1-q6", "quote": "Provide padlock latches and standard hinges.", "reason": "r"},
    {"answered": True},                                                          # missing qa_id and quote
    {"answered": True, "qa_id": "add1-q6"},                                      # missing quote
    {"answered": True, "qa_id": None, "quote": "Provide padlock latches and standard hinges.", "reason": "r"},
    {"answered": True, "qa_id": "add1-q6", "quote": None, "reason": "r"},
])
def test_malformed_model_output_is_never_answered(monkeypatch, isolated_build, bad):
    fake_llm(monkeypatch, bad)
    out = rfi.already_answered(GATE_Q, QA)
    assert out["answered"] is False and out["qa_id"] is None


def test_extra_keys_in_model_output_are_ignored(monkeypatch):
    fake_llm(monkeypatch, {"answered": True, "qa_id": "add1-q6", "quote": "Provide padlock latches and standard hinges.",
                           "reason": "r", "confidence": 0.99, "extra": [1, 2]})
    out = rfi.already_answered(GATE_Q, QA)
    assert out["answered"] and out["answered_by"] == "add1 p2 Q6"


@pytest.mark.parametrize("qa_id", ["add1-q6 ", "ADD1-Q6", "add1-q06", "add1-q6\n"])
def test_qa_id_must_match_exactly(monkeypatch, qa_id):
    fake_llm(monkeypatch, {"answered": True, "qa_id": qa_id, "quote": "Provide padlock latches and standard hinges.",
                           "reason": "r"})
    out = rfi.already_answered(GATE_Q, QA)
    assert not out["answered"] and "not a retrieved candidate" in out["reason"]


def test_qa_id_not_among_candidates_is_rejected_even_if_it_exists_in_index(monkeypatch):
    # add2-q14 is in the index but is not retrieved for the gate question.
    fake_llm(monkeypatch, {"answered": True, "qa_id": "add2-q14", "quote": "There is not a generator", "reason": "r"})
    out = rfi.already_answered(GATE_Q, QA)
    assert not out["answered"] and "add2-q14" in out["reason"]


# ---- rfi: retrieval edge inputs ----------------------------------------------------------------
@pytest.mark.parametrize("question", ["", "   \n\t  ", "?!...  --"])
def test_blank_or_punctuation_question_never_reaches_the_model(monkeypatch, isolated_build, question):
    calls = fake_llm(monkeypatch, {"answered": True, "qa_id": "add1-q6", "quote": "Provide padlock latches and standard hinges.", "reason": "r"})
    assert rfi.retrieve(question, QA) == []
    assert not rfi.already_answered(question, QA)["answered"] and calls == []


def test_huge_question_returns_bounded_scores_quickly():
    out = rfi.retrieve("playground " * 20000, QA)
    assert 1 <= len(out) <= rfi.TOP_K
    assert all(0.0 <= s <= 1.0 + 1e-9 for s, _ in out)


def test_unicode_question_does_not_crash_and_matches_nothing(monkeypatch, isolated_build):
    calls = fake_llm(monkeypatch, {"answered": False, "qa_id": "", "quote": "", "reason": ""})
    q = "¿Quién paga el equipo de juegos? 你好 \U0001F642 ’“”"
    assert rfi.already_answered(q, QA)["answered"] is False
    assert calls == []


def test_all_scores_stay_within_zero_and_one_for_mixed_queries():
    for q in ["playground equipment surface installation", "generator", "padlock latches hinges maglock",
              "zzz qqq xxx", "who who who who playground"]:
        for s, _ in rfi.retrieve(q, QA):
            assert 0.0 <= s <= 1.0 + 1e-9, (q, s)


# ---- rfi: supersession -------------------------------------------------------------------------
def test_identical_question_in_add1_and_add2_offers_only_add2():
    ids = [row["id"] for _, row in rfi.retrieve("Who is responsible for playground equipment and surface installation?", QA)]
    assert "add2-q11" in ids and "add1-q2" not in ids


def test_supersession_ignores_case_and_punctuation():
    ids = [row["id"] for _, row in rfi.retrieve("WHO IS RESPONSIBLE FOR PLAYGROUND EQUIPMENT AND SURFACE INSTALLATION", QA)]
    assert "add1-q2" not in ids and "add2-q11" in ids


def test_near_but_different_question_does_not_supersede():
    # add2 asks a different (shorter) question, so add1's identical-question entry is not superseded.
    qa = [QA[1], dict(QA[3], question="Who is responsible for playground equipment?")]
    ids = [row["id"] for _, row in rfi.retrieve("Who is responsible for playground equipment and surface installation?", qa)]
    assert "add1-q2" in ids and "add2-q11" in ids


def test_draft_blocked_by_superseding_addendum_cites_add2(monkeypatch, isolated_build):
    fake_llm(monkeypatch, {"answered": True, "qa_id": "add2-q11",
                           "quote": "YMCA’s contractor to provide and install the play surface", "reason": "r"})
    out = rfi.draft_rfi(dict(CARD, claim="Who is responsible for playground equipment and surface installation?"), QA)
    assert out["status"] == "blocked" and out["answered_by"] == "add2 p3 Q11"


# ---- rfi: draft text ---------------------------------------------------------------------------
SENT_LINE = "DRAFT: not sent. Review and send manually."


@pytest.mark.parametrize("card", [
    CARD,
    dict(CARD, evidence=[]),
    dict(CARD, checks=[], reason="Drawings and specs disagree on the door clear width."),
    dict(CARD, claim="x" * 500),
    dict(CARD, spec_refs=[], evidence=[{"sheet": None}]),
], ids=["default", "no-evidence", "reason-given", "long-claim", "empty-refs"])
def test_draft_text_only_ever_says_not_sent(monkeypatch, isolated_build, card):
    fake_llm(monkeypatch, {"answered": False, "qa_id": "", "quote": "", "reason": ""})
    text = rfi.draft_rfi(card, QA)["text"]
    sent_lines = [ln for ln in text.splitlines() if re.search(r"\bsent\b", ln, re.I)]
    assert sent_lines == [SENT_LINE]
    assert text.splitlines()[-1] == SENT_LINE


def test_blocked_draft_also_carries_the_not_sent_line(monkeypatch, isolated_build):
    fake_llm(monkeypatch, {"answered": True, "qa_id": "add1-q6", "quote": "Provide padlock latches and standard hinges.", "reason": "r"})
    out = rfi.draft_rfi(dict(CARD, claim=GATE_Q), QA)
    assert out["status"] == "blocked" and out["text"].splitlines()[-1] == SENT_LINE
    assert not re.search(r"\b(has been|was|were|been) sent\b", out["text"], re.I)


# ---- rfi: sheet number and revision for real sheet ids -----------------------------------------
REAL_BUILD = ROOT / "build"


def _real_rows():
    p = REAL_BUILD / "sheet_index.json"
    if not p.exists():
        pytest.skip("build/sheet_index.json missing")
    return json.loads(p.read_text())["rows"]


def test_every_current_real_sheet_is_cited_with_number_title_and_revision(monkeypatch):
    rows = [r for r in _real_rows() if r.get("current")]
    assert rows
    monkeypatch.setattr(rfi, "BUILD", REAL_BUILD)  # read-only use of the real index
    for r in rows:
        text = rfi.rfi_text(dict(CARD, evidence=[{"sheet": r["sheet"], "revision": "add1"}]))
        expect = f"Sheet {r['sheet_number']}" + (f" — {r['title']}" if r.get("title") else "") + \
            f", rev {r['revision']}"
        assert expect in text, r["sheet"]


@pytest.mark.xfail(strict=True, reason="BUG: a superseded sheet is cited as if current. Input: evidence sheet "
                   "'dwg-p003' (current=False, superseded_by='add1-p006') gives 'Sheet G002 ..., rev 06/02/2026' "
                   "with no mention of the superseding sheet")
def test_superseded_sheet_is_not_cited_as_current(monkeypatch):
    rows = {r["sheet"]: r for r in _real_rows()}
    assert rows["dwg-p003"]["current"] is False and rows["dwg-p003"]["superseded_by"] == "add1-p006"
    monkeypatch.setattr(rfi, "BUILD", REAL_BUILD)
    text = rfi.rfi_text(dict(CARD, evidence=[{"sheet": "dwg-p003", "revision": "dwg"}]))
    assert "add1-p006" in text or "superseded" in text.lower()


def test_unknown_sheet_id_falls_back_to_the_card_fields(isolated_build):
    text = rfi.rfi_text(dict(CARD, evidence=[{"sheet": "add9-p999", "revision": "add9"}]))
    assert "Sheet add9-p999, rev add9" in text


# ---- qa_index: parsing from real PDFs built in tmp -----------------------------------------------
def make_pdf(path, pages):
    """pages: [(lines, page_width)]. Letter pages are 612 wide; drawing sheets are wider than qa_index.LETTER_MAX_W."""
    doc = pymupdf.open()
    for lines, width in pages:
        page = doc.new_page(width=width, height=792)
        for i, line in enumerate(lines):
            page.insert_text((36, 40 + 12 * i), line, fontsize=9)
    doc.save(str(path))
    doc.close()
    return path


def test_parse_qa_indexes_question_and_response(tmp_path):
    p = make_pdf(tmp_path / "a.pdf", [(["Question: Is a generator required?",
                                        "Response: There is not a generator included."], 612)])
    out = qa_index.parse_qa(p, "add1")
    assert out == [{"id": "add1-q1", "source": "Addendum 1", "page": 1, "question": "Is a generator required?",
                    "response": "There is not a generator included."}]


def test_question_without_response_raises_loudly(tmp_path):
    p = make_pdf(tmp_path / "a.pdf", [(["Question: Lonely question?"], 612)])
    with pytest.raises(ValueError, match="has no Response"):
        qa_index.parse_qa(p, "add1")


def test_page_number_and_bullet_lines_are_stripped(tmp_path):
    p = make_pdf(tmp_path / "a.pdf", [(["3", "•", "Question: Gate hardware?",
                                        "Response: Provide padlock latches and standard hinges."], 612)])
    out = qa_index.parse_qa(p, "add1")
    assert out[0]["question"] == "Gate hardware?"
    assert out[0]["response"] == "Provide padlock latches and standard hinges."


def test_narrative_stops_at_drawings_heading_and_at_wide_pages(tmp_path):
    p = make_pdf(tmp_path / "a.pdf", [
        (["Question: First?", "Response: First answer text here."], 612),
        (["Drawings:", "Question: After drawings?", "Response: Must be ignored."], 612),
        (["Question: Wide sheet?", "Response: Also ignored."], 3024),
    ])
    out = qa_index.parse_qa(p, "add1")
    assert [q["question"] for q in out] == ["First?"]


@pytest.mark.xfail(strict=True, reason="BUG: an upper-case 'QUESTION:' header is not recognised (the header regex is "
                   "case-sensitive), so no error is raised; the Q&A is dropped and its text is absorbed into the "
                   "previous response. Input: page text 'Question: A?', 'Response: first answer text.', "
                   "'QUESTION: B?', 'RESPONSE: second answer.'")
def test_uppercase_question_header_is_not_absorbed_into_previous_response(tmp_path):
    p = make_pdf(tmp_path / "a.pdf", [(["Question: A?", "Response: first answer text.",
                                        "QUESTION: B?", "RESPONSE: second answer."], 612)])
    out = qa_index.parse_qa(p, "add1")
    assert len(out) == 2
    assert "QUESTION" not in out[0]["response"]
