"""Required (5): JSON when stdout is piped, and the same llm JSON fields from every site, with a mocked site
response. Also the formats (md, csv, the format env) and the tmuxlet -p contract."""

from __future__ import annotations

import copy
import json

import pytest
from click.testing import CliRunner

from webcli.apps import llmcli
from webcli.core import output
from webcli.sites.chatgpt import fetch as chatgpt_fetch
from webcli.sites.gemini import fetch as gemini_fetch
from conftest import make_profile

UNIFORM = {"answer", "chatId", "requestedModel", "servedModel", "servedModelDisplay", "fallback", "images",
           "citations", "canvas"}
# What each site's fetch.ask returns (the shapes from the ported fetch code), minus the uniform fields it lacks.
GEMINI_ASK = {"chatId": "7e6d5c4b3a291807", "chatUrl": "https://gemini.google.com/app/7e6d5c4b3a291807",
              "model": "3.8 Flash", "thinking": "high", "prompt": "q", "answer": "Four.[^1]\n\n[^1]: notes.md",
              "citations": [{"n": 1, "label": "MD", "file": "notes.md"}], "imageCount": 0,
              "canvas": {"title": "Doc", "markdown": "# Doc"}}
CHATGPT_ASK = {"chatId": "68f3c1a2-1234-8000-9abc-0123456789ab", "chatUrl": "https://chatgpt.com/c/68f3c1a2",
               "requestedModel": "gpt-6", "servedModel": "gpt-6-thinking", "servedModelDisplay": "GPT-6 Thinking",
               "fallback": False, "fallbackFrom": None, "fallbackTo": None, "prompt": "q", "answer": "Four.",
               "imageCount": 0, "attachments": [], "citations": []}


@pytest.fixture(autouse=True)
def stubs(monkeypatch):
    make_profile("google:default")
    make_profile("chatgpt:default")
    for module, answer in ((gemini_fetch, GEMINI_ASK), (chatgpt_fetch, CHATGPT_ASK)):
        async def ask(*args, _answer=answer, **kwargs):
            return copy.deepcopy(_answer)

        monkeypatch.setattr(module, "ask", ask)

    async def chats(limit, **kwargs):
        return [{"id": "1a2b3c4d5e6f7a8b", "title": "Trip | ideas", "updated": "2026-10-19T09:00:00Z",
                 "pinned": True, "url": "https://gemini.google.com/app/1a2b3c4d5e6f7a8b"}]

    monkeypatch.setattr(gemini_fetch, "chats", chats)


def run(*args, env=None, input=None):
    return CliRunner().invoke(llmcli.build(), list(args), env=env, input=input)


@pytest.mark.parametrize("site", ["gemini", "chatgpt"])
def test_piped_ask_is_json_with_every_uniform_field(site):
    result = run(site, "ask", "q")  # CliRunner's stdout is not a TTY: piped
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert UNIFORM <= set(data)
    assert isinstance(data["images"], list) and isinstance(data["citations"], list)
    assert isinstance(data["fallback"], bool)


def test_the_uniform_fields_carry_each_sites_values():
    gemini = json.loads(run("gemini", "ask", "-m", "flash", "q").stdout)
    assert {k: gemini[k] for k in UNIFORM} == {
        "answer": "Four.[^1]\n\n[^1]: notes.md", "chatId": "7e6d5c4b3a291807", "requestedModel": "flash",
        "servedModel": "3.8 Flash", "servedModelDisplay": "3.8 Flash", "fallback": False, "images": [],
        "citations": [{"n": 1, "label": "MD", "file": "notes.md"}],
        "canvas": {"title": "Doc", "markdown": "# Doc", "chars": 5, "path": None}}
    chatgpt = json.loads(run("chatgpt", "ask", "q").stdout)
    assert {k: chatgpt[k] for k in UNIFORM} == {
        "answer": "Four.", "chatId": "68f3c1a2-1234-8000-9abc-0123456789ab", "requestedModel": "gpt-6",
        "servedModel": "gpt-6-thinking", "servedModelDisplay": "GPT-6 Thinking", "fallback": False, "images": [],
        "citations": [], "canvas": None}


@pytest.mark.parametrize("site", ["gemini", "chatgpt"])
def test_print_mode_json_has_the_same_fields(site):
    data = json.loads(run(site, "-p", "--output-format", "json", "q").stdout)
    assert UNIFORM <= set(data)


@pytest.mark.parametrize("site", ["gemini", "chatgpt"])
def test_print_mode_keeps_the_tmuxlet_contract(site, tmp_path):
    """tmuxlet runs `<site> --lock-wait N -p --quiet -c --prompt-file P --output-file A` with the format env set to
    json (for JSON errors) and reads the answer TEXT from A."""
    prompt, answer = tmp_path / "prompt.txt", tmp_path / "answer.txt"
    prompt.write_text("What is 2+2?\n")
    first = run(site, "-p", "--quiet", "--prompt-file", str(prompt), "--output-file", str(answer),
                env={"LLMCLI_FORMAT": "json"})
    assert first.exit_code == 0 and first.stdout == "" and first.stderr == ""
    assert answer.read_text().startswith("Four.")
    again = run(site, "--lock-wait", "1800", "-p", "--quiet", "-c", "--prompt-file", str(prompt),
                "--output-file", str(answer), env={"LLMCLI_FORMAT": "json"})
    assert again.exit_code == 0 and answer.read_text().startswith("Four.")
    dash = run(site, "-p", "--prompt-file", "-", "--output-file", "-", "-q", input="hi")
    assert dash.exit_code == 0 and dash.stdout.startswith("Four.")


def test_md_and_csv_formats_and_the_format_env():
    md = run("gemini", "chats", "list", "--format", "md").stdout
    assert md.splitlines()[0] == "| id | title | updated | pinned | url |" and "Trip \\| ideas" in md
    assert run("gemini", "chats", "list", "--csv").stdout.startswith("id,title,updated,pinned,url\n")
    assert run("gemini", "chats", "list", "--format", "csv").stdout.startswith("id,title,")
    assert run("gemini", "chats", "list", env={"LLMCLI_FORMAT": "md"}).stdout.startswith("| id |")
    assert json.loads(run("gemini", "chats", "list", "--format", "json", env={"LLMCLI_FORMAT": "md"}).stdout)
    text = run("gemini", "ask", "q", "--format", "md").stdout
    assert text.startswith("Four.") and not text.startswith("{")  # md is the answer as-is (it is markdown)
    assert run("gemini", "ask", "q", env={"LLMCLI_FORMAT": "text"}).stdout.startswith("Four.")


def test_resolve_format_order(monkeypatch):
    monkeypatch.setattr(output, "FORMAT_ENV", "LLMCLI_FORMAT")
    monkeypatch.setenv("LLMCLI_FORMAT", "table")
    assert output.resolve_format("json") == "json"  # an explicit --format wins
    assert output.resolve_format(None, human="text") == "text"  # table/text in the env mean "human"
    monkeypatch.setenv("LLMCLI_FORMAT", "md")
    assert output.resolve_format(None) == "md"
    monkeypatch.delenv("LLMCLI_FORMAT")
    assert output.resolve_format(None) == "json"  # pytest's stdout is not a TTY


def test_dry_run_is_the_default_for_writes(capsys):
    assert output.confirm_write(False, {"would": "label 3 contacts"}) is False
    assert json.loads(capsys.readouterr().out)["dryRun"] is True
    assert output.confirm_write(True, {}, dry_run=True) is False
    assert output.confirm_write(True, {}) is True
