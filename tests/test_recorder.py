"""webcli recorder, offline: the fail-closed guard (exact PlaybackService reads, audio hops), the gRPC-web parsers,
the list, transcript, download, and sync read off a scripted page, and the commands over a faked daemon."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from datetime import timezone

import pytest
from click.testing import CliRunner

from webcli.apps import webcli as webcli_app
from webcli.core import daemon, guard
from webcli.core.errors import NotFound
from webcli.sites.recorder import endpoints, fetch, parse
from webcli.sites.recorder.endpoints import APP_URL, AUDIO_URL, RECORDING_URL
from webcli.sites.recorder.guard import audio_url_allowed, request_allowed, rpc_method

from google_fakes import APIResponse, Response, ScriptedPage, install

RPC = "https://pixelrecorder-pa.clients6.google.com/$rpc/java.com.google.wireless.android.pixel.recorder.protos.PlaybackService/"


def rec(id_: str, title: str, audio_id: str, *, transcript: bool = True, created: int = 1760900000) -> list:
    """A GetRecordingList recording: [id, title, [created_sec, ns], [duration_sec, ns], ..., segments (10), ...,
    audio_id (13)]."""
    return [id_, title, [str(created), 0], ["125", 0], None, None, None, None, None, None,
            [[1]] if transcript else None, None, None, audio_id]


def word(text: str, start: int) -> list:
    return [text, text, str(start), str(start + 400), None, None, [0, 1]]


# GetTranscription, as captured live (2026-10-19): [[unit, ...]], each unit [[word, ...], 0, language].
WORDS = [[[[word("Hello", 0)], 0, "en-US"], [[word("world.", 500)], 0, "en-US"]]]


# The guard.

@pytest.mark.parametrize(("method", "url"), [
    ("GET", "https://recorder.google.com/"),
    ("GET", "https://recorder.google.com/a1b2c3"),
    ("GET", "https://usercontent.recorder.google.com/download/playback/a1b2c3"),
    ("POST", RPC + "ListLabels"),  # the app waits for it before GetRecordingList
    ("POST", RPC + "GetRecordingList"),
    ("OPTIONS", RPC + "GetRecordingList"),
    ("POST", RPC + "GetRecordingInfo"),  # a recording's page waits for it before GetTranscription
    ("POST", RPC + "GetTranscription"),
    ("POST", RPC.replace("$rpc", "%24rpc") + "GetTranscription"),
    ("POST", "https://accounts.google.com/RotateCookies"),
])
def test_listed_reads_pass(method, url):
    assert request_allowed(method, url)


@pytest.mark.parametrize(("method", "url"), [
    ("POST", RPC + "DeleteRecording"),
    ("POST", RPC + "UpdateRecordingTitle"),
    ("POST", RPC + "ShareRecording"),
    ("POST", RPC + "GetShareLink"),  # a Get the guard hasn't been told is a read
    ("POST", RPC + "GetShareList"),  # the page asks for these; nothing webcli reads waits on them
    ("POST", RPC + "GetAudioTag"),
    ("POST", RPC + "GetWaveform"),
    ("POST", RPC + "GetGlobalSearchReadiness"),
    ("POST", RPC + "ListLabels/extra"),
    ("POST", RPC.replace("pixelrecorder-pa.clients6.google.com", "pixelrecorder-pa.clients6.google.com.evil.example")
     + "ListLabels"),
    ("POST", RPC + "GetRecordingList/extra"),
    ("POST", RPC.replace("PlaybackService", "EditingService") + "GetRecordingList"),
    ("POST", RPC.replace("pixelrecorder-pa.clients6.google.com", "evil.example") + "GetRecordingList"),
    ("POST", RPC.replace("https", "http") + "GetRecordingList"),
    ("POST", RPC.replace(".com/", ".com:8443/", 1) + "GetRecordingList"),
    ("PUT", RPC + "GetRecordingList"),
    ("GET", RPC + "GetRecordingList"),
    ("POST", "https://recorder.google.com/"),
    ("POST", "https://play.google.com/log?format=json"),
    ("GET", "https://peoplestack-pa.clients6.google.com/v1/people"),
    ("POST", "https://peoplestack-pa.clients6.google.com/$rpc/peoplestack.PeopleStackAutocompleteService/Autocomplete"),
    ("POST", "https://recorder.google.com/jserror?error=x"),
    ("POST", "https://recorder.google.com/api/monitoring/apicall"),
])
def test_writes_unknown_methods_and_other_hosts_are_blocked(method, url):
    assert not request_allowed(method, url)


def test_rpc_method():
    assert rpc_method(RPC + "GetTranscription") == "GetTranscription"
    assert rpc_method(RPC) is None and rpc_method("https://recorder.google.com/x") is None


@pytest.mark.parametrize(("url", "allowed"), [
    ("https://usercontent.recorder.google.com/download/playback/a1", True),
    ("https://rr3.googleusercontent.com/audio", True),
    ("http://usercontent.recorder.google.com/download/playback/a1", False),
    ("https://usercontent.recorder.google.com.evil.example/a1", False),
    ("https://user@usercontent.recorder.google.com/a1", False),
    ("https://recorder.google.com/a1", False),
])
def test_audio_hops(url, allowed):
    assert audio_url_allowed(url) is allowed


# The parsers.

def test_recordings_rows_and_a_malformed_one_is_skipped():
    rows = parse.recordings([[rec("r1", "Standup", "a1"), ["bad"], rec("r2", "Walk", "", transcript=False)], "cur"],
                            tz=timezone.utc)
    assert rows == [
        {"id": "r1", "title": "Standup", "created_at": "2025-10-19T18:53:20+00:00", "duration_seconds": 125,
         "has_transcript": True, "audio_id": "a1"},
        {"id": "r2", "title": "Walk", "created_at": "2025-10-19T18:53:20+00:00", "duration_seconds": 125,
         "has_transcript": False, "audio_id": ""}]
    assert parse.recordings(None) == [] and parse.recordings({}) == []


def test_transcript_joins_every_units_words_and_skips_what_doesnt_parse():
    assert parse.transcript(WORDS) == "Hello world."
    units = [[[word("One", 0), "junk", [], word("two", 400)], 0, "en-US"], "junk", [], [None, 0, "en-US"],
             [[word("three.", 900)], 0, "en-US"]]
    assert parse.transcript([units]) == "One two three."  # a bad unit doesn't drop the ones after it
    assert parse.transcript([]) == "" and parse.transcript(None) == "" and parse.transcript([7]) == ""


def test_merge_keeps_each_recording_once_newest_first():
    older, newer = rec("r1", "Old", "a1", created=100), rec("r2", "New", "a2", created=200)
    merged = parse.merge([[[older], 1], [[newer, ["bad"], rec("r1", "Old again", "a1", created=100)], 1], None, [7]])
    assert merged == [[newer, older, ["bad"]], None]  # first-seen r1 kept; unparseable sorts last
    assert [row["id"] for row in parse.recordings(merged)] == ["r2", "r1"]
    assert parse.page_items([[older], 1]) == [older] and parse.page_items(None) == [] and parse.page_items([7]) == []


# The page.

def recorder_page(recordings: list, *, transcripts: dict | None = None, files: dict | None = None) -> ScriptedPage:
    responses = {APP_URL: [Response(RPC + "GetRecordingList", [recordings, None])]}
    for audio_id, words in (transcripts or {}).items():
        responses[RECORDING_URL.format(audio_id=audio_id)] = [Response(RPC + "GetTranscription", words)]
    return ScriptedPage(responses=responses, files=files or {})


class PagedRecorderPage(ScriptedPage):
    """The real app's paging (captured live 2026-10-23): GetRecordingList answers PAGE_SIZE recordings at a time; the
    first page comes when the app opens, and each later one only once its list is scrolled to the end."""

    def __init__(self, pages: list, **kwargs) -> None:
        super().__init__(**kwargs)
        self.responses.setdefault(APP_URL, []).append(Response(RPC + "GetRecordingList", [pages[0], 1]))
        self.later = [Response(RPC + "GetRecordingList", [items, 1]) for items in pages[1:]]
        self.scrolled = 0

    async def evaluate(self, script: str, arg=None):
        assert script == fetch.SCROLL_TO_END
        self.scrolled += 1
        if self.later:
            response = self.later.pop(0)
            for handler in list(self.listeners["response"]):
                await handler(response)
        return 1


def library(count: int) -> list:
    """`count` recordings, newest first, in pages of PAGE_SIZE."""
    recs = [rec(f"r{i}", f"Call {i}", f"a{i}", created=1790000000 - i) for i in range(count)]
    return [recs[i:i + endpoints.PAGE_SIZE] for i in range(0, count, endpoints.PAGE_SIZE)]


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(fetch, "MORE_SECONDS", 0.01)


def test_the_list_reads_every_page_not_just_the_first(monkeypatch, quick):
    pages = library(26)  # 10 + 10 + 6, like the live library (86 = 8 x 10 + 6)
    pages[1].insert(0, pages[0][-1])  # a recording the app sends twice is listed once
    page = install(monkeypatch, PagedRecorderPage(pages))
    rows = asyncio.run(fetch.recordings())
    assert [row["id"] for row in rows] == [f"r{i}" for i in range(26)]
    assert page.scrolled == 2 and page.listeners["response"] == []  # the short page ends it: no extra scroll


def test_a_full_last_page_ends_after_quiet_scrolls(monkeypatch, quick):
    page = install(monkeypatch, PagedRecorderPage(library(20)))
    assert len(asyncio.run(fetch.recordings())) == 20
    assert page.scrolled == 1 + endpoints.MORE_TRIES  # one brought page 2, then MORE_TRIES brought nothing


def test_the_list_stops_at_its_deadline(monkeypatch, quick):
    monkeypatch.setattr(fetch, "LIST_SECONDS", 0)
    page = install(monkeypatch, PagedRecorderPage(library(30)))
    assert len(asyncio.run(fetch.recordings())) == 10 and page.scrolled == 0


def test_a_recording_past_the_first_page_has_its_transcript(monkeypatch, quick):
    install(monkeypatch, PagedRecorderPage(library(15), responses={
        RECORDING_URL.format(audio_id="a12"): [Response(RPC + "GetTranscription", WORDS)]}))
    assert asyncio.run(fetch.transcript("r12")) == {"id": "r12", "title": "Call 12", "text": "Hello world."}


def test_transcript_opens_the_recordings_page_and_reads_its_answer(monkeypatch):
    page = install(monkeypatch, recorder_page([rec("r1", "Standup", "a1")], transcripts={"a1": WORDS}))
    assert asyncio.run(fetch.transcript("r1")) == {"id": "r1", "title": "Standup", "text": "Hello world."}
    assert page.gotos == [APP_URL, "https://recorder.google.com/a1"] and page.listeners["response"] == []


def test_unknown_recordings_and_missing_transcripts_are_not_found(monkeypatch):
    install(monkeypatch, recorder_page([rec("r1", "Standup", "a1", transcript=False)]))
    with pytest.raises(NotFound, match="No recording r9"):
        asyncio.run(fetch.transcript("r9"))
    with pytest.raises(NotFound, match="has no transcript"):
        asyncio.run(fetch.transcript("r1"))


def test_a_page_that_never_answers_names_what_the_guard_blocked(monkeypatch):
    monkeypatch.setattr(fetch, "RPC_SECONDS", 0.01)
    install(monkeypatch, ScriptedPage())
    guard.state.blocked.append("POST pixelrecorder-pa.clients6.google.com/$rpc/x.PlaybackService/GetAudioTag")
    with pytest.raises(RuntimeError, match=r"no recording list within 0.01s\. The guard blocked: .*GetAudioTag"):
        asyncio.run(fetch.recordings())


def test_download_saves_title_m4a(monkeypatch, tmp_path):
    install(monkeypatch, recorder_page([rec("r1", "Team sync 10/19", "a1")],
                                       files={AUDIO_URL.format(audio_id="a1"): APIResponse(200, b"m4a")}))
    assert asyncio.run(fetch.download("r1", str(tmp_path))) == {
        "id": "r1", "title": "Team sync 10/19", "path": str(tmp_path / "Team sync 10_19.m4a"), "bytes": 3}
    assert stat.S_IMODE(os.stat(tmp_path / "Team sync 10_19.m4a").st_mode) == 0o600


def test_sync_backs_up_everything_keeps_same_titles_apart_and_lists_failures(monkeypatch, tmp_path):
    install(monkeypatch, recorder_page(
        [rec("r1", "Note", "a1"), rec("r2", "Note", "a2", transcript=False), rec("r3", "Gone", "a3")],
        transcripts={"a1": WORDS, "a3": WORDS},
        files={AUDIO_URL.format(audio_id="a1"): APIResponse(200, b"one"),
               AUDIO_URL.format(audio_id="a2"): APIResponse(200, b"two"),
               AUDIO_URL.format(audio_id="a3"): APIResponse(404)}))
    result = asyncio.run(fetch.sync(str(tmp_path)))
    assert result == {
        "transcripts": [str(tmp_path / "transcripts" / "Note.txt"), str(tmp_path / "transcripts" / "Gone.txt")],
        "audio": [str(tmp_path / "audio" / "Note.m4a"), str(tmp_path / "audio" / "Note-r2.m4a")],
        "errors": ["Audio r3: Recorder answered HTTP 404 for r3's audio."]}
    assert (tmp_path / "audio" / "Note-r2.m4a").read_bytes() == b"two"
    assert (tmp_path / "transcripts" / "Note.txt").read_text() == "Hello world."


def test_sync_names_stay_unique_whatever_the_titles_and_ids(monkeypatch, tmp_path):
    files = {AUDIO_URL.format(audio_id=a): APIResponse(200, a.encode()) for a in ("a1", "a2", "a3")}
    install(monkeypatch, recorder_page([rec("r1", "Note-r3", "a1", transcript=False),
                                        rec("r2", "Note", "a2", transcript=False),
                                        rec("r3", "note", "a3", transcript=False)], files=files))
    result = asyncio.run(fetch.sync(str(tmp_path)))
    assert [os.path.basename(p) for p in result["audio"]] == ["Note-r3.m4a", "Note.m4a", "note-r3-r3.m4a"]
    assert [p.read_bytes() for p in sorted((tmp_path / "audio").iterdir())] == [b"a1", b"a2", b"a3"]


# The commands, over a faked daemon.

ROWS = parse.recordings([[rec("r1", "Standup", "a1"), rec("r2", "Walk", "a2", transcript=False)]], tz=timezone.utc)


@pytest.fixture
def calls(monkeypatch):
    seen = []

    async def call(ident, site, op, *args, **kwargs):
        seen.append((op, args))
        if op == "recordings":
            return ROWS
        if op == "transcript":
            return {"id": args[0], "title": "Standup", "text": "Hello world."}
        if op == "sync":
            return {"transcripts": [], "audio": [f"{args[0]}/audio/Standup.m4a"], "errors": ["Audio r2: HTTP 500"]}
        return {"id": args[0], "title": "Standup", "path": f"{args[1]}/Standup.m4a", "bytes": 3}

    monkeypatch.setattr(daemon, "call", call)
    return seen


def run(*args):
    return CliRunner().invoke(webcli_app.build(), ["recorder", *args])


def test_list_info_and_search(calls):
    assert run("list", "--id-only").stdout == "r1\nr2\n"
    assert [set(r) for r in json.loads(run("list").stdout)] == [
        {"id", "title", "created_at", "duration_seconds", "has_transcript"}] * 2  # no internal audio id
    assert json.loads(run("info", "r2", "--compact").stdout) == {"id": "r2", "title": "Walk",
                                                                 "created_at": ROWS[1]["created_at"]}
    assert run("info", "r9").exit_code == 3
    assert [r["id"] for r in json.loads(run("search", "STAND").stdout)] == ["r1"]
    assert run("search", "nothing").exit_code == 3


def test_transcript_writes_id_txt_or_prints_with_dash(calls, tmp_path):
    result = run("transcript", "r1", "-o", str(tmp_path))
    assert result.exit_code == 0 and (tmp_path / "r1.txt").read_text() == "Hello world."
    assert json.loads(result.stdout) == {"id": "r1", "title": "Standup", "path": str(tmp_path / "r1.txt"), "chars": 12}
    assert run("transcript", "r1", "-o", "-").stdout == "Hello world.\n"


def test_download_and_sync_send_an_absolute_dir_and_sync_failures_exit_5(calls, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run("download", "r1").exit_code == 0 and calls[-1] == ("download", ("r1", str(tmp_path.resolve())))
    result = run("sync")
    assert calls[-1] == ("sync", (str(tmp_path.resolve() / "recorder-backup"),))
    assert result.exit_code == 5 and json.loads(result.stdout)["audio"] == [
        f"{tmp_path.resolve()}/recorder-backup/audio/Standup.m4a"]
    assert "Audio r2: HTTP 500" in result.stderr
