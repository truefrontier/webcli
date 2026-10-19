"""Every test runs offline: a temp WEBCLI_HOME, no daemon, no system tools, and no real browser.

A test that needs to see a launch swaps in `fake_playwright` (it records launch_persistent_context's arguments and
stops there); anything else that reaches Playwright fails loudly.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from webcli.core import browser, daemon, guard, hardening, output
from webcli.core.identities import Identity


class LaunchStopped(RuntimeError):
    """Raised by the fake Playwright once it has recorded a launch: no Chrome ever starts."""


class FakeCDP:
    """A CDP session: records each send as (method, params) in `sent` and keeps each event handler."""

    def __init__(self, sent: list) -> None:
        self.sent, self.handlers = sent, {}

    def on(self, event: str, handler) -> None:
        self.handlers[event] = handler

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.sent.append((method, params))
        return {}


class FakePage:
    """A tab that loads nothing: goto only moves .url, and every evaluate answers like signed-out chatgpt.com."""

    def __init__(self, context: "FakeContext") -> None:
        self.context, self.url = context, "about:blank"

    async def goto(self, url, **_options):
        self.url = url

    async def evaluate(self, *_args):
        return {"title": "ChatGPT", "url": self.url, "app": True, "status": 200, "signedIn": False}

    async def title(self):
        return "ChatGPT"

    def is_closed(self):
        return False

    async def route(self, *_args):
        pass

    async def route_web_socket(self, *_args):
        pass

    async def close(self):
        pass


class FakeContext:
    """A launched Chrome with one tab. `cdp`: every CDP send, from every session, in order."""

    def __init__(self) -> None:
        self.cdp: list = []
        self.sessions: list[FakeCDP] = []
        self.pages = [FakePage(self)]

    async def new_page(self):
        self.pages.append(FakePage(self))
        return self.pages[-1]

    async def new_cdp_session(self, _page):
        self.sessions.append(FakeCDP(self.cdp))
        return self.sessions[-1]

    async def route(self, *_args):
        pass

    async def route_web_socket(self, *_args):
        pass

    async def close(self):
        pass


class FakeChromium:
    def __init__(self, launches: list[dict], contexts: list[FakeContext] | None = None) -> None:
        self.launches, self.contexts = launches, contexts

    async def launch_persistent_context(self, **options):
        self.launches.append(options)
        if self.contexts is None:
            raise LaunchStopped("fake playwright: launch recorded, no Chrome started")
        self.contexts.append(FakeContext())
        return self.contexts[-1]


class FakePlaywright:
    def __init__(self, launches: list[dict], contexts: list[FakeContext] | None = None) -> None:
        self.chromium = FakeChromium(launches, contexts)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _no_browser():
    raise AssertionError("tests never launch a real browser: use the fake_playwright fixture")


def _no_tool(args, **_kwargs):
    """hardening's tmutil/fdesetup, answered offline as a Mac with FileVault on and exclusions working."""
    if args[:2] == ["tmutil", "isexcluded"]:
        return subprocess.CompletedProcess(args, 0, f"[Excluded]    {args[2]}\n", "")
    if args[:2] == ["fdesetup", "status"]:
        return subprocess.CompletedProcess(args, 0, "FileVault is On.\n", "")
    return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("WEBCLI_HOME", str(home))
    monkeypatch.setenv("WEBCLI_DAEMON", "0")
    for var in ("WEBCLI_IDENTITY", "LLMCLI_FORMAT", "WEBCLI_FORMAT", "WEBCLI_LOCK_WAIT", "WEBCLI_SERVE_RECYCLE_HOURS"):
        monkeypatch.delenv(var, raising=False)
    import playwright.async_api

    monkeypatch.setattr(playwright.async_api, "async_playwright", _no_browser)
    monkeypatch.setattr(hardening, "_run", lambda args: _no_tool(list(args)))
    monkeypatch.setattr(daemon, "start", lambda ident: (_ for _ in ()).throw(AssertionError("no daemon in tests")))
    monkeypatch.setattr(browser, "identity", None)
    monkeypatch.setattr(browser, "shared", None)
    monkeypatch.setattr(browser, "lock_wait", 30.0)
    monkeypatch.setattr(guard, "state", guard.Guard())
    monkeypatch.setattr(output, "APP", "llmcli")
    monkeypatch.setattr(output, "FORMAT_ENV", "LLMCLI_FORMAT")
    yield home


@pytest.fixture
def tool_calls(monkeypatch):
    calls: list[list[str]] = []

    def tool(args):
        calls.append(list(args))
        return _no_tool(list(args))

    monkeypatch.setattr(hardening, "_run", tool)
    return calls


@pytest.fixture
def fake_playwright(monkeypatch):
    """The list launch_persistent_context's keyword arguments land in, one dict per launch."""
    import playwright.async_api

    launches: list[dict] = []
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: FakePlaywright(launches))
    return launches


@pytest.fixture
def fake_chrome(monkeypatch):
    """Like fake_playwright, but the launch succeeds with a FakeContext: (launches, contexts), one entry per launch.
    Chrome's version reads as 155.0.8059.80 wherever the tests run."""
    import playwright.async_api

    launches: list[dict] = []
    contexts: list[FakeContext] = []
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: FakePlaywright(launches, contexts))
    monkeypatch.setattr(browser, "chrome_version", lambda _plist=None: "155.0.8059.80")
    return launches, contexts


def make_profile(identity_id: str) -> Identity:
    """A non-empty chrome-profile, so commands treat the identity as having a session."""
    ident = Identity(identity_id)
    ident.profile.mkdir(parents=True, exist_ok=True)
    (ident.profile / "Local State").write_text("{}")
    return ident


@pytest.fixture
def google_profile() -> Identity:
    return make_profile("google:default")


@pytest.fixture
def chatgpt_profile() -> Identity:
    return make_profile("chatgpt:default")


FIXTURES = Path(__file__).parent / "fixtures"
