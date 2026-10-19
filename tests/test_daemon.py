"""The identity daemon over a real unix socket, with the site's fetch stubbed: no Chrome is launched. Ported from
cgpt's test_daemon.py, plus what the generalization adds (site/op allowlist, provider binding, forced headless)."""

from __future__ import annotations

import asyncio
import json
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import pytest

from webcli import __version__
from webcli.core import browser, config, daemon, guard, output
from webcli.core.errors import AuthRequired, Challenged, ConfigError, NotFound, ProfileBusy, RateLimited
from webcli.core.identities import Identity
from webcli.sites.chatgpt import fetch
from webcli.sites.chatgpt.guard import POLICY

IDENT = Identity("chatgpt:default")
_real_start = daemon.start  # before conftest stubs it


async def _timed(coro):
    import time

    began = time.monotonic()
    return await coro, time.monotonic() - began


@pytest.fixture
def sock(monkeypatch):
    work = Path(tempfile.mkdtemp(prefix="webcli-", dir="/tmp"))  # unix socket paths must stay short
    monkeypatch.setattr(daemon, "socket_path", lambda ident: work / f"{ident.dir_name}.sock")
    monkeypatch.setenv("WEBCLI_DAEMON", "1")

    def no_spawn(ident):
        raise OSError("tests never spawn a daemon")

    monkeypatch.setattr(daemon, "start", no_spawn)
    yield work / f"{IDENT.dir_name}.sock"
    shutil.rmtree(work, ignore_errors=True)


def serving(path: Path, body, stops: list | None = None, ident: Identity = IDENT):
    async def main():
        shared = daemon.Shared(ident)
        server = await daemon.listen(shared, path, lambda: stops.append("stop") if stops is not None else None)
        try:
            return await body(shared)
        finally:
            server.close()
    return asyncio.run(main())


def test_socket_is_private_and_an_ask_round_trips_with_image_bytes(monkeypatch, sock):
    calls = []

    async def ask(prompt, **kwargs):
        calls.append((prompt, kwargs, daemon.request_wait.get()))
        return {"chatId": "c", "answer": "", "imageCount": 1,
                "imageData": [{"data": b"\x89PNG\x00\xff", "sourceWidth": 2, "sourceHeight": 2, "sourceBytes": 6}]}

    monkeypatch.setattr(fetch, "ask", ask)
    monkeypatch.setattr(browser, "lock_wait", 900.0)

    async def body(_shared):
        assert stat.S_IMODE(sock.stat().st_mode) == 0o600
        return await daemon.call(IDENT, "chatgpt", "ask", "q", chat_id=None, want_images=True, timeout=600,
                                 headed=False)

    result = serving(sock, body)
    assert result["imageData"][0]["data"] == b"\x89PNG\x00\xff"
    assert calls == [("q", {"chat_id": None, "want_images": True, "timeout": 600, "headed": False}, 900.0)]


def test_the_daemon_forces_headless_whatever_the_client_asks(monkeypatch, sock):
    seen = []

    async def models(**kwargs):
        seen.append(kwargs["headed"])
        return []

    monkeypatch.setattr(fetch, "models", models)

    async def body(_shared):
        reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(json.dumps({"site": "chatgpt", "op": "models", "kwargs": {"headed": True},
                                 "version": __version__}).encode() + b"\n")
        await writer.drain()
        answer = json.loads(await reader.readline())
        writer.close()
        return answer

    assert serving(sock, body)["ok"] is True
    assert seen == [False]


@pytest.mark.parametrize(("site", "op"), [("gemini", "models"), ("chatgpt", "login"), ("chatgpt", "api"),
                                          ("nope", "models")])
def test_the_daemon_runs_only_its_providers_listed_ops(monkeypatch, sock, site, op):
    async def body(_shared):
        reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(json.dumps({"site": site, "op": op, "version": __version__}).encode() + b"\n")
        await writer.drain()
        answer = json.loads(await reader.readline())
        writer.close()
        return answer

    answer = serving(sock, body)
    assert answer["ok"] is False and answer["error"]["type"] == "ConfigError"


@pytest.mark.parametrize("error", [
    Challenged("headless", "chatgpt.com"),
    RateLimited("You've hit the limit.", sent=True, resets_at="2026-10-19T09:00:00-05:00"),
    NotFound("Chat x not found.", hint="check llmcli chatgpt status"),
    ConfigError("bad config", hint="fix it"),
    AuthRequired("Not signed in to ChatGPT."),
    ProfileBusy("busy"),
])
def test_errors_come_back_as_the_same_exception(monkeypatch, sock, error):
    async def models(**_kwargs):
        raise error

    monkeypatch.setattr(fetch, "models", models)

    async def body(_shared):
        with pytest.raises(type(error)) as raised:
            await daemon.call(IDENT, "chatgpt", "models", headed=False)
        return raised.value

    got = serving(sock, body)
    assert str(got) == str(error)
    for attr in ("mode", "host", "sent", "resets_at", "hint"):
        assert getattr(got, attr, None) == getattr(error, attr, None)


def test_other_errors_are_redacted_runtime_errors(monkeypatch, sock):
    token = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlLWZha2U"

    async def models(**_kwargs):
        raise KeyError(f"Authorization: Bearer {token}")

    monkeypatch.setattr(fetch, "models", models)

    async def body(_shared):
        with pytest.raises(RuntimeError) as raised:
            await daemon.call(IDENT, "chatgpt", "models", headed=False)
        return str(raised.value)

    assert token not in serving(sock, body)


def test_a_client_that_leaves_cancels_its_command(monkeypatch, sock):
    started, cancelled = asyncio.Event(), []

    async def models(**_kwargs):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(fetch, "models", models)

    async def body(_shared):
        reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(json.dumps({"site": "chatgpt", "op": "models", "kwargs": {}, "version": __version__}).encode()
                     + b"\n")
        await writer.drain()
        await asyncio.wait_for(started.wait(), 5)
        writer.close()  # what SIGTERM to the command does
        for _ in range(40):
            if cancelled:
                break
            await asyncio.sleep(0.05)

    serving(sock, body)
    assert cancelled == [True]


def test_pause_is_a_lease_that_holds_the_turn_until_its_connection_closes(monkeypatch, sock):
    monkeypatch.setattr(browser, "lock_wait", 0.3)

    async def body(shared):
        monkeypatch.setattr(browser, "shared", shared)  # fetch.models -> with_app -> the daemon's turn
        reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(json.dumps({"op": "pause", "lockWait": 5, "version": __version__}).encode() + b"\n")
        await writer.drain()
        assert json.loads(await reader.readline())["result"] == {"paused": True}
        assert shared.paused and shared.turn.locked()
        with pytest.raises(ProfileBusy, match="identity login"):
            await daemon.call(IDENT, "chatgpt", "models", headed=False)  # no Chrome is launched while paused
        writer.close()
        for _ in range(40):
            if not shared.turn.locked():
                break
            await asyncio.sleep(0.05)
        return shared

    shared = serving(sock, body)
    assert not shared.turn.locked() and not shared.paused and shared.context is None


def test_a_newer_webcli_makes_the_daemon_restart_and_nothing_runs(monkeypatch, sock):
    stops, ran = [], []

    async def models(**_kwargs):
        ran.append(True)
        return []

    monkeypatch.setattr(fetch, "models", models)

    async def body(_shared):
        reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(json.dumps({"site": "chatgpt", "op": "models", "kwargs": {}, "version": "99.0.0"}).encode()
                     + b"\n")
        await writer.drain()
        answer = json.loads(await reader.readline())
        writer.close()
        older = await daemon._request(await asyncio.open_unix_connection(str(sock)),
                                      {"op": "ping", "version": "0.0.1"})
        return answer, older

    answer, older = serving(sock, body, stops)
    assert answer["error"]["type"] == "Restarting" and stops == ["stop"] and ran == []
    assert older["result"]["version"] == __version__ and older["result"]["identity"] == IDENT.id


def test_webcli_daemon_0_pauses_a_running_daemon_and_runs_in_process(monkeypatch, sock):
    monkeypatch.setenv("WEBCLI_DAEMON", "0")
    seen = []

    async def body(shared):
        async def models(**kwargs):
            seen.append((shared.paused, shared.turn.locked()))
            return []

        monkeypatch.setattr(fetch, "models", models)
        await daemon.call(IDENT, "chatgpt", "models", headed=False)
        for _ in range(40):
            if not shared.turn.locked():
                break
            await asyncio.sleep(0.05)
        return shared

    shared = serving(sock, body)
    assert seen == [(True, True)] and not shared.paused and not shared.turn.locked()


def test_page_at_closes_the_guard_when_opening_the_tab_fails():
    class Context:
        pages = []

        async def new_page(self):
            raise asyncio.CancelledError

    async def main():
        async with browser.page_at(Context(), POLICY, send=True, new=True):
            pass

    guard.state.begin(POLICY, send=True)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main())
    assert guard.state.policy is None and guard.state.send is False and guard.state.expect_models is None


def test_without_a_daemon_the_command_runs_in_process(monkeypatch, sock):
    async def models(**kwargs):
        return [{"slug": "gpt-6", "headed": kwargs["headed"]}]

    monkeypatch.setattr(fetch, "models", models)
    assert asyncio.run(daemon.call(IDENT, "chatgpt", "models", headed=False)) == [{"slug": "gpt-6", "headed": False}]


def test_a_check_that_must_not_start_a_daemon_runs_in_process(monkeypatch, sock):
    async def status(**kwargs):
        return {"signedIn": False}

    monkeypatch.setattr(fetch, "status", status)
    monkeypatch.setattr(daemon, "start", lambda ident: pytest.fail("started a daemon"))
    assert asyncio.run(daemon.call(IDENT, "chatgpt", "status", headed=False, start_daemon=False)) == \
        {"signedIn": False}


def test_bad_requests_are_refused():
    assert daemon.error_payload(OSError("x"))["type"] == "RuntimeError"
    assert isinstance(daemon.rebuild({"type": "SystemExit", "message": "x"}), RuntimeError)


def test_launchd_plist_is_a_per_identity_background_agent_with_no_raw_output(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/Users/me/.local/bin/llmcli")
    prog = daemon.program(Identity("google:default"))
    plist = daemon.plist(Identity("google:default"), prog)
    assert plist["Label"] == "com.truefrontier.webcli.google-default"
    assert plist["ProgramArguments"] == ["/Users/me/.local/bin/llmcli", "serve", "--identity", "google:default"]
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["ProcessType"] == "Background"
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == "/dev/null"
    assert plist["EnvironmentVariables"]["WEBCLI_HOME"] == str(config.home())
    assert daemon.plist_path(Identity("chatgpt:default")).name == "com.truefrontier.webcli.chatgpt-default.plist"


def test_a_spawned_daemon_runs_llmcli_serve_for_its_identity():
    assert daemon.serve_command(IDENT) == [sys.executable, "-m", "webcli.apps.llmcli", "serve", "--identity",
                                           "chatgpt:default"]


def test_the_daemons_output_goes_through_redaction_into_its_log(monkeypatch):
    token = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlLWZha2U"
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    daemon._redirect_output(IDENT)
    daemon.log(f"chrome: launch failed: Authorization: Bearer {token}")
    print("traceback line: cookie=" + token[:20], file=sys.stderr, end="")  # a line split across writes
    print(token[20:] + " rest", file=sys.stderr)
    sys.stderr.flush()
    text = (config.home() / "logs" / f"serve-{IDENT.dir_name}.log").read_text()
    assert token not in text and token[:20] not in text and "<redacted>" in text
    assert stat.S_IMODE((config.home() / "logs" / f"serve-{IDENT.dir_name}.log").stat().st_mode) == 0o600
    assert output.RedactingStream  # the stream class serve() installs


def test_a_home_too_long_for_a_socket_runs_in_process_without_waiting(monkeypatch):
    monkeypatch.setattr(daemon, "socket_path", lambda ident: Path("/" + "x" * 120) / "s.sock")
    monkeypatch.setattr(daemon, "start", _real_start)  # conftest's stub would hide the length check
    monkeypatch.setenv("WEBCLI_DAEMON", "1")

    async def models(**kwargs):
        return ["in-process"]

    monkeypatch.setattr(fetch, "models", models)
    started = asyncio.run(_timed(daemon.call(IDENT, "chatgpt", "models", headed=False)))
    assert started[0] == ["in-process"] and started[1] < 2  # not START_SECONDS
