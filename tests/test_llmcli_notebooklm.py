"""llmcli notebooklm: NotebookLM's ask as an llmcli site. The CLI over stubbed fetch ops (the uniform ask JSON,
-n by id / URL / title, stdin and --prompt-file, --source, --chat and -c, exit codes), the ops it may run (reads
and ask only), and fetch's title lookup and conversation id over a scripted page."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from webcli.apps import llmcli
from webcli.core import browser, config
from webcli.core.errors import AuthRequired, NotFound, RateLimited, UsageError
from webcli.core.identities import Identity
from webcli.sites import notebooklm as site_pkg
from webcli.sites.notebooklm import chat, endpoints as ep, fetch, guard, parse

from google_fakes import ScriptedPage, install

FIX = Path(__file__).parent / "fixtures" / "notebooklm"
NB = "11111111-2222-4333-8444-555555555555"
S1 = "aaaaaaaa-1111-4111-8111-111111111111"
CONV = "c0c0c0c0-1111-4111-8111-111111111111"
GOT = {"notebook_id": NB, "notebook": "Synthetic research notebook", "question": "Q?", "answer": "Yes [1].",
       "citations": [{"number": 1, "source_id": S1, "source_title": "Doc one", "excerpt": "First para."}],
       "conversation_id": CONV}
UNIFORM = {"answer", "chatId", "requestedModel", "servedModel", "servedModelDisplay", "fallback", "images",
           "citations", "canvas"}


def run(*args, env=None, input=None):
    return CliRunner().invoke(llmcli.build(), ["notebooklm", *args], env=env, input=input)


def last_chat() -> Path:
    return config.home() / "sites" / "notebooklm" / "google-default" / "last_chat"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.setattr(browser, "lock_wait", 30.0)
    monkeypatch.setattr(Identity, "profile_exists", lambda self: True)
    monkeypatch.delenv(chat.NOTEBOOK_ENV, raising=False)


def stub(monkeypatch, name, result):
    calls = []

    async def fake(*args, **kwargs):
        calls.append((args, kwargs))
        if isinstance(result, BaseException):
            raise result
        return copy.deepcopy(result)

    monkeypatch.setattr(fetch, name, fake)
    return calls


# Registration and the ops it may run.

def test_llmcli_lists_notebooklm_and_webcli_keeps_it():
    from webcli.apps import webcli as webcli_app

    assert "notebooklm" in llmcli.build().commands and "notebooklm" in webcli_app.build().commands
    assert site_pkg.ADAPTER.kind == "data" and site_pkg.ADAPTER.describe()["alsoLlmcli"] is True
    ref = json.loads(CliRunner().invoke(llmcli.build(), ["agent-help", "--format", "json"]).stdout)
    assert any(c["name"] == "notebooklm ask" for c in ref["commands"])


def test_only_reads_and_ask_no_studio_outputs():
    assert set(chat.group.commands) == {"status", "notebooks", "ask", "-p", "models", "login", "serve", "agent-help"}
    assert chat.OPS == {"status", "notebooks", "ask"} and chat.OPS <= set(site_pkg.ADAPTER.ops)
    with pytest.raises(RuntimeError, match="does not run 'generate'"):
        chat._call("generate", "report", NB, None, {}, confirm=True)


def test_the_ask_policy_allows_reads_and_the_chat_but_no_create_or_edit():
    def batch(rpcid):
        from urllib.parse import urlencode

        url = f"https://{ep.APP_HOST}{ep.BATCH_PATH}?" + urlencode({"rpcids": rpcid, "source-path": "/"})
        return url, urlencode({"f.req": json.dumps([[[rpcid, "[]", None, "generic"]]]), "at": "FAKE"})

    assert guard.request_allowed("POST", f"https://{ep.APP_HOST}{ep.QUERY_PATH}?rt=c", ask=True)
    assert all(guard.request_allowed("POST", *batch(r), ask=True) for r in ep.READ_RPCIDS)
    assert not guard.request_allowed("POST", *batch(ep.RPC["create_artifact"]), ask=True)
    assert not any(guard.request_allowed("POST", *batch(r), ask=True) for r in ep.MUTATING_RPCIDS)


# ask and -p.

def test_ask_json_has_every_uniform_field_and_notebooklm_citations(monkeypatch):
    calls = stub(monkeypatch, "ask", GOT)
    result = run("ask", "-n", f"https://notebooklm.google.com/notebook/{NB}", "Q?", "--json")
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert UNIFORM <= set(out) and out["chatId"] == CONV and out["servedModel"] is None and out["images"] == []
    assert out["citations"] == [{"number": 1, "title": "Doc one", "url": None, "source_id": S1,
                                 "source_title": "Doc one", "excerpt": "First para."}]
    assert calls == [((NB, "Q?", None), {"conversation_id": None, "timeout": 180, "headed": False})]


def test_ask_text_reads_stdin_passes_title_sources_and_chat(monkeypatch):
    calls = stub(monkeypatch, "ask", GOT)
    result = run("ask", "-", "-n", "Synthetic research notebook", "--source", S1, "--chat", CONV, "--timeout",
                 "60", "--format", "text", input="line one\nline two\n")
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("Yes [1].\n\nCitations:\n  [1] Doc one")
    assert calls[0] == (("Synthetic research notebook", "line one\nline two", [S1]),
                        {"conversation_id": CONV, "timeout": 60, "headed": False})


def test_the_notebook_env_is_the_default(monkeypatch):
    calls = stub(monkeypatch, "ask", GOT)
    assert run("ask", "Q?", env={chat.NOTEBOOK_ENV: NB}).exit_code == 0
    assert calls[0][0][0] == NB


@pytest.mark.parametrize("args", [
    ("ask", "Q?"),  # no notebook
    ("ask", "-n", NB, "   "),
    ("ask", "-n", NB, "Q?", "--model", "x"),
    ("ask", "-n", NB, "Q?", "--timeout", "5"),
    ("-p", "-n", NB, "--chat", CONV, "-c", "q"),
    ("-p", "-n", NB, ""),
    ("-p", "-c", "q"),  # nothing to continue yet
    ("generate", "report", NB),
])
def test_usage_errors_exit_2_before_any_browser_work(monkeypatch, args):
    calls = stub(monkeypatch, "ask", GOT)
    result = run(*args, input="")
    assert result.exit_code == 2, result.output
    assert calls == []


def test_print_mode_stdout_is_only_the_answer_and_files_work(monkeypatch, tmp_path):
    calls = stub(monkeypatch, "ask", GOT)
    result = run("-p", "-n", NB, "What", "is", "it?", env={"LLMCLI_FORMAT": "json"})
    assert (result.exit_code, result.stdout, result.stderr) == (0, "Yes [1].\n", f"chat: {CONV}\n")
    assert calls[-1][0] == (NB, "What is it?", None)
    prompt, out = tmp_path / "prompt.txt", tmp_path / "answer.txt"
    prompt.write_text("From a file\n")
    result = run("--lock-wait", "9", "-p", "-q", "-n", NB, "--prompt-file", str(prompt), "--output-file", str(out),
                 "--output-format", "json")
    assert (result.exit_code, result.stdout, result.stderr) == (0, "", "")
    assert calls[-1][0][1] == "From a file" and json.loads(out.read_text())["citations"][0]["source_id"] == S1
    assert browser.lock_wait == 9.0


def test_continue_reuses_the_last_notebook_and_conversation(monkeypatch):
    calls = stub(monkeypatch, "ask", GOT)
    assert run("-p", "-n", "Synthetic research notebook", "Q?").exit_code == 0
    assert json.loads(last_chat().read_text()) == {"notebook_id": NB, "conversation_id": CONV}
    assert run("-p", "-c", "And then?").exit_code == 0
    assert calls[-1] == ((NB, "And then?", None), {"conversation_id": CONV, "timeout": 180, "headed": False})
    assert run("-p", "-c", "-n", "99999999-2222-4333-8444-555555555555", "x").exit_code == 2


@pytest.mark.parametrize(("exc", "code"), [(AuthRequired("signed out"), 4), (NotFound("no notebook"), 3),
                                           (RateLimited("limit", sent=True), 6), (UsageError("two titles"), 2)])
def test_failures_keep_stdout_empty_with_their_exit_codes(monkeypatch, exc, code):
    stub(monkeypatch, "ask", exc)
    result = run("-p", "-n", NB, "hi")
    assert (result.exit_code, result.stdout) == (code, "")


def test_models_is_empty_and_opens_no_browser(monkeypatch):
    stub(monkeypatch, "ask", GOT)
    result = run("models", "--format", "json")
    assert result.exit_code == 0 and json.loads(result.stdout) == []


# fetch: a title resolves to its notebook; --chat rides as the conversation id.

@pytest.fixture
def page(monkeypatch):
    browser.identity = Identity("google:default")
    names = {"wXbhsf": "notebooks", "rLM1Ne": "notebook"}
    calls = []

    def call(arg):
        calls.append(arg)
        name = "answer" if arg["path"] == ep.QUERY_PATH else names[arg["rpcid"]]
        return {"status": 200, "contentType": "application/json", "body": (FIX / f"{name}.txt").read_text()}

    scripted = ScriptedPage(answers={"SNlM0e": call})
    scripted.calls = calls
    return install(monkeypatch, scripted)


def test_a_title_resolves_and_the_conversation_id_is_sent(page):
    got = browser.run(fetch.ask("synthetic RESEARCH notebook", "Q?", conversation_id=CONV, timeout=30))
    assert page.policy is guard.ASK
    assert [c["rpcid"] for c in page.calls] == ["wXbhsf", "rLM1Ne", None]
    params = json.loads(json.loads(page.calls[-1]["freq"])[1])
    assert params[4] == CONV and params[7] == NB and page.calls[-1]["timeoutMs"] == 30_000
    assert got["notebook_id"] == NB and got["conversation_id"] == "conv-1"


def test_an_id_skips_the_lookup_and_an_unknown_title_is_not_found(page):
    browser.run(fetch.ask(NB, "Q?"))
    assert [c["rpcid"] for c in page.calls] == ["rLM1Ne", None]
    assert json.loads(json.loads(page.calls[-1]["freq"])[1])[4] is None
    with pytest.raises(NotFound):
        browser.run(fetch.ask("No such notebook", "Q?"))


def test_two_notebooks_with_one_title_need_the_id():
    rows = [{"id": "a", "title": "Same"}, {"id": "b", "title": "Same"}, {"id": "c", "title": "same"}]
    assert [r["id"] for r in parse.notebook_by_title(rows, " Same ")] == ["a", "b"]  # exact wins over casefold
    assert [r["id"] for r in parse.notebook_by_title(rows, "SAME")] == ["a", "b", "c"]
    assert parse.notebook_by_title(rows, "other") == []
