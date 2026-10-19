"""webcli aa offline: the ranking logic on the aanalysis fixtures, the HTTP client against a fake urlopen (pagination,
cache, error codes), and the CLI's formats and exits. No test reaches the network."""

from __future__ import annotations

import email.message
import json
import stat
import urllib.error
import urllib.request
from datetime import datetime

import pytest
from click.testing import CliRunner

from webcli.apps import webcli as webcli_app
from webcli.core import config
from webcli.core.errors import ConfigError
from webcli.sites.aa import api, digest
from conftest import FIXTURES

KEY = "aa_live_9f8e7d6c5b4a39281706f5e4d3c2b1a0"
LLM_URL = f"{api.BASE_URL}{api.LLM_ENDPOINT}?page=1"
TTS_URL = f"{api.BASE_URL}{api.TTS_ENDPOINT}"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / "aa" / name).read_text())


def models() -> list[dict]:
    return [digest.normalize(m) for m in fixture("models.json")["data"]]


def names(ranked: list[dict]) -> list[str]:
    return [m["name"] for m in ranked]


class Replies(dict):
    """url -> reply (a dict to send as JSON, or an exception to raise); .requests: every request, in order."""

    requests: list


class Reply:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def http(monkeypatch):
    monkeypatch.setenv(api.KEY_ENV, KEY)
    replies = Replies({LLM_URL: fixture("models.json"), TTS_URL: fixture("tts_models.json")})
    replies.requests = []

    def urlopen(request, timeout):
        replies.requests.append(request)
        reply = replies[request.full_url]
        if isinstance(reply, BaseException):
            raise reply
        return Reply(json.dumps(reply).encode())

    monkeypatch.setattr(api, "_urlopen", urlopen)
    return replies


def run(*args, input=None):
    return CliRunner().invoke(webcli_app.build(), ["aa", *args], input=input)


def leaked(result) -> bool:
    """Whether the key shows up in the command's stdout, stderr, or logs."""
    logs = "".join(p.read_text() for p in (config.home() / "logs").glob("*.log"))
    return KEY in result.stdout + result.stderr + logs


# The rankings (aanalysis's semantics, on its fixture: 5 models, every one with a cost per task and a speed).

def test_rank_by_intelligence_and_coding():
    assert names(digest.rank_by_intelligence(models())) == [
        "Claude 3.5 Sonnet", "GPT-4o", "Llama 3.1 405B", "Qwen 2.5 72B", "DeepSeek Coder V2"]
    # Qwen has no coding index and no code-ish name, so it is left out
    assert names(digest.rank_by_coding(models())) == ["Claude 3.5 Sonnet", "GPT-4o", "DeepSeek Coder V2",
                                                      "Llama 3.1 405B"]
    assert names(digest.rank_by_intelligence(models(), 2)) == ["Claude 3.5 Sonnet", "GPT-4o"]


def test_smart_fast_keeps_the_upper_half_and_marks_the_pareto_frontier():
    ranked = digest.rank_by_smart_fast(models())
    assert names(ranked) == ["Claude 3.5 Sonnet", "GPT-4o", "Llama 3.1 405B"]  # median intelligence 78.5
    assert [m["_pareto_intel_speed"] for m in ranked] == [True, False, False]  # Claude is smarter and faster


def test_smart_cheap_ranks_intelligence_per_dollar_with_a_blended_fallback():
    ranked = digest.rank_by_smart_cheap(models())
    assert names(ranked) == ["Llama 3.1 405B", "Claude 3.5 Sonnet", "GPT-4o"]
    assert ranked[0]["_cost_efficiency"] == pytest.approx(78.5 / 0.008) and ranked[0]["_cost_basis"] == "task"
    no_task = {"name": "X", "evaluations": {"artificial_analysis_intelligence_index": 50.0},
               "pricing": {"price_1m_input_tokens": 1.0, "price_1m_output_tokens": 3.0}}
    [only] = digest.rank_by_smart_cheap([no_task])
    assert (only["_cost_basis"], only["_cost_efficiency"]) == ("blended", 50.0 / 2.5)


def test_open_digest_and_picks():
    result = digest.open_digest(models())
    assert names(result["open_models"]) == ["Llama 3.1 405B", "Qwen 2.5 72B", "DeepSeek Coder V2"]
    assert names(result["proprietary_comparison"]) == ["Claude 3.5 Sonnet", "GPT-4o"]
    assert result["intelligence_gap"] == pytest.approx(87.1 - 78.5)
    picks = {c: digest.pick(models(), c) for c in digest.PICK_CATEGORIES}
    assert {c: p["recommendation"]["name"] for c, p in picks.items()} == {
        "local": "Llama 3.1 405B", "cheap-api": "Llama 3.1 405B", "frontier": "Claude 3.5 Sonnet",
        "coding-agent": "Claude 3.5 Sonnet"}
    assert names(picks["frontier"]["runners_up"]) == ["GPT-4o", "Llama 3.1 405B", "Qwen 2.5 72B",
                                                      "DeepSeek Coder V2"]


def test_open_weight_guess():
    assert digest.is_open_weight({"name": "gpt-oss-120b", "model_creator_name": "OpenAI"}) is True
    assert digest.is_open_weight({"name": "Gemini-Pro 2", "model_creator_name": "Google"}) is False
    assert digest.is_open_weight({"name": "Gemma 3", "model_creator_name": "Google"}) is True
    assert digest.is_open_weight({"name": None, "model_creator_name": "Anthropic"}) is False


def test_tts_rankings():
    tts = [digest.normalize(m) for m in fixture("tts_models.json")["data"]]
    assert names(digest.rank_by_elo(tts, 3)) == ["ElevenLabs v4", "Sonic 3.6", "Gemini 3.8 Flash TTS"]
    assert names(digest.filter_tts(tts, creator="goo")) == ["Gemini 3.8 Flash TTS"]
    assert digest.rank_tts_smart_fast(tts) == [] and digest.rank_tts_smart_cheap(tts) == []  # free tier: no speed
    fast = [{"name": n, "elo": e, "chars_per_second": c} for n, e, c in
            [("a", 1300, 100), ("b", 1290, 1000), ("c", 1280, 500), ("d", 1100, 2000), ("e", 1050, 3000)]]
    assert names(digest.rank_tts_smart_fast(fast)) == ["b", "a", "c"]  # median elo 1280 drops d and e
    priced = [{"name": n, "elo": e, "price_per_1m_characters": p} for n, e, p in [("a", 1300, 30), ("b", 1280, 10)]]
    assert names(digest.rank_tts_smart_cheap(priced)) == ["a"]  # median elo 1300 leaves only a


def test_model_rows_are_aanalysis_shaped():
    [row] = digest.model_rows(digest.rank_by_intelligence(models(), 1))
    assert row == {"rank": 1, "id": "2", "slug": "claude-3-5-sonnet", "name": "Claude 3.5 Sonnet",
                   "creator": "Anthropic", "intelligence_index": 87.1, "coding_index": 90.2, "agentic_index": None,
                   "tokens_per_second": 92.0, "time_to_first_token_s": 0.28,
                   "input_price_per_1m": 3.0, "output_price_per_1m": 15.0, "cost_per_intel_task": 0.012,
                   "open_guess": None, "pareto_intel_speed": None}


# The HTTP client.

def test_get_sends_the_key_in_a_header_only_and_caches(http):
    assert len(api.llm_models()) == 5
    [request] = http.requests
    assert request.full_url == LLM_URL and KEY not in request.full_url
    assert request.get_header("X-api-key") == KEY and request.get_header("User-agent").startswith("webcli/")
    api.llm_models()
    assert len(http.requests) == 1  # the 6h cache
    api.llm_models(refresh=True)
    assert len(http.requests) == 2
    cache = config.home() / "sites" / "aa" / "cache" / "language_models_free_page-1.json"
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600 and KEY not in cache.read_text()


def test_pagination_follows_has_more(http):
    first = fixture("models.json")
    first["meta"]["pagination"]["has_more"] = True
    http[LLM_URL] = first
    http[f"{api.BASE_URL}{api.LLM_ENDPOINT}?page=2"] = {"pagination": {"has_next": False},
                                                        "data": [{"id": "6", "name": "Extra"}]}
    assert [m["name"] for m in api.llm_models()][-1] == "Extra"
    assert len(http.requests) == 2


def test_the_key_comes_from_the_env_then_the_saved_file(monkeypatch):
    monkeypatch.delenv(api.KEY_ENV)
    with pytest.raises(ConfigError):
        api.api_key()
    path = api.save_key(f"  {KEY}\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and api.api_key() == KEY
    monkeypatch.setenv(api.KEY_ENV, "from-env")
    assert api.api_key() == "from-env"


def test_a_key_that_cant_be_a_header_is_a_config_error_that_never_shows_it(monkeypatch, http):
    monkeypatch.setenv(api.KEY_ENV, "aa_short\nX-Evil: 1")
    result = run("models")
    assert result.exit_code == 7 and "aa_short" not in result.stdout + result.stderr and http.requests == []
    monkeypatch.delenv(api.KEY_ENV)
    assert run("config", "set-key", input="two words\n").exit_code == 7


def test_redirects_keep_the_key_on_the_same_https_host_only():
    request = urllib.request.Request(LLM_URL, headers={"x-api-key": KEY})
    follow = api._SameHostRedirects().redirect_request
    moved = follow(request, None, 301, "Moved", {}, f"{api.BASE_URL}/language/models/free/?page=1")
    assert moved.get_header("X-api-key") == KEY
    for elsewhere in ("https://evil.example/collect", "http://artificialanalysis.ai/api/v2/language/models/free"):
        assert follow(request, None, 302, "Found", {}, elsewhere) is None


def test_resets_at_reads_retry_after():
    now = datetime(2026, 10, 19, 12, 0, tzinfo=api.RESET_TZ)
    assert api.resets_at("90", now) == "2026-10-19T12:01:30-05:00"
    assert api.resets_at("Wed, 21 Oct 2026 07:28:00 GMT") == "2026-10-21T02:28:00-05:00"
    assert api.resets_at(None) is None and api.resets_at("soon") is None


# The CLI.

def test_help_lists_every_verb():
    result = run("--help")
    assert result.exit_code == 0
    for verb in ("models", "digest", "tts", "config", "agent-help"):
        assert verb in result.stdout
    ref = json.loads(run("agent-help", "--format", "json").stdout)
    assert {c["name"] for c in ref["commands"]} == {
        "models", "digest smartest", "digest smart-fast", "digest smart-cheap", "digest open", "digest coding",
        "digest pick", "digest all", "tts models", "tts digest smartest", "tts digest smart-fast",
        "tts digest smart-cheap", "tts digest all", "config set-key", "agent-help"}


def test_models_json_csv_and_md():
    result = run("models", "--limit", "2", "--format", "json")
    assert result.exit_code == 0
    assert [(r["rank"], r["name"]) for r in json.loads(result.stdout)] == [(1, "Claude 3.5 Sonnet"), (2, "GPT-4o")]
    assert run("models", "--creator", "meta", "--select", "name,creator").stdout.strip() == \
        '[{"name":"Llama 3.1 405B","creator":"Meta"}]'
    assert run("models", "--min-intel", "86", "--csv", "--select", "name").stdout == "name\nClaude 3.5 Sonnet\n"
    md = run("digest", "smart-fast", "--format", "md", "--compact").stdout.splitlines()
    assert md[0] == "| rank | name | creator | intelligence_index |" and len(md) == 5


def test_digest_verbs():
    assert [r["name"] for r in json.loads(run("digest", "coding", "--limit", "1").stdout)] == ["Claude 3.5 Sonnet"]
    opened = json.loads(run("digest", "open").stdout)
    assert [r["open_guess"] for r in opened] == [True, True, True]
    pick = json.loads(run("digest", "pick", "local").stdout)
    assert pick["category"] == "local" and pick["recommendation"]["model_creator"]["name"] == "Meta"
    assert run("digest", "pick", "nonsense").exit_code == 2
    everything = json.loads(run("digest", "all", "--limit", "2").stdout)
    assert {k: len(v) for k, v in everything.items()} == {"smartest": 2, "smart_fast": 2, "smart_cheap": 2,
                                                          "coding": 2, "open": 2, "picks": 4}
    assert set(everything["picks"]) == {"local", "cheap_api", "frontier", "coding_agent"}


def test_tts_verbs_and_the_free_tier_exit_3():
    assert [r["name"] for r in json.loads(run("tts", "models", "--limit", "2").stdout)] == ["ElevenLabs v4",
                                                                                            "Sonic 3.6"]
    assert json.loads(run("tts", "digest", "smartest", "--limit", "1").stdout)[0]["elo"] == 1315.2
    for kind in ("smart-fast", "smart-cheap"):
        result = run("tts", "digest", kind)
        assert result.exit_code == 3 and json.loads(result.stderr)["code"] == 3
    assert json.loads(run("tts", "digest", "all").stdout)["smart_fast"] == []


def test_no_key_exits_7_with_a_hint(monkeypatch, http):
    monkeypatch.delenv(api.KEY_ENV)
    result = run("models")
    error = json.loads(result.stderr)
    assert result.exit_code == 7 and error["code"] == 7 and "config set-key" in error["hint"]
    assert http.requests == []


@pytest.mark.parametrize("exc, code", [
    (urllib.error.HTTPError(LLM_URL, 401, "Unauthorized", email.message.Message(), None), 7),
    (urllib.error.HTTPError(LLM_URL, 403, "Forbidden", email.message.Message(), None), 7),
    (urllib.error.HTTPError(LLM_URL, 500, "Server Error", email.message.Message(), None), 5),
    (urllib.error.URLError(OSError("nodename nor servname provided")), 5),
    (TimeoutError("timed out"), 5),
])
def test_api_errors_exit_with_their_code_and_never_show_the_key(http, exc, code):
    http[LLM_URL] = exc
    result = run("models")
    assert result.exit_code == code and json.loads(result.stderr)["code"] == code
    assert not leaked(result)


def test_429_exits_6_rate_limited(http):
    headers = email.message.Message()
    headers["Retry-After"] = "120"
    http[LLM_URL] = urllib.error.HTTPError(LLM_URL, 429, "Too Many Requests", headers, None)
    result = run("digest", "smartest")
    error = json.loads(result.stderr)
    assert result.exit_code == 6 and (error["error"], error["code"]) == ("rate_limited", 6)
    assert error["resets_at"].startswith("20") and not leaked(result)


def test_non_json_reply_exits_5(monkeypatch):
    monkeypatch.setattr(api, "_urlopen", lambda request, timeout: Reply(b"<html>"))
    assert run("models").exit_code == 5


def test_config_set_key_saves_0600_and_never_prints_it(monkeypatch, http):
    monkeypatch.delenv(api.KEY_ENV)
    result = run("config", "set-key", input=KEY + "\n")
    path = config.home() / "sites" / "aa" / "api_key"
    assert result.exit_code == 0 and json.loads(result.stdout) == {"saved": str(path)}
    assert path.read_text() == KEY + "\n" and stat.S_IMODE(path.stat().st_mode) == 0o600 and not leaked(result)
    assert run("models", "--limit", "1").exit_code == 0
    assert http.requests[-1].get_header("X-api-key") == KEY
    assert run("config", "set-key", input="  \n").exit_code == 7


def test_config_set_key_from_a_file(tmp_path, monkeypatch):
    monkeypatch.delenv(api.KEY_ENV)
    key_file = tmp_path / "key"
    key_file.write_text(KEY)
    assert run("config", "set-key", "--key-file", str(key_file)).exit_code == 0
    assert api.api_key() == KEY
    assert run("config", "set-key", "--key-file", str(tmp_path / "missing")).exit_code == 2
