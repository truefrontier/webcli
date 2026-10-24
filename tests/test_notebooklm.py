"""webcli notebooklm: the payload readers over synthetic fixtures (tests/fixtures/notebooklm, built by make.py in the
shapes live answers have), the request builders for every Studio output, the guard's three policies, the fetch code
over a scripted page, and the CLI over a faked daemon (generate's dry run, --confirm, --wait, download, --json)."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

import pytest
from click.testing import CliRunner

from webcli.apps import webcli as webcli_app
from webcli.core import browser, daemon
from webcli.core.errors import AuthRequired, NotFound, UsageError
from webcli.core.identities import Identity
from webcli.sites.notebooklm import cli, endpoints as ep, fetch, guard, parse

from google_fakes import APIResponse, ScriptedPage, install

FIX = Path(__file__).parent / "fixtures" / "notebooklm"
NB = "11111111-2222-4333-8444-555555555555"
S1, S2 = "aaaaaaaa-1111-4111-8111-111111111111", "bbbbbbbb-2222-4222-8222-222222222222"
A_AUDIO, A_REPORT, A_VIDEO = ("cccccccc-0000-4000-8000-00000000000" + c for c in "123")
A_QUIZ, A_MAP, A_SLIDES, A_PENDING = ("dddddddd-0000-4000-8000-00000000000" + c for c in "1234")
MEDIA = "https://lh3.googleusercontent.com/notebooklm/FAKEmedia"


def fixture(name: str) -> str:
    return (FIX / f"{name}.txt").read_text()


def payload(name: str, rpcid: str):
    return parse.rpc_payload(fixture(name), rpcid)


# Readers.

def test_notebooks_read_id_title_count_and_times():
    rows = parse.notebooks(payload("notebooks", "wXbhsf"))
    assert [r["id"] for r in rows] == [NB, "99999999-2222-4333-8444-555555555555"]
    first, second = rows
    assert (first["title"], first["emoji"], first["sources"]) == ("Synthetic research notebook", "📓", 1)
    assert first["created_at"] == "2026-03-14T11:01:55Z" and first["viewed_at"] == "2026-03-14T11:02:01Z"
    assert first["url"] == f"https://notebooklm.google.com/notebook/{NB}"
    assert (second["title"], second["sources"], second["created_at"]) == ("Second notebook", 0, None)


def test_a_notebook_lists_its_sources():
    nb = parse.notebook(payload("notebook", "rLM1Ne"))
    assert nb["id"] == NB and [s["id"] for s in nb["sources"]] == [S1, S2]
    assert [(s["type"], s["status"], s["words"]) for s in nb["sources"]] == [("markdown", "ready", 763),
                                                                             ("youtube", "processing", 120)]


def test_source_text_joins_its_blocks_as_paragraphs():
    got = parse.source_text(payload("source", "hizoJc"))
    assert got == {"id": S1, "title": "Doc one", "type": "markdown", "text": "First para.\n\nSecond para."}


def test_source_guide_reads_summary_and_keywords():
    assert parse.source_guide(payload("guide", "tr032e")) == {"summary": "A short synthetic summary.",
                                                             "keywords": ["alpha", "beta", "gamma"]}


def test_notes_skip_deleted_rows_and_tell_mind_maps_apart():
    rows = parse.notes(payload("notes", "cFji9"))
    assert [(r["id"], r["title"], r["kind"]) for r in rows] == [("note-1", "My note", "note"),
                                                                ("note-2", "Map note", "mind_map")]


def test_artifacts_read_kind_status_and_links_newest_first():
    rows = parse.artifacts(payload("artifacts", "gArtLc"))
    assert [(r["id"], r["kind"], r["status"]) for r in rows] == [
        (A_PENDING, "infographic", "pending"), (A_QUIZ, "quiz", "completed"), (A_REPORT, "report", "completed"),
        (A_AUDIO, "audio", "completed"), (A_VIDEO, "video", "completed"), (A_MAP, "mind_map", "completed"),
        (A_SLIDES, "slide_deck", "completed")]
    by_id = {r["id"]: r for r in rows}
    assert by_id[A_AUDIO]["url"] == MEDIA + "-m4a"  # audio/mp4 over the DASH manifest
    assert by_id[A_VIDEO]["url"] == MEDIA + "-v4"  # video/mp4, kind 4 first
    assert by_id[A_REPORT]["markdown"] == "# Study Guide\n\nBody."
    assert by_id[A_SLIDES]["pdf_url"] == MEDIA + "-pdf"


def test_quiz_and_mind_map_bodies():
    assert parse.app_data(parse.app_html(payload("quiz", "v9rmvd"))) == {
        "quiz": [{"question": "Q1?", "answerOptions": []}]}
    assert json.loads(parse.mind_map_tree(payload("mind_map", "v9rmvd")))["children"] == [{"name": "Leaf"}]
    assert parse.app_data("<div>no data</div>") is None


def test_the_streamed_answer_keeps_the_final_text_and_its_citations():
    got = parse.answer(fixture("answer"))
    assert (got.text, got.final, got.conversation_id) == ("The full answer [1].", True, "conv-1")
    assert got.citations == [{"number": 1, "source_id": S1, "excerpt": "First para."}]


def test_an_error_envelope_raises():
    body = ")]}'\n\n" + json.dumps([["wrb.fr", "wXbhsf", None, None, None, [7], "generic"]])
    with pytest.raises(parse.RpcError) as exc:
        parse.rpc_payload(body, "wXbhsf")
    assert exc.value.code == 7


def test_clean_id_accepts_a_notebook_url():
    assert parse.clean_id(f"https://notebooklm.google.com/notebook/{NB}?authuser=0") == NB
    assert parse.clean_id(f"  {NB} ") == NB


# Builders: one CreateArtifact descriptor per kind, at its type's slot.

def descriptor(kind, **opts):
    params = parse.generate_params(kind, NB, [S1, S2], opts)
    assert params[0] == parse.ARTIFACT_CONTEXT and params[1] == NB
    desc = params[2]
    assert desc[:4] == [None, None, desc[2], [[[S1]], [[S2]]]]
    return desc


@pytest.mark.parametrize("kind,type_code,slot", [
    ("audio", 1, 6), ("video", 3, 8), ("report", 2, 7), ("quiz", 4, 9), ("flashcards", 4, 9), ("mind-map", 4, 9),
    ("infographic", 7, 14), ("slide-deck", 8, 16), ("data-table", 9, 18)])
def test_every_kind_puts_its_config_at_its_slot(kind, type_code, slot):
    desc = descriptor(kind)
    assert desc[2] == type_code and len(desc) == slot + 1 and desc[slot] is not None
    assert all(v is None for v in desc[4:slot])


def test_audio_formats_and_lengths():
    config = descriptor("audio", format="brief", length="short", language="de", instructions="focus")[6]
    assert config == [None, ["focus", 1, None, [[S1], [S2]], "de", None, 2]]
    assert descriptor("audio", format="debate")[6][1][6] == 4 and descriptor("audio")[6][1][1] == 2


def test_video_formats_and_styles():
    assert descriptor("video", format="explainer", style="whiteboard")[8] == [None, None,
                                                                             [[[S1], [S2]], "en", None, None, 1, 3]]
    assert descriptor("video", style="custom", style_prompt="neon")[8][2][5:] == [None, "neon"]
    assert descriptor("video", format="cinematic", style="anime")[8][2] == [[[S1], [S2]], "en", None, None, 3]


def test_report_presets_and_custom():
    title, description, _, _, _, directive, _, flag = descriptor("report", format="study-guide",
                                                                 instructions="Chapter 2")[7][1]
    assert (title, description, flag) == ("Study Guide", "Short-answer quiz, essay questions, glossary", True)
    assert directive.startswith("Create a comprehensive study guide") and directive.endswith("\n\nChapter 2")
    assert descriptor("report", format="custom", instructions="Just a list")[7][1][5] == "Just a list"


def test_quiz_flashcards_and_the_rest():
    assert descriptor("quiz", quantity="more", difficulty="hard")[9] == [None, [2, None, None, None, None, None,
                                                                              None, [3, 3]]]
    assert descriptor("flashcards", quantity="fewer")[9] == [None, [1, None, None, None, None, None, [1, 2]]]
    assert descriptor("mind-map")[9] == [None, [4]]
    assert descriptor("infographic", orientation="portrait", detail="detailed", style="bento-grid")[14] == [
        [None, "en", None, 2, 3, 4]]
    assert descriptor("slide-deck", format="presenter", length="short")[16] == [[None, "en", 2, 2]]
    assert descriptor("data-table", instructions="prices")[18] == [None, ["prices", "en"]]
    with pytest.raises(ValueError):
        parse.generate_params("faq", NB, [S1], {})


# The guard.

def batch(rpcid: str, label: str | None = None, host: str = ep.APP_HOST) -> tuple[str, str]:
    url = f"https://{host}{ep.BATCH_PATH}?" + urlencode({"rpcids": label or rpcid, "source-path": "/"})
    body = urlencode({"f.req": json.dumps([[[rpcid, "[]", None, "generic"]]]), "at": "FAKEtoken"})
    return url, body


@pytest.mark.parametrize("rpcid", sorted(ep.READ_RPCIDS))
def test_reads_pass_every_policy(rpcid):
    for kwargs in ({}, {"ask": True}, {"generate": True}):
        assert guard.request_allowed("POST", *batch(rpcid), **kwargs)


@pytest.mark.parametrize("rpcid", sorted(ep.MUTATING_RPCIDS))
def test_every_known_write_is_blocked_under_every_policy(rpcid):
    for kwargs in ({}, {"ask": True}, {"generate": True}):
        assert not guard.request_allowed("POST", *batch(rpcid), **kwargs)


def test_create_artifact_only_under_generate_and_chat_only_under_ask():
    create = batch("R7cb6c")
    assert not guard.request_allowed("POST", *create) and not guard.request_allowed("POST", *create, ask=True)
    assert guard.request_allowed("POST", *create, generate=True)
    chat = f"https://{ep.APP_HOST}{ep.QUERY_PATH}?rt=c"
    assert guard.request_allowed("POST", chat, ask=True)
    assert not guard.request_allowed("POST", chat) and not guard.request_allowed("POST", chat, generate=True)


def test_a_url_label_that_disagrees_with_the_body_is_blocked():
    assert not guard.request_allowed("POST", *batch("WWINqb", label="wXbhsf"))  # delete smuggled under a read label
    url, _ = batch("wXbhsf")
    two = urlencode({"f.req": json.dumps([[["wXbhsf", "[]", None, "generic"], ["WWINqb", "[]", None, "generic"]]])})
    assert not guard.request_allowed("POST", url.replace("wXbhsf", "wXbhsf%2CWWINqb"), two)


def test_hosts_paths_and_methods():
    assert guard.request_allowed("POST", *batch("wXbhsf", host="notebook.google.com"))  # the new host, same app
    assert not guard.request_allowed("POST", *batch("wXbhsf", host="gemini.google.com"))
    assert not guard.request_allowed("GET", f"https://{ep.APP_HOST}{ep.BATCH_PATH}?rpcids=wXbhsf")
    assert not guard.request_allowed("POST", f"https://{ep.APP_HOST}{ep.RPC_PREFIX}log?format=json")
    assert guard.request_allowed("GET", f"https://{ep.APP_HOST}/notebook/{NB}")
    assert not guard.request_allowed("GET", "https://accounts.google.com/Logout")
    assert not guard.request_allowed("POST", "https://play.google.com/log?format=json")
    assert guard.media_url_allowed(MEDIA) and not guard.media_url_allowed("https://evil.example.com/a.m4a")
    assert not guard.media_url_allowed("http://lh3.googleusercontent.com/a")


def test_describe_names_rpcids_but_never_the_token():
    url, body = batch("WWINqb")
    text = guard.describe("POST", url, body)
    assert "rpcids=WWINqb" in text and "FAKEtoken" not in text and "source-path" not in text


# Fetch, over a scripted page.

ANSWERS = {"wXbhsf": "notebooks", "rLM1Ne": "notebook", "hizoJc": "source", "tr032e": "guide", "cFji9": "notes",
           "gArtLc": "artifacts", "R7cb6c": "created"}


@pytest.fixture
def page(monkeypatch):
    browser.identity = Identity("google:default")
    calls = []

    def call(arg):
        calls.append(arg)
        if arg["path"] == ep.QUERY_PATH:
            return {"status": 200, "contentType": "application/json", "body": fixture("answer")}
        rpcid = arg["rpcid"]
        kind = json.loads(json.loads(arg["freq"])[0][0][1])
        name = ANSWERS.get(rpcid) or ("quiz" if kind == [A_QUIZ] else "mind_map")
        return {"status": 200, "contentType": "application/json", "body": fixture(name)}

    scripted = ScriptedPage(answers={"SNlM0e": call, "oPEP7c": "kevin@example.com"},
                            files={MEDIA + "-m4a": APIResponse(200, b"m4a bytes")})
    scripted.calls = calls
    return install(monkeypatch, scripted)


def test_reads_run_under_the_read_policy(page):
    assert [r["id"] for r in browser.run(fetch.notebooks())][0] == NB
    assert page.policy is guard.READ and page.gotos == [ep.APP_URL]
    assert browser.run(fetch.status()) == {"signedIn": True, "email": "kevin@example.com", "api": "ok"}
    assert browser.run(fetch.source(S1, NB, guide=True))["keywords"] == ["alpha", "beta", "gamma"]
    assert page.calls[-1]["sourcePath"] == f"/notebook/{NB}"


def test_ask_runs_under_ask_with_every_source_and_names_cited_sources(page):
    got = browser.run(fetch.ask(NB, "What is it?"))
    assert page.policy is guard.ASK
    chat = page.calls[-1]
    params = json.loads(json.loads(chat["freq"])[1])
    assert params[0] == [[[S1]], [[S2]]] and params[1] == "What is it?" and params[7] == NB
    assert got["answer"] == "The full answer [1]." and got["citations"][0]["source_title"] == "Doc one"
    with pytest.raises(NotFound):
        browser.run(fetch.ask(NB, "Q", ["cccccccc-9999-4999-8999-999999999999"]))


def test_generate_needs_confirm_and_runs_under_generate(page):
    with pytest.raises(UsageError):
        browser.run(fetch.generate("report", NB, None, {"format": "study-guide"}))
    assert page.calls == []
    row = browser.run(fetch.generate("report", NB, [S1], {"format": "study-guide"}, confirm=True))
    assert page.policy is guard.GENERATE and row["status"] == "pending" and row["notebook_id"] == NB
    sent = json.loads(json.loads(page.calls[-1]["freq"])[0][0][1])
    assert page.calls[-1]["rpcid"] == "R7cb6c" and sent[2][3] == [[[S1]]]
    assert [c["rpcid"] for c in page.calls].count("R7cb6c") == 1


def test_plan_is_read_only(page):
    got = browser.run(fetch.plan("audio", NB, None, {"format": "brief", "length": "short"}))
    assert page.policy is guard.READ and got["sources"] == 2 and "R7cb6c" not in {c["rpcid"] for c in page.calls}


def test_download_saves_each_kind(page, tmp_path):
    audio = browser.run(fetch.download(A_AUDIO, str(tmp_path), NB))
    assert Path(audio["path"]).read_bytes() == b"m4a bytes" and audio["path"].endswith("Deep dive.m4a")
    assert (Path(audio["path"]).stat().st_mode & 0o777) == 0o600
    report = browser.run(fetch.download(A_REPORT, str(tmp_path / "guide.md"), NB))
    assert Path(report["path"]).read_text() == "# Study Guide\n\nBody."
    quiz = browser.run(fetch.download(A_QUIZ, str(tmp_path), NB))
    assert json.loads(Path(quiz["path"]).read_text())["quiz"][0]["question"] == "Q1?"
    tree = browser.run(fetch.download(A_MAP, str(tmp_path), NB))
    assert json.loads(Path(tree["path"]).read_text())["name"] == "Root"
    with pytest.raises(UsageError):  # not completed yet
        browser.run(fetch.download(A_PENDING, str(tmp_path), NB))
    with pytest.raises(NotFound):
        browser.run(fetch.download("ffffffff-0000-4000-8000-000000000000", str(tmp_path), NB))


def test_a_media_link_off_the_allowed_hosts_is_refused(page, tmp_path, monkeypatch):
    real = parse.artifacts
    monkeypatch.setattr(parse, "artifacts", lambda p: [{**r, "url": "https://evil.example.com/x"} for r in real(p)])
    with pytest.raises(RuntimeError, match="Refused"):
        browser.run(fetch.download(A_AUDIO, str(tmp_path), NB))
    assert page.fetched == []


def test_signed_out(monkeypatch):
    browser.identity = Identity("google:default")
    install(monkeypatch, ScriptedPage(redirects={ep.APP_URL: "https://accounts.google.com/v3/signin"}))
    assert browser.run(fetch.status())["signedIn"] is False
    with pytest.raises(AuthRequired):
        browser.run(fetch.notebooks())


# The CLI, over a faked daemon.

PENDING = {"id": "new-1", "title": None, "kind": "report", "status": "pending", "created_at": None,
           "notebook_id": NB, "notebook": "Synthetic"}


@pytest.fixture
def calls(monkeypatch):
    seen, answers = [], {
        "plan": {"kind": "report", "notebook_id": NB, "notebook": "Synthetic", "sources": 2, "options": {}},
        "generate": PENDING,
    }

    async def call(ident, site, op, *args, **kwargs):
        seen.append((op, args, kwargs))
        value = answers.get(op)
        return value(*args, **kwargs) if callable(value) else value

    monkeypatch.setattr(daemon, "call", call)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    return seen, answers


def run(*args):
    return CliRunner().invoke(webcli_app.build(), ["notebooklm", *args])


def test_generate_without_confirm_is_a_dry_run_that_exits_2(calls):
    seen, _ = calls
    result = run("generate", "report", NB, "--style", "study-guide")
    assert result.exit_code == 2
    assert json.loads(result.stdout)["dryRun"] is True
    assert [op for op, *_ in seen] == ["plan"]
    assert seen[0][1][3]["format"] == "study-guide"
    assert run("generate", "audio", NB, "--dry-run").exit_code == 2
    assert run("generate", "audio", NB, "--dry-run", "--confirm").exit_code == 2
    assert "generate" not in [op for op, *_ in seen]


def test_confirm_creates_once_and_wait_polls_until_completed(calls):
    seen, answers = calls
    states = iter(["in_progress", "completed"])
    answers["artifact"] = lambda artifact_id, nb, **_: {**PENDING, "status": next(states)}
    result = run("generate", "audio", NB, "--style", "brief", "--length", "short", "--confirm", "--wait", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "completed"
    ops = [op for op, *_ in seen]
    assert ops == ["generate", "artifact", "artifact"]
    assert seen[0][1][:3] == ("audio", NB, None) and seen[0][1][3]["format"] == "brief"
    assert seen[0][2]["confirm"] is True


def test_wait_exits_5_when_it_fails_or_times_out(calls):
    _, answers = calls
    answers["artifact"] = lambda *a, **k: {**PENDING, "status": "failed"}
    assert run("generate", "quiz", NB, "--yes", "--wait").exit_code == 5
    answers["artifact"] = lambda *a, **k: PENDING
    assert run("generate", "quiz", NB, "--yes", "--wait", "--timeout", "0").exit_code == 5


def test_video_custom_style_needs_a_prompt(calls):
    assert run("generate", "video", NB, "--visual", "custom").exit_code == 2
    assert run("generate", "video", NB, "--visual-prompt", "neon").exit_code == 2


def test_status_with_an_artifact_and_download(calls, tmp_path):
    seen, answers = calls
    answers["artifact"] = {**PENDING, "status": "completed"}
    result = run("status", "new-1", "-n", NB, "--json")
    assert result.exit_code == 0 and json.loads(result.stdout)["status"] == "completed"
    answers["download"] = lambda aid, path, nb, **_: {"id": aid, "title": "T", "kind": "report", "path": path,
                                                      "bytes": 3}
    result = run("download", "new-1", "-o", str(tmp_path / "out") + "/", "--json")
    assert result.exit_code == 0
    assert seen[-1][1] == ("new-1", str((tmp_path / "out").resolve()), None) and (tmp_path / "out").is_dir()


def test_kinds_lists_faq_and_timeline_as_unavailable(calls):
    rows = json.loads(run("generate", "kinds", "--json").stdout)
    assert {r["kind"] for r in rows if r["available"]} == set(parse.KINDS)
    assert {r["kind"] for r in rows if not r["available"]} == {"faq", "timeline"}


def test_generate_offers_every_kind():
    assert set(cli.generate_group.commands) == {*parse.KINDS, "kinds"}


def test_ask_json_and_text(calls):
    _, answers = calls
    answers["ask"] = {"answer": "Yes [1].", "citations": [{"number": 1, "source_id": S1, "source_title": "Doc one",
                                                            "excerpt": "x"}], "conversation_id": "c"}
    assert json.loads(run("ask", NB, "Q?", "--json").stdout)["answer"] == "Yes [1]."
    assert run("ask", NB, "  ").exit_code == 2
