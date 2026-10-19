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


class FakeChromium:
    def __init__(self, launches: list[dict]) -> None:
        self.launches = launches

    async def launch_persistent_context(self, **options):
        self.launches.append(options)
        raise LaunchStopped("fake playwright: launch recorded, no Chrome started")


class FakePlaywright:
    def __init__(self, launches: list[dict]) -> None:
        self.chromium = FakeChromium(launches)

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
