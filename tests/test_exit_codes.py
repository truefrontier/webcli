"""Required (3): the unified exit-code table, in every agent-help and on the real error paths (6 rate_limited with
resets_at, 7 config)."""

from __future__ import annotations

import copy
import json

import pytest
from click.testing import CliRunner

from webcli.apps import llmcli, webcli as webcli_app
from webcli.core import config
from webcli.core.errors import NotFound, ProfileBusy, RateLimited
from webcli.sites.chatgpt import fetch as chatgpt_fetch
from webcli.sites.gemini import fetch as gemini_fetch
from conftest import make_profile

CODES = {"0", "2", "3", "4", "5", "6", "7", "129", "130", "143"}
RESETS = "2026-10-19T13:53:00-05:00"
FETCH = {"gemini": gemini_fetch, "chatgpt": chatgpt_fetch}
ANSWER = {"chatId": None, "chatUrl": None, "prompt": "q", "answer": "4", "imageCount": 0, "model": None,
          "servedModel": None}


def run(app, *args, env=None):
    return CliRunner().invoke(app.build(), list(args), env=env)


def stub(monkeypatch, site, name, result):
    async def fake(*args, **kwargs):
        if isinstance(result, BaseException):
            raise result
        return copy.deepcopy(result)

    monkeypatch.setattr(FETCH[site], name, fake)


@pytest.fixture(autouse=True)
def profiles():
    make_profile("google:default")
    make_profile("chatgpt:default")


@pytest.mark.parametrize(("app", "args"), [(llmcli, ["agent-help"]), (llmcli, ["gemini", "agent-help"]),
                                           (llmcli, ["chatgpt", "agent-help"]), (webcli_app, ["agent-help"])])
def test_every_agent_help_lists_the_whole_table(app, args):
    result = run(app, *args, "--format", "json")
    assert result.exit_code == 0
    ref = json.loads(result.stdout)
    assert set(ref["exitCodes"]) == CODES
    assert "rate limited" in ref["exitCodes"]["6"] and "resets_at" in ref["exitCodes"]["6"]
    assert "model_fallback" in ref["exitCodes"]["6"]
    assert ref["exitCodes"]["7"].startswith("config error")
    assert "hidden_browser_blocked" in ref["exitCodes"]["4"]
    assert ref["errorShapes"]["rate_limited"]["code"] == 6 and "resets_at" in ref["errorShapes"]["rate_limited"]


@pytest.mark.parametrize("site", ["gemini", "chatgpt"])
@pytest.mark.parametrize("command", [("ask", "q"), ("-p", "q")])
def test_a_rate_limit_exits_6_with_resets_at(monkeypatch, site, command):
    stub(monkeypatch, site, "ask", RateLimited("You've reached your limit. It resets at 1:53 PM.", sent=True,
                                               resets_at=RESETS))
    result = run(llmcli, site, *command)
    assert result.exit_code == 6
    error = json.loads(result.stderr)
    assert {k: error[k] for k in ("error", "code", "resets_at")} == {"error": "rate_limited", "code": 6,
                                                                     "resets_at": RESETS}
    assert error["message"].startswith("You've reached your limit")


@pytest.mark.parametrize("site", ["gemini", "chatgpt"])
def test_a_limit_notice_as_the_answer_exits_6_after_printing_the_json(monkeypatch, site):
    stub(monkeypatch, site, "ask", {**ANSWER, "answer": "You've reached your usage limit. Your limit resets at 1:53 PM."})
    result = run(llmcli, site, "ask", "q", "--format", "json")
    assert result.exit_code == 6
    assert json.loads(result.stdout)["error"] == "rate_limited"
    error = json.loads(result.stderr)
    assert error["error"] == "rate_limited" and error["code"] == 6 and "resets_at" in error


def test_a_rate_limit_without_a_known_reset_has_resets_at_null(monkeypatch):
    stub(monkeypatch, "chatgpt", "ask", RateLimited("Too many requests.", sent=False))
    error = json.loads(run(llmcli, "chatgpt", "-p", "q").stderr)
    assert error["resets_at"] is None and error["code"] == 6 and "not sent" in error["hint"]


def test_strict_model_fallback_exits_6_with_both_models(monkeypatch):
    stub(monkeypatch, "chatgpt", "ask", {**ANSWER, "requestedModel": "gpt-6-thinking", "servedModel": "gpt-6-mini",
                                         "fallback": True, "fallbackFrom": "gpt-6-thinking",
                                         "fallbackTo": "gpt-6-mini"})
    result = run(llmcli, "chatgpt", "-p", "-m", "gpt-6-thinking", "--strict-model", "q")
    assert result.exit_code == 6
    error = json.loads(result.stderr.splitlines()[-1])
    assert (error["error"], error["requestedModel"], error["servedModel"]) == \
        ("model_fallback", "gpt-6-thinking", "gpt-6-mini")


@pytest.mark.parametrize(("args", "env", "message"), [
    (["gemini", "--identity", "chatgpt:default", "status"], {}, "a chatgpt identity"),
    (["gemini", "--identity", "Not An Id", "status"], {}, "is not an identity id"),
    (["chatgpt", "status"], {"WEBCLI_IDENTITY": "google:default"}, "a google identity"),
    (["serve", "--identity", "bad id", "--status"], {}, "is not an identity id"),
])
def test_config_errors_exit_7(args, env, message):
    result = run(llmcli, *args, env=env)
    assert result.exit_code == 7
    assert message in json.loads(result.stderr)["error"]


def test_an_invalid_config_toml_exits_7():
    config.ensure_home()
    (config.home() / "config.toml").write_text("[sites.gemini\nidentity = ")
    result = run(llmcli, "gemini", "status")
    assert result.exit_code == 7 and "not valid TOML" in json.loads(result.stderr)["error"]


@pytest.mark.parametrize(("error", "code"), [(NotFound("Chat x not found.", hint="list"), 3),
                                             (ProfileBusy("busy"), 5), (RuntimeError("site changed"), 5)])
def test_errors_map_to_their_codes(monkeypatch, error, code):
    stub(monkeypatch, "gemini", "chat", error)
    assert run(llmcli, "gemini", "chats", "show", "1a2b3c4d5e6f7a8b").exit_code == code


def test_usage_and_sign_in_codes(monkeypatch):
    assert run(llmcli, "gemini", "ask", "q", "--new", "--chat", "1a2b3c4d5e6f7a8b").exit_code == 2
    assert run(llmcli, "chatgpt", "chats", "show", "not-a-uuid").exit_code == 2
    assert run(llmcli, "gemini", "status", env={"LLMCLI_FORMAT": "yaml"}).exit_code == 2
    stub(monkeypatch, "chatgpt", "status", {"signedIn": False, "plan": None, "email": None})
    assert run(llmcli, "chatgpt", "status").exit_code == 4
