"""Offline CLI tests (from musecli): flags, formats, exit codes, JSON errors, the uniform ask JSON, stdin prompts,
identity resolution, agent-help. fetch is stubbed."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import signal

import pytest
from click.testing import CliRunner

from webcli.apps import llmcli
from webcli.core import browser, config
from webcli.core.errors import AuthRequired, ProfileBusy, RateLimited
from webcli.core.identities import Identity
from webcli.sites.muse import cli

UUID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
CHATS = [{"id": "main", "title": "Main chat", "updated": None, "url": "https://muse.ai/"},
         {"id": UUID, "title": "Trip to Lisbon", "updated": "2m", "url": f"https://muse.ai/thread/{UUID}"}]
ANSWER = {"chatId": UUID, "chatUrl": f"https://muse.ai/thread/{UUID}", "prompt": "Plan Lisbon",
          "answer": "Here is the plan.", "citations": [{"title": "Visit Lisboa", "url": "https://www.visitlisboa.com/en"}]}
UNIFORM = {"answer", "chatId", "requestedModel", "servedModel", "servedModelDisplay", "fallback", "images",
           "citations", "canvas"}
# What ask/-p print: fetch's result plus the uniform fields Muse lacks (no models, files, images, or canvas).
OUT = {**ANSWER, "requestedModel": None, "servedModel": None, "servedModelDisplay": None, "fallback": False,
       "images": [], "canvas": None}
CAP = "You've reached your daily message limit. Your limit resets at 4:00 PM."
HINT = "a person runs: llmcli muse login (or webcli identity login meta:default)"


def run(*args, env=None, input=None):
    return CliRunner().invoke(llmcli.build(), ["muse", *args], env=env, input=input)


def last_chat():
    return config.home() / "sites" / "muse" / "meta-default" / "last_chat"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.setattr(browser, "lock_wait", 30.0)
    monkeypatch.setattr(Identity, "profile_exists", lambda self: True)


def stub(monkeypatch, name, result):
    calls = []

    async def fake(*args, **kwargs):
        calls.append((args, kwargs))
        if isinstance(result, BaseException):
            raise result
        return copy.deepcopy(result)

    monkeypatch.setattr(cli.fetch, name, fake)
    return calls


def test_ask_json_when_piped_has_every_uniform_field(monkeypatch):
    calls = stub(monkeypatch, "ask", ANSWER)
    result = run("ask", "  Plan Lisbon  ")
    assert result.exit_code == 0
    out = json.loads(result.stdout)
    assert out == OUT and UNIFORM <= set(out)
    assert result.stderr == ""
    assert calls == [(("Plan Lisbon",), {"chat_id": None, "timeout": 300, "headed": False})]
    assert last_chat().read_text() == UUID + "\n"


def test_print_json_has_the_same_uniform_shape_as_the_other_sites(monkeypatch):
    from webcli.sites.chatgpt import cli as chatgpt_cli

    stub(monkeypatch, "ask", ANSWER)
    out = json.loads(run("-p", "--output-format", "json", "Plan Lisbon").stdout)
    assert out == OUT
    chatgpt = {"chatId": None, "chatUrl": None, "requestedModel": None, "servedModel": "gpt-6",
               "servedModelDisplay": "GPT-6", "fallback": False, "fallbackFrom": None, "fallbackTo": None,
               "prompt": "q", "answer": "a", "imageCount": 0, "attachments": [], "citations": []}

    async def chatgpt_ask(*args, **kwargs):
        return copy.deepcopy(chatgpt)

    monkeypatch.setattr(chatgpt_cli.fetch, "ask", chatgpt_ask)
    other = json.loads(CliRunner().invoke(llmcli.build(), ["chatgpt", "-p", "--output-format", "json", "q"]).stdout)
    for key in UNIFORM:  # same fields, same JSON types
        assert type(out[key]) is type(other[key]) or None in (out[key], other[key]), key


def test_ask_text_prints_the_answer_and_a_note(monkeypatch):
    stub(monkeypatch, "ask", ANSWER)
    assert run("ask", "Plan Lisbon", "--format", "text").stdout == "Here is the plan.\n"
    assert run("ask", "Plan Lisbon", env={"LLMCLI_FORMAT": "text"}).stdout == "Here is the plan.\n"


def test_ask_reads_stdin_and_passes_options(monkeypatch):
    calls = stub(monkeypatch, "ask", ANSWER)
    result = run("ask", "-", "--chat", f"https://muse.ai/thread/{UUID}", "--timeout", "60", "--headed",
                 input="line one\nline two\n")
    assert result.exit_code == 0
    assert calls[0] == (("line one\nline two",), {"chat_id": UUID, "timeout": 60, "headed": True})
    assert run("ask", "hi", "--chat", "main").exit_code == 0
    assert calls[-1][1]["chat_id"] == "main"


@pytest.mark.parametrize("args", [
    ("ask", "hi", "--new", "--chat", UUID),
    ("ask", "   "),
    ("ask", "-"),  # empty stdin
    ("ask", "hi", "--model", " "),
    ("ask", "hi", "--model", "Muse Spark"),  # muse.ai has no model picker
    ("ask", "hi", "--chat", "../api/logout"),
    ("ask", "hi", "--chat", "new"),
    ("ask", "hi", "--timeout", "5"),
    ("chats", "show", "https://muse.ai/library"),
    ("chats", "list", "--limit", "0"),
    ("-p", "--chat", UUID, "-c", "q"),
    ("-p", ""),
    ("nope",),
])
def test_usage_errors_are_json_exit_2_before_any_browser_work(monkeypatch, args):
    calls = [stub(monkeypatch, name, {}) for name in ("ask", "chat", "chats")]
    result = run(*args, input="")
    assert result.exit_code == 2
    assert json.loads(result.stderr)["code"] == 2
    assert calls == [[], [], []]


def test_ask_model_says_why():
    result = run("ask", "hi", "--model", "Muse Spark")
    assert json.loads(result.stderr) == {
        "error": "--model isn't supported: muse.ai has no model picker; Muse chooses its model. Drop --model.",
        "code": 2}


def test_print_mode_stdout_is_only_the_answer(monkeypatch):
    calls = stub(monkeypatch, "ask", ANSWER)
    for args in (("-p", "Plan", "Lisbon"), ("--print", "Plan Lisbon"), ("--lock-wait", "7", "-p", "Plan Lisbon")):
        result = run(*args, env={"LLMCLI_FORMAT": "json"})  # the env format doesn't apply to -p
        assert (result.exit_code, result.stdout, result.stderr) == (0, "Here is the plan.\n", f"chat: {UUID}\n")
    assert calls[0] == (("Plan Lisbon",), {"chat_id": None, "timeout": 300, "headed": False})
    assert browser.lock_wait == 7.0
    quiet = run("-p", "-q", "--timeout", "60", "Plan Lisbon")
    assert (quiet.stdout, quiet.stderr) == ("Here is the plan.\n", "")
    assert calls[-1][1]["timeout"] == 60


def test_print_mode_reads_stdin_prompt_file_and_writes_output_file(monkeypatch, tmp_path):
    calls = stub(monkeypatch, "ask", ANSWER)
    assert run("-p", input="line one\nline two\n").exit_code == 0
    assert calls[-1][0] == ("line one\nline two",)
    prompt, out = tmp_path / "prompt.txt", tmp_path / "answer.txt"
    prompt.write_text("Plan Lisbon\n")
    result = run("-p", "-q", "--prompt-file", str(prompt), "--output-file", str(out))
    assert (result.exit_code, result.stdout) == (0, "") and out.read_text() == "Here is the plan.\n"
    assert run("-p", "--output-file", str(tmp_path / "missing" / "a.txt"), "q").exit_code == 2


def test_continue_sends_into_the_last_finished_chat(monkeypatch):
    calls = stub(monkeypatch, "ask", ANSWER)
    assert run("-p", "-c", "again").exit_code == 2  # nothing finished yet
    run("-p", "Plan Lisbon")
    assert run("-p", "-c", "again").exit_code == 0
    assert calls[-1][1]["chat_id"] == UUID
    stub(monkeypatch, "ask", {**ANSWER, "chatId": None, "chatUrl": None})
    run("-p", "new chat that never got its id")
    assert not last_chat().exists()  # -c can't fall back to an older chat


def test_print_mode_failures_keep_stdout_empty(monkeypatch):
    stub(monkeypatch, "ask", AuthRequired("Not signed in to Muse."))
    result = run("-p", "hi")
    assert (result.exit_code, result.stdout) == (4, "")
    assert json.loads(result.stderr)["hint"] == HINT
    stub(monkeypatch, "ask", TimeoutError("No finished answer after 300s."))
    result = run("-p", "hi")
    assert (result.exit_code, result.stdout) == (5, "")


@pytest.mark.parametrize("command", [("ask", "--format", "json", "q"), ("-p", "--output-format", "json", "q")])
def test_a_cap_notice_as_the_answer_exits_6_with_resets_at(monkeypatch, command):
    stub(monkeypatch, "ask", {**ANSWER, "answer": CAP})
    result = run(*command)
    assert result.exit_code == 6
    out = json.loads(result.stdout)  # the JSON result still prints first
    assert (out["error"], out["code"], out["message"]) == ("rate_limited", 6, CAP)
    error = json.loads(result.stderr)
    assert (error["error"], error["code"], error["message"]) == ("rate_limited", 6, CAP)
    assert error["resets_at"] == out["resets_at"] and error["resets_at"].endswith(("T16:00:00-05:00", "T16:00:00-06:00"))
    assert f"The prompt reached Muse (chat {UUID})." in error["hint"]


def test_a_cap_notice_is_never_printed_as_the_answer(monkeypatch):
    stub(monkeypatch, "ask", {**ANSWER, "answer": "Too many requests."})
    result = run("-p", "q")
    assert (result.exit_code, result.stdout) == (6, "")
    assert json.loads(result.stderr)["resets_at"] is None
    stub(monkeypatch, "ask", {**ANSWER, "answer": "Rate limits keep servers healthy. " * 3})
    assert run("-p", "q").exit_code == 0  # an answer about limits is an answer


def test_a_rate_limit_raised_by_fetch_exits_6(monkeypatch):
    stub(monkeypatch, "ask", RateLimited("Too many requests.", sent=False, resets_at="2026-10-19T15:00:00-05:00"))
    result = run("-p", "q")
    assert result.exit_code == 6
    error = json.loads(result.stderr)
    assert (error["error"], error["resets_at"]) == ("rate_limited", "2026-10-19T15:00:00-05:00")
    assert "The prompt was not sent." in error["hint"]


def test_chats_list_formats(monkeypatch):
    calls = stub(monkeypatch, "chats", CHATS)
    assert json.loads(run("chats", "list", "--limit", "5").stdout) == CHATS
    assert calls[0] == ((5,), {"headed": False})
    assert run("chats", "list").exit_code == 0 and calls[-1] == ((10,), {"headed": False})
    assert json.loads(run("chats", "list", "--compact").stdout) == [{"id": "main", "title": "Main chat"},
                                                                   {"id": UUID, "title": "Trip to Lisbon"}]
    assert json.loads(run("chats", "list", "--select", "id").stdout) == [{"id": "main"}, {"id": UUID}]
    assert run("chats", "list", "--csv").stdout.splitlines() == [
        "id,title,updated,url", "main,Main chat,,https://muse.ai/", f"{UUID},Trip to Lisbon,2m,https://muse.ai/thread/{UUID}"]
    table = run("chats", "list", env={"LLMCLI_FORMAT": "table"})
    assert "Muse chats" in table.stdout and "Trip to Lisbon" in table.stdout and "2m" in table.stdout


def test_chats_show_text_and_json(monkeypatch):
    chat = {"id": UUID, "url": f"https://muse.ai/thread/{UUID}",
            "messages": [{"role": "user", "text": "Plan Lisbon"}, {"role": "assistant", "text": "Here is the plan."}]}
    calls = stub(monkeypatch, "chat", chat)
    assert run("chats", "show", f"muse.ai/thread/{UUID}", "--format", "text").stdout == (
        "## You\n\nPlan Lisbon\n\n## Muse\n\nHere is the plan.\n\n")
    assert calls[0] == ((UUID,), {"headed": False})
    assert json.loads(run("chats", "show", UUID).stdout) == chat


def test_models_is_empty_and_says_why_without_a_browser(monkeypatch, fake_playwright):
    monkeypatch.setattr(Identity, "profile_exists", lambda self: False)  # no session needed: nothing to open
    result = run("models")
    assert (result.exit_code, json.loads(result.stdout)) == (0, [])
    assert result.stderr == "muse.ai has no model picker; Muse chooses its model.\n"
    assert json.loads(run("models", "--compact", "--select", "name").stdout) == []
    quiet = run("models", "-q")
    assert (quiet.stdout, quiet.stderr) == ("[]\n", "")
    human = run("models", env={"LLMCLI_FORMAT": "table"})
    assert (human.exit_code, human.stdout) == (0, "")
    assert "no model picker" in human.stderr
    assert run("models", "--csv").stdout == ""
    assert fake_playwright == []


@pytest.mark.parametrize(("error", "code"), [(AuthRequired("Not signed in to Muse."), 4),
                                             (cli.fetch.NotFound("No chats found.", hint="The guard blocked: x."), 3),
                                             (ProfileBusy("busy"), 5), (TimeoutError("No finished answer."), 5),
                                             (RuntimeError("Muse is asking for approval in chat x."), 5)])
def test_errors_map_to_exit_codes(monkeypatch, error, code):
    stub(monkeypatch, "chats", error)
    result = run("chats", "list")
    assert result.exit_code == code and json.loads(result.stderr)["code"] == code


def test_errors_never_print_a_token(monkeypatch):
    stub(monkeypatch, "chat", RuntimeError("failed: fb_dtsg=NAfakeDtsg0123456789:42:1791990000&lsd=AVfakeLsd0123456789 "
                                           "x-hatch-gateway-token: EAAfakeAccessToken0123456789abcdefFAKE"))
    result = run("chats", "show", UUID)
    assert result.exit_code == 5
    for secret in ("NAfakeDtsg0123456789", "AVfakeLsd0123456789", "EAAfakeAccessToken0123456789abcdefFAKE"):
        assert secret not in result.stderr


def test_no_session_means_exit_4_before_any_browser_work(monkeypatch):
    monkeypatch.setattr(Identity, "profile_exists", lambda self: False)
    calls = stub(monkeypatch, "ask", ANSWER)
    for command in (("ask", "hi"), ("-p", "hi"), ("chats", "list"), ("chats", "show", UUID)):
        result = run(*command)
        assert (result.exit_code, json.loads(result.stderr)["hint"]) == (4, HINT), command
    assert calls == []


def test_status(monkeypatch):
    info = {"signedIn": True, "account": "Test Owner", "access": "ok"}
    calls = stub(monkeypatch, "status", info)
    result = run("status")
    assert (result.exit_code, json.loads(result.stdout)) == (0, {"identity": "meta:default", **info})
    assert calls == [((), {"headed": False})]
    stub(monkeypatch, "status", {**info, "access": "waitlist"})
    result = run("status")
    assert result.exit_code == 4 and "waitlist" in json.loads(result.stderr)["error"]
    stub(monkeypatch, "status", {"signedIn": False, "account": None, "access": None})
    result = run("status")
    assert result.exit_code == 4 and json.loads(result.stderr) == {"error": "Not signed in to Muse.", "code": 4,
                                                                   "hint": HINT}


def test_status_exits_4_when_signed_out_without_starting_chrome(monkeypatch, fake_playwright):
    monkeypatch.setattr(Identity, "profile_exists", lambda self: False)
    result = run("status")
    assert result.exit_code == 4
    assert json.loads(result.stdout) == {"identity": "meta:default", "signedIn": False, "account": None,
                                         "access": None}
    assert fake_playwright == []


def test_the_identity_is_meta_default_unless_overridden(monkeypatch):
    calls = stub(monkeypatch, "status", {"signedIn": True, "account": None, "access": "ok"})
    assert json.loads(run("status").stdout)["identity"] == "meta:default"
    assert browser.identity.profile == config.home() / "identities" / "meta-default" / "chrome-profile"
    assert json.loads(run("--identity", "meta:work", "status").stdout)["identity"] == "meta:work"
    assert json.loads(run("status", env={"WEBCLI_IDENTITY": "meta:env"}).stdout)["identity"] == "meta:env"
    result = run("--identity", "google:default", "status")  # another provider's identity: config error
    assert result.exit_code == 7 and "signs in with meta" in json.loads(result.stderr)["error"]
    assert len(calls) == 3


def test_lock_wait_flag_and_env(monkeypatch):
    stub(monkeypatch, "chats", CHATS)
    assert run("--lock-wait", "7", "chats", "list").exit_code == 0
    assert browser.lock_wait == 7.0
    assert run("chats", "list", env={"WEBCLI_LOCK_WAIT": "2.5"}).exit_code == 0
    assert browser.lock_wait == 2.5
    assert run("--lock-wait", "-1", "chats", "list").exit_code == 2


def test_sigterm_mid_command_exits_143_with_json_error(monkeypatch):
    async def killed(*args, **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(5)

    monkeypatch.setattr(cli.fetch, "ask", killed)
    result = run("ask", "hi")
    assert result.exit_code == 143
    assert json.loads(result.stderr)["code"] == 143


def test_agent_help_lists_commands_flags_exit_codes_and_notes():
    ref = json.loads(run("agent-help", "--format", "json").stdout)
    by_name = {c["name"]: c for c in ref["commands"]}
    assert sorted(by_name) == ["-p", "agent-help", "ask", "chats list", "chats show", "login", "models", "serve",
                               "status"]
    assert by_name["ask"]["usage"] == "llmcli muse ask [OPTIONS] PROMPT"
    flags = {f for c in ref["commands"] for o in c["options"] for f in o["flags"]}
    assert {"--format", "--select", "--compact", "--csv", "--quiet", "-q", "--limit", "--new", "--chat",
            "--timeout", "--headed", "-c", "--continue", "--prompt-file", "--output-file", "--output-format"} <= flags
    assert "--model" not in flags  # hidden: it only explains that there is no model picker
    assert next(o for o in by_name["chats list"]["options"] if "--limit" in o["flags"])["default"] == 10
    assert next(o for o in by_name["ask"]["options"] if "--timeout" in o["flags"])["default"] == 300
    assert set(ref["exitCodes"]) >= {"0", "2", "3", "4", "5", "6", "7"}
    assert "WEBCLI_DAEMON" in ref["env"]
    assert any("meta:default" in n and "Meta account" in n for n in ref["notes"])
    assert any("metaaivm.com" in n for n in ref["notes"])
    assert any("never clicks an approval" in w for w in ref["writes"])


def test_the_umbrella_lists_muse():
    ref = json.loads(CliRunner().invoke(llmcli.build(), ["agent-help", "--format", "json"]).stdout)
    assert any(c["name"].startswith("muse ") for c in ref["commands"])
    assert any("muse" in n and "meta:default" in n for n in ref["notes"])
