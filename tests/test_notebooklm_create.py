"""webcli notebooklm create / source add: the request builders (CreateProject, AddSources for URLs / YouTube / text,
the file register and upload start body), the readers over synthetic fixtures (make.py), the budgeted write guard
(each command opens only its own create / add / upload calls, and rename, delete, share, source edits, and notes stay
blocked), the fetch code over a scripted page (call order, the in-page upload's arguments), the CLI over a faked
daemon (dry runs exit 2, --confirm, --wait, validation), and that llmcli notebooklm can't reach any of it."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from click.testing import CliRunner

from webcli.apps import webcli as webcli_app
from webcli.core import browser, daemon
from webcli.core.errors import UsageError
from webcli.core.identities import Identity
from webcli.sites.notebooklm import chat, cli, endpoints as ep, fetch, guard, parse

from google_fakes import ScriptedPage, install

FIX = Path(__file__).parent / "fixtures" / "notebooklm"
NB = "11111111-2222-4333-8444-555555555555"
S1, S2 = "aaaaaaaa-1111-4111-8111-111111111111", "bbbbbbbb-2222-4222-8222-222222222222"
NEW_NB, NEW_SRC, NEW_FILE = ("ffffffff-1111-4111-8111-11111111111" + c for c in "123")
CREATE, ADD, REGISTER = "CCqFvf", "izAoDd", "o4cbdc"
UPLOAD_URL = f"https://{ep.APP_HOST}{ep.UPLOAD_PATH}"
# Writes that must stay blocked whatever create / source add opens.
STILL_BLOCKED = sorted(set(ep.MUTATING_RPCIDS) - ep.WRITE_RPCIDS)


def fixture(name: str) -> str:
    return (FIX / f"{name}.txt").read_text()


def payload(name: str, rpcid: str):
    return parse.rpc_payload(fixture(name), rpcid)


# Builders.

def test_create_notebook_params_end_in_the_request_context():
    assert parse.create_notebook_params("My notes") == ["My notes", None, None, parse.CONTEXT]
    assert parse.CONTEXT == [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]]


def test_url_sources_put_web_pages_at_2_and_youtube_at_7():
    web = parse.url_source_params(NB, "https://example.com/a")
    assert web == [[[None, None, ["https://example.com/a"], None, None, None, None, None, None, None, 1]], NB,
                   parse.CONTEXT]
    for url in ("https://www.youtube.com/watch?v=abc", "https://youtu.be/abc", "https://m.youtube.com/watch?v=x"):
        spec = parse.url_source_params(NB, url)[0][0]
        assert spec[7] == [url] and spec[2] is None and spec[10] == 1 and len(spec) == 11
    assert parse.url_source_params(NB, "https://notyoutube.com/x")[0][0][2] == ["https://notyoutube.com/x"]


def test_text_source_and_file_register_params():
    assert parse.text_source_params(NB, "Title", "Body") == [
        [[None, ["Title", "Body"], None, 2, None, None, None, None, None, None, 1]], NB, parse.CONTEXT]
    assert parse.register_file_params(NB, "notes.md") == [[["notes.md"]], NB, parse.CONTEXT]
    assert json.loads(parse.upload_start_body(NB, "notes.md", NEW_FILE)) == {
        "PROJECT_ID": NB, "SOURCE_NAME": "notes.md", "SOURCE_ID": NEW_FILE}


def test_budget_counts_one_call_per_item():
    items = [{"kind": "file"}, {"kind": "file"}, {"kind": "url"}, {"kind": "text"}]
    assert parse.write_budget(items) == {ADD: 2, REGISTER: 2, ep.UPLOAD: 4}
    assert parse.write_budget([], create=True) == {CREATE: 1}
    assert parse.write_budget([{"kind": "url"}], create=True) == {CREATE: 1, ADD: 1}


# Readers.

def test_created_notebook_reads_id_title_url():
    assert parse.created_notebook(payload("new_notebook", CREATE)) == {
        "id": NEW_NB, "title": "New synthetic notebook", "url": f"https://notebooklm.google.com/notebook/{NEW_NB}"}
    assert parse.created_notebook([payload("new_notebook", CREATE)])["id"] == NEW_NB  # one extra wrapper
    assert parse.created_notebook(["t", None, None]) is None


def test_added_source_reads_the_row_at_either_nesting():
    row = parse.added_source(payload("added_source", ADD))
    assert (row["id"], row["title"], row["type"], row["status"]) == (NEW_SRC, "Example page", "web_page",
                                                                     "processing")
    assert parse.added_source(payload("added_source", ADD)[0])["id"] == NEW_SRC
    assert parse.added_source(None) is None and parse.added_source([[None]]) is None


def test_registered_source_id_needs_exactly_one_id():
    assert parse.registered_source_id(payload("registered", REGISTER), "a.md") == NEW_FILE
    assert parse.registered_source_id([[NEW_FILE, "a.md"]], "a.md") == NEW_FILE
    assert parse.registered_source_id([[S1], [S2]], "a.md") is None
    assert parse.registered_source_id([["a.md"]], "a.md") is None


# The guard.

def batch(rpcid: str, label: str | None = None, host: str = ep.APP_HOST) -> tuple[str, str]:
    url = f"https://{host}{ep.BATCH_PATH}?" + urlencode({"rpcids": label or rpcid, "source-path": "/"})
    body = urlencode({"f.req": json.dumps([[[rpcid, "[]", None, "generic"]]]), "at": "FAKEtoken"})
    return url, body


def request(method: str, url: str, body: str | None = None):
    return SimpleNamespace(method=method, url=url, post_data=body)


def test_writes_open_only_create_add_and_register():
    for rpcid in ep.WRITE_RPCIDS:
        assert not guard.request_allowed("POST", *batch(rpcid))
        assert not guard.request_allowed("POST", *batch(rpcid), generate=True)
        assert guard.request_allowed("POST", *batch(rpcid), writes=frozenset({rpcid}))
    assert not guard.request_allowed("POST", *batch(ADD), writes=frozenset({CREATE}))


@pytest.mark.parametrize("rpcid", STILL_BLOCKED)
def test_rename_delete_share_source_edits_and_notes_stay_blocked(rpcid):
    assert not guard.request_allowed("POST", *batch(rpcid), writes=ep.WRITE_RPCIDS, upload=True)
    assert not guard.request_allowed("POST", *batch(rpcid), writes=frozenset({rpcid}))  # not in WRITE_RPCIDS
    policy = guard.write_policy({CREATE: 1, ADD: 5, REGISTER: 5, ep.UPLOAD: 10, rpcid: 5})
    assert not policy.allowed(request("POST", *batch(rpcid)), guard.Guard())


def test_the_named_rename_and_delete_ids_are_blocked():
    for rpcid, what in [("s0tc2d", "rename"), ("WWINqb", "delete notebook"), ("QDyure", "share"),
                        ("tGMBJ", "delete source"), ("b7Wfje", "rename source"), ("FLmJqe", "refresh source"),
                        ("CYK0Xb", "create note"), ("cYAfTb", "edit note"), ("AH0mwd", "delete note")]:
        assert rpcid in STILL_BLOCKED, what


def test_write_policy_spends_a_budget_per_call():
    policy = guard.write_policy({CREATE: 1, ADD: 2})
    ok = lambda *args: policy.allowed(request(*args), guard.Guard())  # noqa: E731
    assert ok("POST", *batch(CREATE)) and not ok("POST", *batch(CREATE))  # one notebook, never two
    assert ok("POST", *batch(ADD)) and ok("POST", *batch(ADD)) and not ok("POST", *batch(ADD))
    assert not ok("POST", *batch(REGISTER)) and not ok("POST", UPLOAD_URL + "?authuser=0")  # no files named
    for _ in range(5):
        assert ok("POST", *batch("rLM1Ne")) and ok("POST", *batch("wXbhsf"))  # reads are not budgeted
    assert not ok("POST", f"https://{ep.APP_HOST}{ep.QUERY_PATH}")  # no chat, no CreateArtifact
    assert not ok("POST", *batch("R7cb6c"))


def test_uploads_only_on_the_app_hosts_upload_path_and_within_budget():
    policy = guard.write_policy({REGISTER: 1, ep.UPLOAD: 2})
    ok = lambda *args: policy.allowed(request(*args), guard.Guard())  # noqa: E731
    assert not ok("POST", "https://evil.example.com/upload/_/?authuser=0")
    assert not ok("POST", f"https://{ep.APP_HOST}/upload/other")
    assert not ok("POST", f"http://{ep.APP_HOST}{ep.UPLOAD_PATH}")
    assert ok("POST", UPLOAD_URL + "?authuser=0") and ok("POST", UPLOAD_URL + "?upload_id=FAKE")
    assert not ok("POST", UPLOAD_URL + "?upload_id=FAKE")  # a third POST: over budget
    assert not guard.request_allowed("POST", UPLOAD_URL)  # never outside a write policy
    assert guard.request_allowed("POST", f"https://notebook.google.com{ep.UPLOAD_PATH}", upload=True)


def test_a_smuggled_write_under_a_read_label_is_still_blocked():
    policy = guard.write_policy({CREATE: 1})
    assert not policy.allowed(request("POST", *batch("WWINqb", label=CREATE)), guard.Guard())


# Fetch, over a scripted page.

ANSWERS = {"rLM1Ne": "notebook", "wXbhsf": "notebooks", CREATE: "new_notebook", ADD: "added_source",
           REGISTER: "registered"}


@pytest.fixture
def page(monkeypatch):
    browser.identity = Identity("google:default")
    calls, uploads = [], []

    def call(arg):
        calls.append(arg)
        queued = scripted.replies.get(arg["rpcid"])
        body = queued.pop(0) if queued else fixture(ANSWERS[arg["rpcid"]])
        return {"status": 200, "contentType": "application/json", "body": body}

    def upload(arg):
        uploads.append(arg)
        return scripted.upload_answer

    scripted = ScriptedPage(answers={"SNlM0e": call, "oPEP7c": "kevin@example.com", "x-goog-upload-command": upload})
    scripted.calls, scripted.uploads = calls, uploads
    scripted.replies = {}  # rpcid -> bodies answered (in order) before falling back to the fixture
    scripted.upload_answer = {"start": 200, "upload": 200, "trusted": True, "final": "final"}
    return install(monkeypatch, scripted)


def sent(call) -> list:
    return json.loads(json.loads(call["freq"])[0][0][1])


def test_create_and_add_need_confirm(page):
    with pytest.raises(UsageError):
        browser.run(fetch.create("T"))
    with pytest.raises(UsageError):
        browser.run(fetch.add_sources(NB, [{"kind": "url", "url": "https://example.com/"}]))
    assert page.calls == [] and page.policy is None


def test_create_alone_sends_one_create_under_a_create_only_policy(page):
    got = browser.run(fetch.create("New synthetic notebook", confirm=True))
    assert got == {"notebook_id": NEW_NB, "title": "New synthetic notebook",
                   "url": f"https://notebooklm.google.com/notebook/{NEW_NB}", "sources": []}
    assert [c["rpcid"] for c in page.calls] == [CREATE]
    assert sent(page.calls[0]) == ["New synthetic notebook", None, None, parse.CONTEXT]
    assert page.policy not in (guard.READ, guard.ASK, guard.GENERATE)
    assert not page.policy.allowed(request("POST", *batch(ADD)), guard.Guard())  # create alone can't add


def test_create_with_sources_adds_them_to_the_new_notebook(page, tmp_path):
    md = tmp_path / "notes.md"
    md.write_text("# Notes\n")
    items = [{"kind": "file", "path": str(md), "name": "notes.md", "content_type": "text/markdown", "bytes": 8},
             {"kind": "url", "url": "https://example.com/"}]
    got = browser.run(fetch.create("New", items, confirm=True))
    assert [c["rpcid"] for c in page.calls] == [CREATE, REGISTER, ADD, "rLM1Ne"]
    assert all(c["sourcePath"] == f"/notebook/{NEW_NB}" for c in page.calls[1:])
    assert sent(page.calls[1]) == [[["notes.md"]], NEW_NB, parse.CONTEXT]
    assert [s["id"] for s in got["sources"]] == [NEW_FILE, NEW_SRC]


def test_source_add_sends_each_item_once_in_order(page, tmp_path):
    md, txt, pdf = tmp_path / "a.md", tmp_path / "b.txt", tmp_path / "c.pdf"
    md.write_text("# A")
    txt.write_text("B")
    pdf.write_bytes(b"%PDF-1.4 fake")
    items = [{"kind": "file", "path": str(p), "name": p.name, "content_type": parse.UPLOAD_TYPES[p.suffix],
              "bytes": p.stat().st_size} for p in (md, txt, pdf)]
    items += [{"kind": "url", "url": "https://example.com/"}, {"kind": "url", "url": "https://youtu.be/abc"},
              {"kind": "text", "title": "Pasted", "text": "Some text"}]
    got = browser.run(fetch.add_sources(NB, items, confirm=True))
    assert [c["rpcid"] for c in page.calls] == ["rLM1Ne", REGISTER, REGISTER, REGISTER, ADD, ADD, ADD, "rLM1Ne"]
    assert sent(page.calls[5])[0][0][7] == ["https://youtu.be/abc"]
    assert sent(page.calls[6])[0][0][1] == ["Pasted", "Some text"]
    assert (got["notebook_id"], got["notebook"], got["url"]) == (NB, "Synthetic research notebook",
                                                                 f"https://notebooklm.google.com/notebook/{NB}")
    assert [(s["kind"], s["error"]) for s in got["sources"]] == [("file", None)] * 3 + [("url", None)] * 2 + [
        ("text", None)]
    # The upload: two POSTs in the page per file, with the file's bytes and type; no token goes in.
    assert len(page.uploads) == 3
    first = page.uploads[0]
    assert first["uploadPath"] == "/upload/_/" and first["authuser"] == "kevin@example.com"
    assert base64.b64decode(first["b64"]) == b"# A" and first["contentType"] == "text/markdown"
    assert json.loads(first["startBody"]) == {"PROJECT_ID": NB, "SOURCE_NAME": "a.md", "SOURCE_ID": NEW_FILE}
    assert page.uploads[2]["contentType"] == "application/pdf" and "at" not in first
    assert page.policy.allowed(request("POST", UPLOAD_URL + "?authuser=0"), guard.Guard())


def test_a_failed_upload_is_that_items_error_and_the_rest_still_go(page, tmp_path):
    md = tmp_path / "a.md"
    md.write_text("x")
    page.upload_answer = {"start": 200, "upload": None, "trusted": False}
    items = [{"kind": "file", "path": str(md), "name": "a.md", "content_type": "text/markdown", "bytes": 1},
             {"kind": "url", "url": "https://example.com/"}]
    got = browser.run(fetch.add_sources(NB, items, confirm=True))
    bad, good = got["sources"]
    assert bad["id"] is None and bad["status"] == "failed" and "not on its own upload path" in bad["error"]
    assert good["id"] == NEW_SRC and good["error"] is None


def test_plan_sources_is_read_only(page):
    got = browser.run(fetch.plan_sources(NB, [{"kind": "url", "url": "https://example.com/"}]))
    assert page.policy is guard.READ and [c["rpcid"] for c in page.calls] == ["rLM1Ne"]
    assert got["existing_sources"] == 2 and got["calls"] == {ADD: 1}


def status_reply(rpcid: str, code: int) -> str:
    """batchexecute's answer when an RPC fails: no payload, the status code at index 5."""
    return ")]}'\n\n" + json.dumps([["wrb.fr", rpcid, None, None, None, [code], "generic"]])


def test_status_reply_is_what_parse_reads_as_an_error_code():
    with pytest.raises(parse.RpcError) as caught:
        parse.rpc_payload(status_reply(ADD, 9), ADD)
    assert caught.value.code == 9


@pytest.mark.parametrize("kind", ["url", "text", "file"])
def test_rejection_messages_name_the_code_and_what_to_try(kind):
    message = parse.source_rejected_message(9, kind)
    assert message.startswith("NotebookLM couldn't import this source (code 9).")
    assert "API may have changed" not in message
    if kind == "url":
        assert message == ("NotebookLM couldn't import this source (code 9). The page may be paywalled or blocking "
                           "imports. Try uploading a PDF or pasting the text instead.")


def test_a_refused_url_is_that_sources_failure_and_the_rest_still_go(page):
    page.replies[ADD] = [status_reply(ADD, 9)]
    items = [{"kind": "url", "url": "https://paywalled.example.com/article"},
             {"kind": "url", "url": "https://open.example.org/page"}]
    got = browser.run(fetch.add_sources(NB, items, confirm=True))
    refused, added = got["sources"]
    assert refused == {"kind": "url", "input": "https://paywalled.example.com/article", "type": "web_page",
                       "id": None, "title": None, "status": "failed",
                       "error": parse.source_rejected_message(9, "url")}
    assert added["id"] == NEW_SRC and added["error"] is None
    assert [c["rpcid"] for c in page.calls] == ["rLM1Ne", ADD, ADD, "rLM1Ne"]


@pytest.mark.parametrize("code", sorted(parse.SOURCE_REJECT_CODES))
def test_each_known_rejection_code_is_per_source(page, code, tmp_path):
    md = tmp_path / "a.md"
    md.write_text("x")
    page.replies[ADD] = [status_reply(ADD, code), status_reply(ADD, code)]
    page.replies[REGISTER] = [status_reply(REGISTER, code)]
    items = [{"kind": "url", "url": "https://blocked.example.net/"}, {"kind": "text", "title": "T", "text": "t"},
             {"kind": "file", "path": str(md), "name": "a.md", "content_type": "text/markdown", "bytes": 1}]
    got = browser.run(fetch.add_sources(NB, items, confirm=True))
    assert [s["error"] for s in got["sources"]] == [parse.source_rejected_message(code, k) for k in
                                                    ("url", "text", "file")]
    assert page.uploads == []  # a refused file is never uploaded


def test_unknown_codes_keep_the_api_change_hint(page):
    page.replies[ADD] = [status_reply(ADD, 13)]
    got = browser.run(fetch.add_sources(NB, [{"kind": "url", "url": "https://example.com/"}], confirm=True))
    error = got["sources"][0]["error"]
    assert "error code 13" in error and "API may have changed" in error and "couldn't import" not in error


def test_rejection_codes_dont_hide_auth_or_usage_limits(page):
    page.replies[ADD] = [status_reply(ADD, 16)]
    with pytest.raises(fetch.AuthRequired):
        browser.run(fetch.add_sources(NB, [{"kind": "url", "url": "https://example.com/"}], confirm=True))
    page.replies[ADD] = [status_reply(ADD, 8)]
    with pytest.raises(fetch.RateLimited):
        browser.run(fetch.add_sources(NB, [{"kind": "url", "url": "https://example.com/"}], confirm=True))


def test_rejection_codes_only_apply_to_source_adds(page):
    page.replies["rLM1Ne"] = [status_reply("rLM1Ne", 9)]
    with pytest.raises(RuntimeError, match="API may have changed"):
        browser.run(fetch.add_sources(NB, [{"kind": "url", "url": "https://example.com/"}], confirm=True))


# The CLI, over a faked daemon.

ADDED = {"notebook_id": NB, "notebook": "Synthetic", "url": f"https://notebooklm.google.com/notebook/{NB}",
         "sources": [{"kind": "url", "input": "https://example.com/", "type": "web_page", "id": NEW_SRC,
                      "title": "Example", "status": "processing", "error": None}]}


@pytest.fixture
def calls(monkeypatch):
    seen, answers = [], {
        "plan_sources": lambda nb, items, **_: {"notebook_id": nb, "notebook": "Synthetic", "url": "u",
                                                "existing_sources": 2,
                                                "sources": [parse.describe_item(i) for i in items],
                                                "calls": parse.write_budget(items)},
        "add_sources": lambda nb, items, **_: json.loads(json.dumps(ADDED)),
        "create": lambda title, items, **_: {"notebook_id": NEW_NB, "title": title, "url": "u", "sources": []},
    }

    async def call(ident, site, op, *args, **kwargs):
        seen.append((op, args, kwargs))
        value = answers.get(op)
        return value(*args, **kwargs) if callable(value) else value

    monkeypatch.setattr(daemon, "call", call)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    return seen, answers


def run(*args, input=None):
    return CliRunner().invoke(webcli_app.build(), ["notebooklm", *args], input=input)


def test_create_without_confirm_is_a_local_dry_run_that_exits_2(calls, tmp_path):
    seen, _ = calls
    md = tmp_path / "a.md"
    md.write_text("# A")
    result = run("create", "My notebook", "--file", str(md), "--url", "https://example.com/")
    assert result.exit_code == 2
    plan = json.loads(result.stdout)
    assert plan["dryRun"] is True and plan["title"] == "My notebook"
    assert plan["calls"] == {CREATE: 1, ADD: 1, REGISTER: 1, ep.UPLOAD: 2}
    assert [s["kind"] for s in plan["sources"]] == ["file", "url"]
    assert seen == []  # no browser for a create dry run
    assert run("create", "X", "--dry-run", "--confirm").exit_code == 2 and seen == []


def test_create_confirm_prints_id_and_url(calls):
    seen, _ = calls
    result = run("create", "My notebook", "--confirm", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"notebook_id": NEW_NB, "title": "My notebook", "url": "u", "sources": [], "failed": []}
    assert seen == [("create", ("My notebook", []), {"confirm": True, "headed": False})]


def test_source_add_dry_run_checks_the_notebook_and_exits_2(calls):
    seen, _ = calls
    result = run("source", "add", f"https://notebooklm.google.com/notebook/{NB}", "--url", "https://example.com/")
    assert result.exit_code == 2
    assert json.loads(result.stdout)["dryRun"] is True
    assert [op for op, *_ in seen] == ["plan_sources"] and seen[0][1][0] == NB


def test_source_add_confirm_and_wait_polls_until_ready(calls, tmp_path):
    seen, answers = calls
    states = iter(["processing", "ready"])
    answers["sources"] = lambda nb, **_: {"id": nb, "title": "Synthetic",
                                          "sources": [{"id": NEW_SRC, "title": "Example", "status": next(states)}]}
    urls = tmp_path / "urls.txt"
    urls.write_text("# reading list\nhttps://example.com/\n\nhttps://example.com/\n")
    result = run("source", "add", NB, "--urls-file", str(urls), "--confirm", "--wait", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["sources"][0]["status"] == "ready"
    ops = [op for op, *_ in seen]
    assert ops == ["add_sources", "sources", "sources"]
    assert seen[0][1] == (NB, [{"kind": "url", "url": "https://example.com/"}])  # duplicates dropped
    assert seen[0][2]["confirm"] is True


def test_source_add_exits_5_when_a_source_fails_or_wait_times_out(calls):
    _, answers = calls
    failed = json.loads(json.dumps(ADDED))
    failed["sources"][0].update({"id": None, "status": "failed", "error": "boom"})
    answers["add_sources"] = lambda *a, **k: failed
    assert run("source", "add", NB, "--url", "https://example.com/", "--yes").exit_code == 5
    answers["add_sources"] = lambda *a, **k: json.loads(json.dumps(ADDED))
    answers["sources"] = lambda nb, **_: {"sources": [{"id": NEW_SRC, "status": "processing"}]}
    assert run("source", "add", NB, "--url", "https://example.com/", "--yes", "--wait", "--timeout",
               "0").exit_code == 5


def test_the_failure_summary_names_the_failed_inputs(calls, monkeypatch):
    _, answers = calls
    result = json.loads(json.dumps(ADDED))
    bad = {"kind": "url", "type": "web_page", "id": None, "title": None, "status": "failed",
           "error": parse.source_rejected_message(9, "url")}
    result["sources"] += [dict(bad, input="https://paywalled.example.com/a"),
                          dict(bad, input="https://blocked.example.net/b")]
    answers["add_sources"] = lambda *a, **k: result
    out = run("source", "add", NB, "--url", "https://example.com/", "--url", "https://paywalled.example.com/a",
              "--url", "https://blocked.example.net/b", "--confirm", "--json")
    assert out.exit_code == 5
    assert json.loads(out.stdout)["sources"][1]["error"].startswith("NotebookLM couldn't import this source")
    # 0.16.4: stdout's JSON has the same top-level `failed` list as the stderr error.
    assert json.loads(out.stdout)["failed"] == ["https://paywalled.example.com/a", "https://blocked.example.net/b"]
    error = json.loads(out.stderr.strip().splitlines()[-1])
    assert error["error"] == ("2 of 3 sources failed: https://paywalled.example.com/a, "
                              "https://blocked.example.net/b.")
    assert error["failed"] == ["https://paywalled.example.com/a", "https://blocked.example.net/b"]
    assert error["code"] == 5


def test_a_long_failure_summary_is_shortened_but_json_lists_all(calls):
    _, answers = calls
    bad = {"kind": "url", "type": "web_page", "id": None, "title": None, "status": "failed", "error": "x"}
    result = dict(ADDED, sources=[dict(bad, input=f"https://site{i}.example.com/") for i in range(12)])
    answers["add_sources"] = lambda *a, **k: result
    out = run("source", "add", NB, "--url", "https://example.com/", "--confirm", "--json")
    error = json.loads(out.stderr.strip().splitlines()[-1])
    assert out.exit_code == 5 and error["error"].endswith("https://site9.example.com/, and 2 more.")
    assert len(error["failed"]) == 12
    assert len(json.loads(out.stdout)["failed"]) == 12


def test_json_stdout_has_an_empty_failed_list_when_nothing_failed(calls):
    _, answers = calls
    answers["add_sources"] = lambda *a, **k: json.loads(json.dumps(ADDED))
    out = run("source", "add", NB, "--url", "https://example.com/", "--confirm", "--json")
    assert out.exit_code == 0, out.output
    assert json.loads(out.stdout)["failed"] == []


def test_a_long_article_slug_is_not_redacted_in_the_failure_summary(calls):
    _, answers = calls
    url = "https://news.example.com/2025/08/18/study-finds-95-percent-of-ai-pilots-at-companies-failing-cfo"
    bad = {"kind": "url", "type": "web_page", "id": None, "title": None, "status": "failed", "error": "x", "input": url}
    answers["add_sources"] = lambda *a, **k: dict(ADDED, sources=[bad])
    out = run("source", "add", NB, "--url", url, "--confirm", "--json")
    error = json.loads(out.stderr.strip().splitlines()[-1])
    assert error["failed"] == [url] and error["error"] == f"1 of 1 sources failed: {url}."
    assert json.loads(out.stdout)["failed"] == [url]


def test_source_add_validates_before_anything_opens(calls, tmp_path):
    seen, _ = calls
    html = tmp_path / "page.html"
    html.write_text("<p>x</p>")
    empty = tmp_path / "empty.md"
    empty.write_text("")
    for args in (["--file", str(html)], ["--file", str(empty)], ["--file", str(tmp_path / "missing.md")],
                 ["--url", "ftp://example.com/x"], ["--url", "not a url"], ["--text", "body"],
                 ["--title", "T"], ["--text", "  ", "--title", "T"], []):
        assert run("source", "add", NB, *args, "--confirm").exit_code == 2, args
    assert seen == []


def test_text_from_stdin_and_mixed_sources(calls, tmp_path):
    seen, _ = calls
    txt = tmp_path / "b.txt"
    txt.write_text("B")
    result = run("source", "add", NB, "--text", "-", "--title", "Pasted", "--file", str(txt), "--url",
                 "https://youtu.be/abc", "--confirm", "--json", input="from stdin")
    assert result.exit_code == 0, result.output
    items = seen[0][1][1]
    assert [i["kind"] for i in items] == ["file", "url", "text"]
    assert items[0]["path"] == str(txt.resolve()) and items[0]["content_type"] == "text/plain"
    assert items[2] == {"kind": "text", "title": "Pasted", "text": "from stdin"}


def test_create_and_source_add_are_offered():
    assert "create" in cli.group.commands and set(cli.source_group.commands) == {"get", "add"}


# llmcli notebooklm stays ask-only.

def test_llmcli_cannot_create_or_add_sources():
    assert not ({"create", "add_sources", "plan_sources"} & chat.OPS)
    assert "create" not in chat.group.commands and "source" not in chat.group.commands
    for op in ("create", "add_sources"):
        with pytest.raises(RuntimeError, match=f"does not run '{op}'"):
            chat._call(op, "T", [], confirm=True)
