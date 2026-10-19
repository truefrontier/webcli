"""Offline CLI tests: flags, formats, exit codes, JSON errors, the ask JSON shape, agent-help. fetch is stubbed."""

from __future__ import annotations

import copy
import json
import struct
import zlib

import pytest
from click.testing import CliRunner

from webcli.apps import llmcli
from webcli.core import browser, config
from webcli.core.errors import AuthRequired, Challenged, ProfileBusy
from webcli.core.identities import Identity
from webcli.sites.chatgpt import cli

CHAT = "00000000-0000-4000-8000-000000000001"
URL = f"https://chatgpt.com/c/{CHAT}"
ANSWER = {"chatId": CHAT, "chatUrl": URL, "requestedModel": None, "servedModel": "gpt-6-thinking",
          "servedModelDisplay": "GPT-6 Thinking", "fallback": False, "fallbackFrom": None, "fallbackTo": None,
          "prompt": "Reply with exactly: pong", "answer": "pong", "imageCount": 0, "attachments": [], "citations": []}
OUT = {**ANSWER, "images": [], "canvas": None}  # what ask/-p print: fetch's result, saved images, uniform fields
FALLBACK = {**ANSWER, "requestedModel": "gpt-6-thinking", "servedModel": "gpt-6-t-mini", "servedModelDisplay": "GPT-6",
            "fallback": True, "fallbackFrom": "gpt-6-thinking", "fallbackTo": "gpt-6-t-mini"}
STATUS = {"signedIn": True, "plan": "plus", "email": "o***@example.com"}
MODELS = [{"slug": "gpt-6", "title": "GPT-6", "description": "Flagship", "default": True, "reasoning": "auto",
           "images": True},
          {"slug": "gpt-6-instant", "title": "GPT-6", "description": "Fast", "default": False, "reasoning": "none",
           "images": True}]


def png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    return b"\x89PNG\r\n\x1a\n" + chunk + b"\x00\x00\x00\x00IEND\xaeB`\x82"


def run(*args, env=None, input=None):
    return CliRunner().invoke(llmcli.build(), ["chatgpt", *args], env=env, input=input)


def last_chat():
    return config.home() / "sites" / "chatgpt" / "chatgpt-default" / "last_chat"


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


def test_ask_json_when_piped(monkeypatch, tmp_path):
    calls = stub(monkeypatch, "ask", ANSWER)
    result = run("ask", "Reply with exactly: pong")
    assert result.exit_code == 0
    assert json.loads(result.stdout) == OUT
    assert result.stderr == ""
    assert calls == [(("Reply with exactly: pong",), {"chat_id": None, "want_images": False, "model": None,
                                                     "files": [], "timeout": 180, "headed": False})]
    assert last_chat().read_text() == CHAT + "\n"


def test_ask_json_shape_has_every_documented_field(monkeypatch):
    stub(monkeypatch, "ask", ANSWER)
    keys = set(json.loads(run("ask", "q").stdout))
    assert {"chatId", "chatUrl", "requestedModel", "servedModel", "servedModelDisplay", "fallback", "fallbackFrom",
            "fallbackTo", "prompt", "answer", "imageCount", "images", "attachments", "citations"} <= keys


def test_ask_text_prints_only_the_answer(monkeypatch):
    stub(monkeypatch, "ask", ANSWER)
    result = run("ask", "q", "--format", "text")
    assert (result.exit_code, result.stdout) == (0, "pong\n")


def test_options_reach_fetch(monkeypatch, tmp_path):
    attachment = tmp_path / "notes.txt"
    attachment.write_text("hello")
    calls = stub(monkeypatch, "ask", ANSWER)
    result = run("ask", "Summarize", "-m", "gpt-6-instant", "--chat", URL, "--file", str(attachment),
                 "--timeout", "42", "--headed")
    assert result.exit_code == 0
    assert calls[0][1] == {"chat_id": CHAT, "want_images": False, "model": "gpt-6-instant",
                           "files": [str(attachment.resolve())], "timeout": 42, "headed": True}


def test_fallback_warns_and_exits_0(monkeypatch):
    stub(monkeypatch, "ask", FALLBACK)
    result = run("-p", "--output-format", "json", "-m", "gpt-6-thinking", "q")
    assert result.exit_code == 0
    out = json.loads(result.stdout)
    assert (out["fallback"], out["fallbackFrom"], out["fallbackTo"]) == (True, "gpt-6-thinking", "gpt-6-t-mini")
    assert "warning: asked for gpt-6-thinking, but ChatGPT answered with gpt-6-t-mini (fallback)." in result.stderr


def test_strict_model_turns_a_fallback_into_exit_6(monkeypatch):
    stub(monkeypatch, "ask", FALLBACK)
    result = run("-p", "--output-format", "json", "--strict-model", "-m", "gpt-6-thinking", "q")
    assert result.exit_code == 6
    out = json.loads(result.stdout)  # the JSON result still prints first
    assert (out["error"], out["code"]) == ("model_fallback", 6)
    error = json.loads(result.stderr.strip().splitlines()[-1])
    assert (error["error"], error["code"], error["requestedModel"], error["servedModel"]) == \
        ("model_fallback", 6, "gpt-6-thinking", "gpt-6-t-mini")


def test_strict_model_without_a_fallback_is_ok(monkeypatch):
    stub(monkeypatch, "ask", {**ANSWER, "requestedModel": "gpt-6-thinking"})
    assert run("-p", "--strict-model", "-m", "gpt-6-thinking", "q").exit_code == 0


def test_a_cap_notice_as_the_answer_exits_6_with_resets_at(monkeypatch):
    notice = "You've hit the Plus plan limit for image generations requests. You can create more images when the " \
             "limit resets in 2 hours."
    stub(monkeypatch, "ask", {**ANSWER, "answer": notice})
    result = run("ask", "--format", "json", "q")
    assert result.exit_code == 6
    out = json.loads(result.stdout)
    assert (out["error"], out["code"], out["message"]) == ("rate_limited", 6, notice)
    error = json.loads(result.stderr)
    assert error["error"] == "rate_limited" and error["resets_at"] == out["resets_at"]
    assert error["resets_at"].endswith(("-05:00", "-06:00"))


def test_a_refusal_raised_by_fetch_exits_6(monkeypatch):
    stub(monkeypatch, "ask", cli.fetch.RateLimited("You've reached our limit of messages per hour.", sent=True,
                                                   resets_at="2026-10-18T15:00:00-05:00"))
    result = run("-p", "q")
    assert result.exit_code == 6
    error = json.loads(result.stderr)
    assert error == {"error": "rate_limited", "code": 6, "message": "You've reached our limit of messages per hour.",
                     "resets_at": "2026-10-18T15:00:00-05:00", "hint": error["hint"]}
    assert "check `llmcli chatgpt chats list`" in error["hint"]
    assert "until 2026-10-18T15:00:00-05:00:" in error["hint"]  # a time is no token, even before a colon


def test_a_challenge_in_hidden_chrome_exits_4_and_never_opens_a_window(monkeypatch):
    for command in (("status",), ("models",), ("chats", "list"), ("-p", "q")):
        calls = stub(monkeypatch, {"status": "status", "models": "models", "chats": "chats", "-p": "ask"}[command[0]],
                     Challenged("headless"))
        result = run(*command)
        assert result.exit_code == 4, command
        error = json.loads(result.stderr)
        assert error["error"] == "hidden_browser_blocked", command
        assert error["message"] == f"Cloudflare needs a visible check: {cli.LOGIN_HINT}"
        assert "llmcli chatgpt login" in error["hint"] and "No window is ever shown" in error["hint"]
        assert [kwargs["headed"] for _args, kwargs in calls] == [False]  # one hidden try, no visible retry


def test_a_challenge_with_headed_exits_4_as_cloudflare_challenge(monkeypatch):
    calls = stub(monkeypatch, "models", Challenged("visible"))
    result = run("models", "--headed")
    assert result.exit_code == 4 and json.loads(result.stderr)["error"] == "cloudflare_challenge"
    assert calls[0][1]["headed"] is True


def test_image_out_saves_and_reports_original(monkeypatch, tmp_path):
    data = png(1254, 1254)
    stub(monkeypatch, "ask", {**ANSWER, "answer": "", "imageCount": 1,
                              "imageData": [{"data": data, "sourceWidth": 1254, "sourceHeight": 1254,
                                             "sourceBytes": len(data)}]})
    target = tmp_path / "stars.png"
    result = run("ask", "--format", "json", "--image-out", str(target), "Make stars")
    assert result.exit_code == 0
    out = json.loads(result.stdout)
    assert out["images"] == [{"path": str(target.resolve()), "width": 1254, "height": 1254, "bytes": len(data),
                              "mime": "image/png", "original": True, "sourceWidth": 1254, "sourceHeight": 1254,
                              "source": "original"}]
    assert target.read_bytes() == data and "imageData" not in out


def test_image_out_without_an_image_exits_5_after_printing_json(monkeypatch, tmp_path):
    stub(monkeypatch, "ask", {**ANSWER, "answer": "I can't make that.", "imageData": []})
    result = run("-p", "--output-format", "json", "--image-out", str(tmp_path / "out/"), "q")
    assert result.exit_code == 5
    assert json.loads(result.stdout)["error"].startswith("ChatGPT answered without an image")


def test_an_image_without_image_out_gets_a_hint(monkeypatch):
    stub(monkeypatch, "ask", {**ANSWER, "answer": "", "imageCount": 1})
    out = json.loads(run("ask", "q").stdout)
    assert out["images"] == [] and "rerun with --image-out" in out["imageHint"]
    text = run("ask", "q", "--format", "text").stdout
    assert text.startswith("[ChatGPT made 1 image:")


def test_image_timeout_default(monkeypatch, tmp_path):
    calls = stub(monkeypatch, "ask", {**ANSWER, "imageCount": 1, "imageData": [
        {"data": png(2, 2), "sourceWidth": 2, "sourceHeight": 2, "sourceBytes": None}]})
    run("-p", "--image-out", str(tmp_path), "q")
    assert calls[0][1]["timeout"] == 600 and calls[0][1]["want_images"] is True


def test_print_mode_writes_output_file_and_continues_the_last_chat(monkeypatch, tmp_path):
    calls = stub(monkeypatch, "ask", ANSWER)
    out = tmp_path / "answer.txt"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Reply with exactly: pong\n")
    result = run("--lock-wait", "5", "-p", "--quiet", "--prompt-file", str(prompt), "--output-file", str(out))
    assert result.exit_code == 0 and result.stdout == "" and out.read_text() == "pong\n"
    assert browser.lock_wait == 5
    run("-p", "-c", "again")
    assert calls[-1][1]["chat_id"] == CHAT


def test_continue_without_a_previous_chat_is_a_usage_error(monkeypatch):
    stub(monkeypatch, "ask", ANSWER)
    result = run("-p", "-c", "q")
    assert result.exit_code == 2 and "No chat to continue" in result.stderr


@pytest.mark.parametrize("args", [("ask", "--chat", "nope", "q"), ("-p", "--chat", CHAT, "-c", "q"),
                                  ("ask", "--new", "--chat", CHAT, "q"), ("-p", ""), ("ask", "  ")])
def test_usage_errors_exit_2_with_json(monkeypatch, args):
    stub(monkeypatch, "ask", ANSWER)
    result = run(*args)
    assert result.exit_code == 2
    assert json.loads(result.stderr)["code"] == 2


def test_file_over_the_image_limit_is_refused_before_chrome_opens(monkeypatch, tmp_path):
    calls = stub(monkeypatch, "ask", ANSWER)
    big = tmp_path / "huge.png"
    with open(big, "wb") as handle:
        handle.truncate(cli.MAX_IMAGE_BYTES + 1)
    result = run("ask", "--file", str(big), "q")
    assert result.exit_code == 2 and "20 MB per image" in result.stderr and calls == []


def test_status_prints_only_signed_in_plan_and_masked_email(monkeypatch):
    stub(monkeypatch, "status", STATUS)
    result = run("status")
    assert (result.exit_code, json.loads(result.stdout)) == (0, {"identity": "chatgpt:default", **STATUS})
    stub(monkeypatch, "status", {"signedIn": False, "plan": None, "email": None})
    result = run("status")
    assert result.exit_code == 4 and json.loads(result.stdout)["signedIn"] is False


@pytest.mark.parametrize(("error", "code"), [(AuthRequired("x"), 4), (cli.fetch.NotFound("Chat x not found."), 3),
                                             (ProfileBusy("busy"), 5), (RuntimeError("boom"), 5)])
def test_errors_map_to_exit_codes(monkeypatch, error, code):
    stub(monkeypatch, "chat", error)
    result = run("chats", "show", CHAT)
    assert result.exit_code == code and json.loads(result.stderr)["code"] == code


def test_errors_never_print_a_token(monkeypatch):
    token = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlLWZha2U"
    stub(monkeypatch, "models", RuntimeError(f"failed with Authorization: Bearer {token}"))
    result = run("models")
    assert token not in result.stderr and result.exit_code == 5


def test_models_and_chats_list(monkeypatch):
    stub(monkeypatch, "models", MODELS)
    assert json.loads(run("models", "--compact").stdout) == [
        {"slug": "gpt-6", "title": "GPT-6", "default": True}, {"slug": "gpt-6-instant", "title": "GPT-6", "default": False}]
    stub(monkeypatch, "chats", [])
    assert run("chats", "list").exit_code == 3


def test_no_session_means_exit_4(monkeypatch):
    monkeypatch.setattr(Identity, "profile_exists", lambda self: False)
    result = run("-p", "q")
    assert result.exit_code == 4 and "llmcli chatgpt login" in result.stderr


def test_agent_help_lists_commands_exit_codes_and_env():
    ref = json.loads(run("agent-help", "--format", "json").stdout)
    names = {c["name"] for c in ref["commands"]}
    assert {"login", "status", "models", "ask", "-p", "chats list", "chats show", "agent-help"} <= names
    assert set(ref["exitCodes"]) >= {"0", "2", "3", "4", "5", "6", "7"}
    assert "WEBCLI_DAEMON" in ref["env"] and not any("HEADLESS" in name for name in ref["env"])
    assert any("hidden_browser_blocked" in n for n in ref["notes"]) and "serve" in names
    flags = {f for c in ref["commands"] if c["name"] == "-p" for o in c["options"] for f in o["flags"]}
    assert {"-m", "--model", "--strict-model", "--image-out", "--file", "--chat", "-c", "--prompt-file",
            "--output-file", "--output-format", "--timeout"} <= flags
