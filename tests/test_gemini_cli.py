"""Offline CLI tests: flags, formats, exit codes, JSON errors, stdin, agent-help. The browser layer is stubbed."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import signal
import zipfile

import pytest
from click.testing import CliRunner

from webcli.apps import llmcli
from webcli.core import browser, config
from webcli.core import output as agent_ux
from webcli.core.errors import AuthRequired, ProfileBusy
from webcli.core.identities import Identity
from webcli.sites import llm
from webcli.sites.gemini import cli

CHATS = [
    {"id": "1a2b3c4d5e6f7a8b", "title": "Trip ideas for Kyoto", "updated": "2025-10-14T14:00:00Z", "pinned": True,
     "url": "https://gemini.google.com/app/1a2b3c4d5e6f7a8b"},
    {"id": "0f1e2d3c4b5a6978", "title": "Fix flaky pytest", "updated": "2025-10-14T11:06:40Z", "pinned": False,
     "url": "https://gemini.google.com/app/0f1e2d3c4b5a6978"},
]
ANSWER = {"chatId": "7e6d5c4b3a291807", "chatUrl": "https://gemini.google.com/app/7e6d5c4b3a291807", "model": "Flash",
          "thinking": None, "prompt": "What is 2+2?", "answer": "Two plus two is **4**.", "imageCount": 0}
# what ask/-p print: fetch's result plus the saved images and the uniform llm fields
OUT = {**ANSWER, "images": [], "requestedModel": None, "servedModel": "Flash", "servedModelDisplay": "Flash",
       "fallback": False, "citations": [], "canvas": None}
ERROR_ANSWER = {**ANSWER, "answer": "I encountered an error doing what you asked. Could you try again?"}
ERROR_REPLY = "Gemini replied with an error instead of an answer: " + ERROR_ANSWER["answer"]
MODELS = [{"name": "Fast", "description": "Answers quickly", "current": True},
          {"name": "Thinking", "description": "Solves complex problems", "current": False}]


def run(*args, env=None, input=None):
    return CliRunner().invoke(llmcli.build(), ["gemini", *args], env=env, input=input)


def last_chat():
    return config.home() / "sites" / "gemini" / "google-default" / "last_chat"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
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


def test_ask_json_when_piped(monkeypatch):
    calls = stub(monkeypatch, "ask", ANSWER)
    result = run("ask", "What is 2+2?")
    assert result.exit_code == 0
    assert json.loads(result.stdout) == OUT
    assert result.stderr == ""
    assert calls == [(("What is 2+2?",), {"model": None, "thinking": None, "chat_id": None, "files": [],
                                          "timeout": 150, "headed": False, "image_format": None,
                                          "canvas": False, "footnotes": True})]


def test_ask_text_prints_only_the_answer(monkeypatch):
    stub(monkeypatch, "ask", {**ANSWER, "citations": [{"n": 1, "label": "x.test", "title": "x", "url": "https://x.test"}]})
    for result in (run("ask", "q", "--format", "text"), run("ask", "q", env={"LLMCLI_FORMAT": "table"})):
        assert (result.exit_code, result.stdout) == (0, "Two plus two is **4**.\n")


def test_ask_options_reach_fetch(monkeypatch, tmp_path):
    attachment = tmp_path / "notes.txt"
    attachment.write_text("hello")
    calls = stub(monkeypatch, "ask", ANSWER)
    result = run("ask", "Summarize", "--model", "thinking", "--chat", "https://gemini.google.com/app/1a2b3c4d5e6f7a8b",
                 "--file", str(attachment), "--timeout", "60", "--headed", "--thinking", "H")
    assert result.exit_code == 0
    assert calls[0][1] == {"model": "thinking", "thinking": "high", "chat_id": "1a2b3c4d5e6f7a8b",
                           "files": [str(attachment.resolve())], "timeout": 60, "headed": True, "image_format": None,
                           "canvas": False, "footnotes": True}


@pytest.mark.parametrize(("value", "level"), [
    ("low", "low"), ("l", "low"), ("Low", "low"), ("L", "low"),
    ("medium", "medium"), ("m", "medium"), ("MEDIUM", "medium"),
    ("high", "high"), ("h", "high"), ("High", "high"),
])
def test_thinking_levels_and_aliases(monkeypatch, value, level):
    calls = stub(monkeypatch, "ask", ANSWER)
    assert run("ask", "q", "--thinking", value).exit_code == 0
    assert run("-p", "--thinking", value, "q").exit_code == 0
    assert [c[1]["thinking"] for c in calls] == [level, level]


def test_ask_reads_stdin_with_dash(monkeypatch):
    calls = stub(monkeypatch, "ask", ANSWER)
    assert run("ask", "-", input="  multi\nline prompt\n").exit_code == 0
    assert calls[0][0] == ("multi\nline prompt",)


@pytest.mark.parametrize(
    "args",
    [
        ("ask",),  # no prompt
        ("ask", "   "),  # empty prompt
        ("ask", "q", "--new", "--chat", "1a2b3c4d5e6f7a8b"),
        ("ask", "q", "--chat", "../settings"),
        ("ask", "q", "--file", "/no/such/file.txt"),
        ("ask", "q", "--timeout", "0"),
        ("ask", "q", "--thinking", "extreme"),
        ("ask", "q", "--thinking", ""),
        ("ask", "q", "--thinking", "lo"),
        ("ask", "q", "--thinking"),
        ("chats", "show", "not-an-id"),
        ("chats", "list", "--limit", "0"),
        ("nope",),
    ],
)
def test_usage_errors_are_json_exit_2_before_any_browser_work(monkeypatch, args):
    calls = stub(monkeypatch, "ask", ANSWER)
    stub(monkeypatch, "chat", {})
    result = run(*args)
    assert result.exit_code == 2, result.output
    assert json.loads(result.stderr)["code"] == 2
    assert calls == []


def test_ask_dash_never_waits_on_a_terminal(monkeypatch):
    calls = stub(monkeypatch, "ask", ANSWER)

    class Tty:
        def isatty(self):
            return True

        def read(self):
            pytest.fail("read a TTY")

    class Sys:
        stdin = Tty()

    monkeypatch.setattr(llm, "sys", Sys)
    result = run("ask", "-")
    assert result.exit_code == 2 and "stdin is a terminal" in json.loads(result.stderr)["error"]
    assert calls == []


CHAT = ANSWER["chatId"]
PRINTED = "Two plus two is **4**.\n"


def test_print_mode_stdout_is_only_the_answer(monkeypatch):
    calls = stub(monkeypatch, "ask", ANSWER)
    for args in (("-p", "What", "is", "2+2?"), ("--print", "What is 2+2?"), ("--lock-wait", "7", "-p", "What is 2+2?")):
        result = run(*args, env={"LLMCLI_FORMAT": "json"})  # the env format doesn't apply to -p
        assert (result.exit_code, result.stdout, result.stderr) == (0, PRINTED, f"chat: {CHAT}\n")
    assert calls[0] == (("What is 2+2?",), {"chat_id": None, "model": None, "thinking": None, "timeout": 150, "image_format": None,
                                            "files": [], "canvas": False, "footnotes": True, "headed": False})
    assert browser.lock_wait == 7.0
    quiet = run("-p", "-q", "-m", "3.8 Flash", "--timeout", "60", "q")
    assert (quiet.stdout, quiet.stderr) == (PRINTED, "")
    assert calls[-1][1] == {"chat_id": None, "model": "3.8 Flash", "thinking": None, "timeout": 60, "image_format": None,
                            "files": [], "canvas": False, "footnotes": True, "headed": False}
    assert json.loads(run("-p", "--output-format", "json", "q").stdout) == OUT


def test_print_mode_reads_stdin_but_never_a_terminal(monkeypatch):
    calls = stub(monkeypatch, "ask", ANSWER)
    assert run("-p", input="multi\nline prompt\n").exit_code == 0
    assert run("-p", "-", input="  piped  ").exit_code == 0
    assert run("-p", "", input="piped").exit_code == 2  # an empty prompt, not "read stdin"
    assert [c[0] for c in calls] == [("multi\nline prompt",), ("piped",)]

    class Tty:
        def isatty(self):
            return True

        def read(self):
            pytest.fail("read a TTY")

    monkeypatch.setattr(llm, "sys", type("Sys", (), {"stdin": Tty()}))
    result = run("-p")
    assert result.exit_code == 2 and "stdin is a terminal" in json.loads(result.stderr)["error"]
    assert len(calls) == 2


def test_print_mode_prompt_file(monkeypatch, tmp_path):
    calls = stub(monkeypatch, "ask", ANSWER)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Summarize:\n\nnotes\n")
    assert run("-p", "--prompt-file", str(prompt)).exit_code == 0
    assert calls[0][0] == ("Summarize:\n\nnotes",)
    for bad in (b"   \n", b"\xff\xfe bad utf-8"):
        prompt.write_bytes(bad)
        assert run("-p", "--prompt-file", str(prompt)).exit_code == 2
    assert run("-p", "--prompt-file", str(prompt), "also words").exit_code == 2
    assert len(calls) == 1


def test_every_finished_ask_is_remembered_for_continue(monkeypatch):
    calls = stub(monkeypatch, "ask", ANSWER)
    result = run("-p", "-c", "more")
    assert (result.exit_code, json.loads(result.stderr)["error"]) == (2, "No chat to continue: no ask has finished yet.")
    assert calls == []
    assert run("ask", "What is 2+2?").exit_code == 0
    assert last_chat().read_text() == CHAT + "\n"
    assert last_chat().stat().st_mode & 0o777 == 0o600
    assert run("-p", "--continue", "more").exit_code == 0
    assert calls[-1] == (("more",), {"chat_id": CHAT, "model": None, "thinking": None, "timeout": 150, "image_format": None,
                                     "files": [], "canvas": False, "footnotes": True, "headed": False})
    stub(monkeypatch, "ask", {**ANSWER, "chatId": "1a2b3c4d5e6f7a8b"})
    assert run("-p", "--chat", "https://gemini.google.com/app/1a2b3c4d5e6f7a8b", "hi").exit_code == 0
    assert last_chat().read_text() == "1a2b3c4d5e6f7a8b\n"
    stub(monkeypatch, "ask", {**ANSWER, "chatId": None})  # a new chat that never showed its id
    result = run("-p", "hi")
    assert (result.stdout, result.stderr) == (PRINTED, "")
    assert not last_chat().exists()


def test_print_mode_output_file_is_written_atomically(monkeypatch, tmp_path):
    stub(monkeypatch, "ask", ANSWER)
    out = tmp_path / "run" / "answer.txt"
    out.parent.mkdir()
    result = run("-p", "--output-file", str(out), "q")
    assert (result.exit_code, result.stdout, result.stderr) == (0, "", f"chat: {CHAT}\n")
    assert out.read_text() == PRINTED
    assert run("-p", "--output-file", str(out), "--output-format", "json", "q").exit_code == 0
    assert json.loads(out.read_text()) == OUT
    assert sorted(p.name for p in out.parent.iterdir()) == ["answer.txt"]  # no temp files left

    def no_rename(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(agent_ux.os, "replace", no_rename)
    result = run("-p", "--output-file", str(out), "q")
    assert (result.exit_code, result.stdout) == (5, "")
    assert json.loads(result.stderr)["hint"] == f"Don't resend the prompt. Read it with: llmcli gemini chats show {CHAT}"
    assert json.loads(out.read_text()) == OUT  # the old file, whole
    assert sorted(p.name for p in out.parent.iterdir()) == ["answer.txt"]


@pytest.mark.parametrize("args", [
    ("-p", "-c", "--chat", CHAT, "hi"),
    ("-p", "--chat", "../settings", "hi"),
    ("-p", "--timeout", "0", "hi"),
    ("-p", "--thinking", "x", "hi"),
    ("-p", "   "),
    ("-p", "--output-file", "/no/such/dir/answer.txt", "hi"),
    ("-p", "--output-file", "/tmp", "hi"),  # a directory
    ("-p", "--prompt-file", "/no/such/prompt.txt"),
])
def test_print_mode_usage_errors_exit_2_before_sending(monkeypatch, args):
    calls = stub(monkeypatch, "ask", ANSWER)
    result = run(*args, input="")
    assert (result.exit_code, result.stdout) == (2, "")
    assert json.loads(result.stderr)["code"] == 2
    assert calls == []


def test_print_mode_failures_keep_stdout_empty(monkeypatch):
    stub(monkeypatch, "ask", AuthRequired("Not signed in to Gemini."))
    result = run("-p", "hi")
    assert (result.exit_code, result.stdout) == (4, "")
    stub(monkeypatch, "ask", TimeoutError("No answer after 300s."))
    result = run("-p", "hi")
    assert (result.exit_code, result.stdout) == (5, "")
    assert run("-p", "--help").stdout.startswith("Usage: llmcli gemini -p [OPTIONS] [PROMPT]...")


def test_an_error_reply_exits_5_with_the_json_result_still_on_stdout(monkeypatch, tmp_path):
    stub(monkeypatch, "ask", ERROR_ANSWER)
    error = {"error": ERROR_REPLY, "code": 5,
             "hint": f"The prompt reached Gemini (chat {CHAT}). Retry it, or try another --model."}
    printed = {**OUT, **ERROR_ANSWER, "error": ERROR_REPLY}  # the answer, plus an error a file reader can see
    for args in (("ask", "q"), ("ask", "q", "--format", "json"), ("-p", "-q", "--output-format", "json", "q")):
        result = run(*args)
        assert (result.exit_code, json.loads(result.stdout), json.loads(result.stderr)) == (5, printed, error)
    out = tmp_path / "answer.json"
    result = run("-p", "--output-format", "json", "--output-file", str(out), "q")
    assert (result.exit_code, result.stdout, json.loads(out.read_text())) == (5, "", printed)
    assert last_chat().read_text() == CHAT + "\n"  # the chat exists: -c can retry in it


def test_an_error_reply_is_not_printed_as_text(monkeypatch, tmp_path):
    stub(monkeypatch, "ask", {**ERROR_ANSWER, "chatId": None})
    out = tmp_path / "answer.txt"
    for args in (("ask", "q", "--format", "text"), ("-p", "q"), ("-p", "-q", "q"),
                 ("-p", "--output-file", str(out), "q")):
        result = run(*args)
        assert (result.exit_code, result.stdout) == (5, "")
        assert json.loads(result.stderr) == {  # no `chat: <id>` line: stderr is the one JSON error
            "error": ERROR_REPLY, "code": 5, "hint": "The prompt reached Gemini. Retry it, or try another --model."}
    assert not out.exists()


def test_a_refusal_is_an_answer(monkeypatch):
    stub(monkeypatch, "ask", {**ANSWER, "answer": "I can't help with that."})
    result = run("-p", "q")
    assert (result.exit_code, result.stdout) == (0, "I can't help with that.\n")


def test_exit_codes_and_json_errors(monkeypatch):
    stub(monkeypatch, "ask", AuthRequired("Not signed in to Gemini."))
    result = run("ask", "q")
    assert result.exit_code == 4
    assert json.loads(result.stderr) == {"error": "Not signed in to Gemini.", "code": 4, "hint": "a person runs: llmcli gemini login (or webcli identity login google:default)"}
    stub(monkeypatch, "ask", cli.fetch.NotFound("No model matches 'gpt'. Available: Fast.", hint="run: gweb models"))
    result = run("ask", "q", "--model", "gpt")
    assert (result.exit_code, json.loads(result.stderr)["hint"]) == (3, "run: gweb models")
    stub(monkeypatch, "ask", ProfileBusy("busy"))
    assert run("ask", "q").exit_code == 5
    stub(monkeypatch, "ask", RuntimeError("Gemini never sent the prompt."))
    assert run("ask", "q").exit_code == 5
    stub(monkeypatch, "chats", [])
    assert run("chats", "list").exit_code == 3


def test_no_profile_is_exit_4_without_chrome(monkeypatch):
    monkeypatch.setattr(Identity, "profile_exists", lambda self: False)
    for name in ("ask", "chats", "chat", "models", "status"):
        stub(monkeypatch, name, AssertionError("must not launch Chrome"))
    for args in (("ask", "q"), ("chats", "list"), ("chats", "show", "1a2b3c4d5e6f7a8b"), ("models",)):
        result = run(*args)
        assert (result.exit_code, json.loads(result.stderr)["hint"]) == (4, "a person runs: llmcli gemini login (or webcli identity login google:default)")
    result = run("status")
    assert result.exit_code == 4
    assert json.loads(result.stdout) == {"identity": "google:default", "signedIn": False, "name": None, "email": None,
                                         "api": None}


def test_status(monkeypatch):
    info = {"signedIn": True, "name": "Test Owner", "email": "owner@example.com", "api": "ok"}
    stub(monkeypatch, "status", info)
    result = run("status")
    assert (result.exit_code, json.loads(result.stdout)) == (0, {"identity": "google:default", **info})
    stub(monkeypatch, "status", {**info, "signedIn": False})
    assert run("status").exit_code == 4


def test_chats_list_select_compact_csv_and_limit(monkeypatch):
    calls = stub(monkeypatch, "chats", CHATS)
    rows = json.loads(run("chats", "list", "--limit", "1").stdout)
    assert [r["id"] for r in rows] == ["1a2b3c4d5e6f7a8b"] and set(rows[0]) == set(cli.CHAT_FIELDS)
    assert calls[0] == ((1,), {"headed": False})
    assert json.loads(run("chats", "list", "--select", "id,pinned").stdout) == [
        {"id": "1a2b3c4d5e6f7a8b", "pinned": True}, {"id": "0f1e2d3c4b5a6978", "pinned": False}]
    assert json.loads(run("chats", "list", "--compact").stdout)[1] == {
        "id": "0f1e2d3c4b5a6978", "title": "Fix flaky pytest", "updated": "2025-10-14T11:06:40Z"}
    assert run("chats", "list", "--csv", "--select", "id,title").stdout.splitlines() == [
        "id,title", "1a2b3c4d5e6f7a8b,Trip ideas for Kyoto", "0f1e2d3c4b5a6978,Fix flaky pytest"]


def test_env_format_overrides_the_default_but_not_the_flag(monkeypatch):
    stub(monkeypatch, "chats", CHATS)
    table = run("chats", "list", env={"LLMCLI_FORMAT": "table"})
    assert table.exit_code == 0 and "Gemini chats" in table.stdout and "Trip ideas for Kyoto" in table.stdout
    with pytest.raises(ValueError):
        json.loads(table.stdout)
    assert json.loads(run("chats", "list", "--format", "json", env={"LLMCLI_FORMAT": "table"}).stdout)[0]["pinned"]
    assert json.loads(run("chats", "list", env={"LLMCLI_FORMAT": "json"}).stdout)[0]["id"] == "1a2b3c4d5e6f7a8b"
    monkeypatch.setattr(Identity, "profile_exists", lambda self: False)  # usage errors come before the session check
    for args in (("chats", "list"), ("ask", "q"), ("agent-help",)):
        result = run(*args, env={"LLMCLI_FORMAT": "yaml"})
        assert (result.exit_code, json.loads(result.stderr)["code"]) == (2, 2)


def test_chats_show_text_and_json(monkeypatch):
    chat = {"chatId": "1a2b3c4d5e6f7a8b", "url": "https://gemini.google.com/app/1a2b3c4d5e6f7a8b", "messages": [
        {"role": "user", "text": "Plan 3 days in Kyoto", "at": "2025-10-14T11:06:40Z"},
        {"role": "model", "text": "**Day 1:** Fushimi Inari", "at": "2025-10-14T11:06:40Z"},
    ]}
    calls = stub(monkeypatch, "chat", chat)
    assert json.loads(run("chats", "show", "c_1a2b3c4d5e6f7a8b").stdout) == chat
    assert calls[0][0] == ("1a2b3c4d5e6f7a8b",)
    assert run("chats", "show", "1a2b3c4d5e6f7a8b", "--format", "text").stdout == (
        "## You\n\nPlan 3 days in Kyoto\n\n## Gemini\n\n**Day 1:** Fushimi Inari\n\n")
    stub(monkeypatch, "chat", cli.fetch.NotFound("Chat 1a2b3c4d5e6f7a8b not found.", hint="run: gweb chats list"))
    result = run("chats", "show", "1a2b3c4d5e6f7a8b")
    assert result.exit_code == 3 and json.loads(result.stderr)["hint"] == "run: gweb chats list"


def test_models(monkeypatch):
    stub(monkeypatch, "models", MODELS)
    assert json.loads(run("models").stdout) == MODELS
    assert json.loads(run("models", "--compact").stdout) == [{"name": "Fast", "current": True},
                                                              {"name": "Thinking", "current": False}]
    stub(monkeypatch, "models", [])
    assert run("models").exit_code == 3


def test_lock_wait_flag_and_env(monkeypatch):
    stub(monkeypatch, "models", MODELS)
    assert run("--lock-wait", "7", "models").exit_code == 0
    assert browser.lock_wait == 7.0
    assert run("models", env={"WEBCLI_LOCK_WAIT": "2.5"}).exit_code == 0
    assert browser.lock_wait == 2.5
    assert run("--lock-wait", "-1", "models").exit_code == 2
    result = run("models", env={"WEBCLI_LOCK_WAIT": "nan"})
    assert (result.exit_code, json.loads(result.stderr)["code"]) == (2, 2)


def test_sigterm_mid_command_exits_143_with_json_error(monkeypatch):
    async def killed(*args, **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(5)

    monkeypatch.setattr(cli.fetch, "ask", killed)
    result = run("ask", "q")
    assert result.exit_code == 143
    assert json.loads(result.stderr) == {"error": "Interrupted by SIGTERM; the command was cancelled and its Chrome tab closed.", "code": 143}


def test_agent_help_json_lists_every_command_flag_and_exit_code():
    result = run("agent-help", "--format", "json")
    assert result.exit_code == 0
    ref = json.loads(result.stdout)
    by_name = {c["name"]: c for c in ref["commands"]}
    assert sorted(by_name) == ["-p", "agent-help", "ask", "chats list", "chats show", "login", "models", "serve",
                               "status"]
    assert by_name["ask"]["usage"] == "llmcli gemini ask [OPTIONS] PROMPT"
    assert by_name["-p"]["usage"] == "llmcli gemini -p [OPTIONS] [PROMPT]..."
    assert by_name["chats show"]["usage"] == "llmcli gemini chats show [OPTIONS] ID"
    flags = {f for c in ref["commands"] for o in c["options"] for f in o["flags"]}
    assert {"--format", "--select", "--compact", "--csv", "--quiet", "-q", "--limit", "--model", "--new", "--chat",
            "--file", "--timeout", "--headed", "-m", "-c", "--continue", "--prompt-file", "--output-file",
            "--output-format", "--thinking"} <= flags
    for name in ("ask", "-p"):
        thinking = next(o for o in by_name[name]["options"] if o["flags"] == ["--thinking"])
        assert thinking["choices"] == ["low", "medium", "high", "l", "m", "h"]
    assert next(o for o in by_name["chats list"]["options"] if "--limit" in o["flags"])["default"] == 50
    assert sorted(ref["exitCodes"]) == ["0", "129", "130", "143", "2", "3", "4", "5", "6", "7"]
    assert set(ref["env"]) == {"LLMCLI_FORMAT", "WEBCLI_LOCK_WAIT", "WEBCLI_IDENTITY", "WEBCLI_DAEMON",
                               "WEBCLI_SERVE_RECYCLE_HOURS", "WEBCLI_HOME"}
    assert ref["version"] == "0.1.0" and ref["writes"] == cli.WRITES
    assert ["--lock-wait"] in [o["flags"] for o in ref["globalOptions"]]
    assert ["--identity"] in [o["flags"] for o in ref["globalOptions"]]


def test_agent_help_markdown_and_env_default():
    md = run("agent-help", "--format", "markdown").stdout
    assert md.startswith("# llmcli gemini agent reference")
    assert "- `llmcli gemini chats list [OPTIONS]`: Your chats in sidebar order" in md
    assert "## Writes (everything else is read-only)" in md
    assert "- 4: sign-in needed, or Cloudflare needs a visible check" in md
    assert "- 7: config error" in md
    assert json.loads(run("agent-help").stdout)["name"] == "llmcli gemini"  # piped -> json
    assert run("agent-help", env={"LLMCLI_FORMAT": "text"}).stdout.startswith("# llmcli gemini")


@pytest.mark.parametrize('args', [('ask', 'q', '--format', 'text'), ('-p', 'q')])
def test_hard_time_error_exits_5(monkeypatch, args):
    stub(monkeypatch, 'ask', {**ANSWER, 'answer': "I'm having a hard time fulfilling your request. Can I help you with something else instead?"})
    result = run(*args)
    assert result.exit_code == 5
    assert result.stdout == ''
    assert 'error instead of an answer' in json.loads(result.stderr)['error']


@pytest.mark.parametrize('command', [('ask', 'q', '--format', 'json'), ('-p', 'q'),
    ('-p', 'q', '--output-format', 'json')])
@pytest.mark.parametrize('banner', [False, True])
@pytest.mark.parametrize('message', ["Your limit resets at 1:53 PM", 'Quota exceeded'])
def test_rate_limit_exit_and_json(monkeypatch, command, banner, message):
    reset = '2026-10-16T13:53:00-05:00' if 'resets' in message else None
    monkeypatch.setattr(cli, 'resets_at', lambda text: reset)
    value = cli.fetch.RateLimited(message, sent=False) if banner else {**ANSWER, 'answer': message}
    calls = stub(monkeypatch, 'ask', value)
    result = run(*command)
    assert result.exit_code == 6
    error = json.loads(result.stderr)
    expected = {'error': 'rate_limited', 'code': 6, 'message': message, 'resets_at': reset}
    assert {key: error[key] for key in expected} == expected
    if not banner and command != ('-p', 'q'):
        output = json.loads(result.stdout)
        assert {key: output[key] for key in expected} == expected
    else:
        assert not result.stdout
    assert len(calls) == 1  # no rate-limit retry


def test_rate_limit_print_does_not_replace_answer_file(monkeypatch, tmp_path):
    target = tmp_path / 'answer.txt'
    target.write_text('previous')
    stub(monkeypatch, 'ask', {**ANSWER, 'answer': 'Usage limit reached'})
    result = run('-p', 'q', '--output-file', str(target))
    assert result.exit_code == 6
    assert target.read_text() == 'previous'


# --image-out
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x0a\xc0\x00\x00\x06\x00\x08\x02\x00\x00\x00" + b"\x00" * 16
JPG = b"\xff\xd8\xff\xc0\x00\x11\x08\x03\x00\x05\x60\x03"
IMAGE_ANSWER = {**ANSWER, "prompt": "blue stars", "answer": "", "imageCount": 1,
                "imageData": [{"data": PNG, "source_url_kind": "full_size"}]}


def test_image_out_dir_saves_and_reports_each_image(monkeypatch, tmp_path):
    calls = stub(monkeypatch, "ask", IMAGE_ANSWER)
    out = tmp_path / "imgs"
    result = run("ask", "blue stars", "--image-out", str(out), "--format", "json")
    assert result.exit_code == 0, result.stderr
    printed = json.loads(result.stdout)
    path = str((out / "blue-stars-1.png").resolve())
    assert printed["images"] == [{"path": path, "width": 2752, "height": 1536, "bytes": len(PNG), "mime": "image/png",
                                  "source_url_kind": "full_size"}]
    assert "imageData" not in printed and printed["chatUrl"] == ANSWER["chatUrl"] and printed["imageCount"] == 1
    assert "imageHint" not in printed  # saved, so nothing to hint
    assert (out / "blue-stars-1.png").read_bytes() == PNG
    assert calls[0][1]["image_format"] == "png" and calls[0][1]["timeout"] == 300  # image default
    text = run("-p", "--image-out", str(out), "blue stars")
    assert (text.exit_code, text.stdout) == (0, str((out / "blue-stars-2.png").resolve()) + "\n")  # never overwrites


def test_image_out_path_writes_exactly_it_and_numbers_extra_images(monkeypatch, tmp_path):
    calls = stub(monkeypatch, "ask", {**IMAGE_ANSWER, "answer": "Here you go.", "imageCount": 2, "imageData": [
        {"data": JPG, "source_url_kind": "full_size"}, {"data": JPG, "source_url_kind": "full_size"}]})
    target = tmp_path / "stars.jpg"
    result = run("ask", "blue stars", "--image-out", str(target), "--format", "text")
    assert result.exit_code == 0
    assert result.stdout == f"Here you go.\n{target.resolve()}\n{(tmp_path / 'stars-2.jpg').resolve()}\n"
    assert calls[0][1]["image_format"] == "jpeg"
    out = tmp_path / "answer.json"
    assert run("-p", "--image-out", str(target), "--output-format", "json", "--output-file", str(out), "q").exit_code == 0
    assert [(i["path"], i["width"], i["height"], i["mime"]) for i in json.loads(out.read_text())["images"]] == [
        (str(target.resolve()), 1376, 768, "image/jpeg"), (str((tmp_path / "stars-2.jpg").resolve()), 1376, 768, "image/jpeg")]


def test_image_out_without_an_image_exits_5_after_printing(monkeypatch, tmp_path):
    stub(monkeypatch, "ask", {**ANSWER, "answer": "I can't draw that.", "imageData": []})
    out = tmp_path / "imgs"
    result = run("ask", "q", "--image-out", str(out), "--format", "json")
    assert result.exit_code == 5
    assert json.loads(result.stdout)["error"] == "Gemini answered without an image: I can't draw that."
    assert json.loads(result.stdout)["images"] == []
    assert "Retry it, or try another --model" in json.loads(result.stderr)["hint"]
    assert (run("-p", "--image-out", str(out), "q").stdout, list(out.iterdir())) == ("", [])


def test_an_image_reply_without_image_out_still_says_it_made_one(monkeypatch):
    stub(monkeypatch, "ask", {**ANSWER, "answer": "", "imageCount": 1})
    line = f"[Gemini made 1 image: {ANSWER['chatUrl']}. Save images with --image-out DIR.]\n"
    assert run("-p", "q").stdout == line
    assert run("ask", "q", "--format", "text").stdout == line
    assert json.loads(run("ask", "q").stdout)["images"] == []


def test_an_image_reply_without_image_out_exits_0_with_an_image_hint(monkeypatch):
    stub(monkeypatch, "ask", {**ANSWER, "answer": "Here you go.", "imageCount": 2})
    hint = (f"Gemini made 2 image(s) that were not saved; rerun with --image-out DIR|PATH (or open {ANSWER['chatUrl']}).")
    for args in (("ask", "q", "--format", "json"), ("-p", "--output-format", "json", "q")):
        result = run(*args)
        assert result.exit_code == 0, result.stderr
        printed = json.loads(result.stdout)
        assert (printed["answer"], printed["imageCount"], printed["images"], printed["imageHint"]) == (
            "Here you go.", 2, [], hint)
        assert "error" not in printed
    stub(monkeypatch, "ask", ANSWER)
    assert "imageHint" not in json.loads(run("ask", "q", "--format", "json").stdout)  # no images, no hint


def test_a_failed_image_write_says_not_to_resend(monkeypatch, tmp_path):
    stub(monkeypatch, "ask", IMAGE_ANSWER)

    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cli.images, "save", full_disk)
    result = run("ask", "q", "--image-out", str(tmp_path))
    assert (result.exit_code, result.stdout) == (5, "")
    error = json.loads(result.stderr)
    assert "No space left" in error["error"]
    assert error["hint"] == f"Don't resend the prompt. Open it at {ANSWER['chatUrl']}"


@pytest.mark.parametrize("args", [
    ("--image-out", "/no/such/dir/stars.png"),
    ("--image-out", "/dev/null/sub/"),  # can't be created
])
def test_image_out_usage_errors_exit_2_before_sending(monkeypatch, args):
    calls = stub(monkeypatch, "ask", IMAGE_ANSWER)
    for command in (("ask", "q"), ("-p", "q")):
        result = run(*command[:1], *args, *command[1:])
        assert result.exit_code == 2, result.stderr
    assert calls == []


@pytest.mark.parametrize(("args", "timeout"), [
    ((), 150),
    (("--model", "3.8 Flash"), 150),
    (("--model", "Argon"), 300),
    (("--model", "4 argon"), 300),
    (("--model", "Nano Banana"), 300),
    (("--model", "Argon", "--timeout", "45"), 45),  # an explicit --timeout always wins
    (("--timeout", "999"), 999),
])
def test_timeout_defaults_to_longer_for_image_models(monkeypatch, args, timeout):
    calls = stub(monkeypatch, "ask", ANSWER)
    assert run("ask", "q", *args).exit_code == 0
    assert run("-p", *args, "q").exit_code == 0
    assert [c[1]["timeout"] for c in calls] == [timeout, timeout]


def test_agent_help_documents_image_out(monkeypatch):
    ref = json.loads(run("agent-help", "--format", "json").stdout)
    flags = {c["name"]: [f for o in c["options"] for f in o["flags"]] for c in ref["commands"]}
    assert "--image-out" in flags["ask"] and "--image-out" in flags["-p"]
    assert any("--image-out DIR|PATH" in n for n in ref["notes"])



def sparse(path, size):
    with open(path, "wb") as handle:
        handle.truncate(size)
    return str(path)


def test_print_mode_attaches_files_like_ask(monkeypatch, tmp_path):
    a, b = tmp_path / "a.md", tmp_path / "b.png"
    a.write_text("# A")
    b.write_bytes(b"\x89PNG")
    calls = stub(monkeypatch, "ask", ANSWER)
    assert run("-p", "--file", str(a), "--file", str(b), "Compare them").exit_code == 0
    assert calls[0][1]["files"] == [str(a.resolve()), str(b.resolve())]
    assert run("-p", "--file", str(tmp_path / "missing.txt"), "q").exit_code == 2
    assert run("-p", "--file", str(tmp_path), "q").exit_code == 2  # a directory
    assert len(calls) == 1


def test_file_limits_are_checked_before_anything_is_sent(monkeypatch, tmp_path):
    calls = stub(monkeypatch, "ask", ANSWER)
    notes = [tmp_path / f"n{i}.txt" for i in range(11)]
    for note in notes:
        note.write_text("x")
    for command in ("ask", "-p"):
        ten = run(command, *[a for n in notes[:10] for a in ("--file", str(n))], "q")
        assert ten.exit_code == 0, ten.stderr
        eleven = run(command, *[a for n in notes for a in ("--file", str(n))], "q")
        assert eleven.exit_code == 2
        assert "at most 10 files per prompt; got 11" in json.loads(eleven.stderr)["error"]
    assert len(calls) == 2


@pytest.mark.parametrize(("name", "size", "ok"), [
    ("big.txt", 100 * 2**20, True),  # Gemini's "100 MB" is 100 MiB (checked live)
    ("big.pdf", 100 * 2**20 + 1, False),
    ("big.png", 100 * 2**20 + 1, False),
    ("clip.mp4", 100 * 2**20 + 1, True),  # video: 2 GB
    ("clip.mov", 2 * 2**30, True),
    ("clip.mp4", 2 * 2**30 + 1, False),
])
def test_file_size_limits_per_type(monkeypatch, tmp_path, name, size, ok):
    calls = stub(monkeypatch, "ask", ANSWER)
    result = run("ask", "q", "--file", sparse(tmp_path / name, size))
    assert result.exit_code == (0 if ok else 2), result.stderr
    if not ok:
        assert "Gemini takes up to" in json.loads(result.stderr)["error"]
    assert len(calls) == (1 if ok else 0)


def test_zip_limits(monkeypatch, tmp_path):
    calls = stub(monkeypatch, "ask", ANSWER)

    def make(name, members):
        path = tmp_path / name
        with zipfile.ZipFile(path, "w") as archive:
            for member in members:
                archive.writestr(member, "x")
        return str(path)

    assert run("ask", "q", "--file", make("ok.zip", ["a.txt", "dir/b.py"])).exit_code == 0
    too_many = run("ask", "q", "--file", make("many.zip", [f"{i}.txt" for i in range(11)]))
    assert "holds 11 files" in json.loads(too_many.stderr)["error"]
    media = run("ask", "q", "--file", make("media.zip", ["a.txt", "song.mp3"]))
    assert "holds song.mp3" in json.loads(media.stderr)["error"]
    assert run("ask", "q", "--file", make("doc.docx", [f"{i}.xml" for i in range(20)])).exit_code == 0  # not a .zip
    (tmp_path / "bad.zip").write_text("not a zip")
    assert run("ask", "q", "--file", str(tmp_path / "bad.zip")).exit_code == 2
    assert len(calls) == 2


CANVAS_MD = "# Rain Probe\n\nRain on tin."
CANVAS_ANSWER = {**ANSWER, "answer": "Here is your scene.", "canvas": {"title": "Rain Probe", "markdown": CANVAS_MD}}


def test_canvas_json_has_the_doc(monkeypatch):
    calls = stub(monkeypatch, "ask", CANVAS_ANSWER)
    for args in (("ask", "q", "--canvas", "--format", "json"), ("-p", "--canvas", "--output-format", "json", "q")):
        result = run(*args)
        assert result.exit_code == 0, result.stderr
        assert json.loads(result.stdout)["canvas"] == {"title": "Rain Probe", "markdown": CANVAS_MD,
                                                       "chars": len(CANVAS_MD), "path": None}
    assert [(c[1]["canvas"], c[1]["timeout"]) for c in calls] == [(True, 300), (True, 300)]
    assert run("-p", "--canvas", "q").stdout == f"Here is your scene.\n{CANVAS_MD}\n"  # nothing saved: the markdown


def test_canvas_out_writes_the_markdown_and_prints_its_path(monkeypatch, tmp_path):
    calls = stub(monkeypatch, "ask", CANVAS_ANSWER)
    target = tmp_path / "new" / "scene.md"  # its directory is created
    result = run("-p", "--canvas-out", str(target), "q")
    assert (result.exit_code, result.stdout) == (0, f"Here is your scene.\n{target.resolve()}\n")
    assert target.read_text() == CANVAS_MD and calls[0][1]["canvas"] is True  # --canvas-out implies --canvas
    target.write_text("old")
    printed = json.loads(run("ask", "q", "--canvas-out", str(target), "--format", "json").stdout)
    assert printed["canvas"]["path"] == str(target.resolve()) and target.read_text() == CANVAS_MD  # replaced
    assert sorted(p.name for p in target.parent.iterdir()) == ["scene.md"]  # no temp files left


def test_canvas_without_a_doc_exits_5_after_printing(monkeypatch, tmp_path):
    stub(monkeypatch, "ask", {**ANSWER, "canvas": None})
    target = tmp_path / "scene.md"
    result = run("ask", "q", "--canvas-out", str(target), "--format", "json")
    assert result.exit_code == 5
    printed = json.loads(result.stdout)
    assert (printed["canvas"], printed["error"]) == (None, "Gemini answered without a Canvas doc: Two plus two is **4**.")
    assert "Retry it" in json.loads(result.stderr)["hint"]
    text = run("-p", "--canvas", "q")
    assert (text.exit_code, text.stdout) == (5, "")
    assert not target.exists()


def test_a_failed_canvas_write_says_not_to_resend(monkeypatch, tmp_path):
    stub(monkeypatch, "ask", CANVAS_ANSWER)

    def full_disk(path, text):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(llm, "write_atomic", full_disk)
    result = run("ask", "q", "--canvas-out", str(tmp_path / "scene.md"))
    assert (result.exit_code, result.stdout) == (5, "")
    assert json.loads(result.stderr)["hint"].startswith("Don't resend the prompt.")


@pytest.mark.parametrize("args", [
    ("--canvas", "--image-out", "/tmp/"),
    ("--canvas-out", "/tmp/x.md", "--image-out", "/tmp/"),
    ("--canvas-out", "/tmp"),  # a directory
    ("--canvas-out", "/tmp/some/dir/"),
    ("--canvas-out", "/dev/null/sub/x.md"),  # can't be created
])
def test_canvas_usage_errors_exit_2_before_sending(monkeypatch, args):
    calls = stub(monkeypatch, "ask", CANVAS_ANSWER)
    for command in ("ask", "-p"):
        assert run(command, *args, "q").exit_code == 2
    assert calls == []


def test_agent_help_documents_file_limits_and_canvas():
    ref = json.loads(run("agent-help", "--format", "json").stdout)
    flags = {c["name"]: [f for o in c["options"] for f in o["flags"]] for c in ref["commands"]}
    for name in ("ask", "-p"):
        assert {"--file", "--canvas", "--canvas-out"} <= set(flags[name])
    assert any("at most 10 files per prompt" in n for n in ref["notes"])
    assert any("--canvas-out PATH" in n for n in ref["notes"])


def test_no_footnotes_reaches_fetch_and_the_answer_prints_as_is(monkeypatch):
    cited = {**ANSWER, "answer": "Two plus two is 4[^1].\n\n[^1]: Math, https://math.example/",
             "citations": [{"n": 1, "label": "math.example", "title": "Math", "url": "https://math.example/"}]}
    calls = stub(monkeypatch, "ask", cited)
    assert run("-p", "q").stdout == cited["answer"] + "\n"  # text output: the footnoted markdown
    assert json.loads(run("ask", "q").stdout)["citations"] == cited["citations"]
    assert run("ask", "--no-footnotes", "q").exit_code == 0 and run("-p", "--no-footnotes", "q").exit_code == 0
    assert [c[1]["footnotes"] for c in calls] == [True, True, False, False]
