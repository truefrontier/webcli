"""Offline tests (from gweb and cgpt) for the core browser: private dirs, the identity profile lock (and its wait),
signals, and Playwright's protocol logging."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import stat
import threading
import time

import pytest

from webcli.core import browser, config
from webcli.core.identities import Identity


def test_private_dir_is_0700_even_if_it_existed(tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    for path in (config.private_dir(tmp_path / "a" / "b"), config.private_dir(loose)):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


IDENT = Identity("google:default")


@pytest.fixture
def home():
    return IDENT.path


def _profile_with_lock(path, target: str):
    path.mkdir(parents=True, exist_ok=True)
    (path / "SingletonLock").symlink_to(target)
    return path


def test_singleton_lock_states(tmp_path):
    assert browser.chrome_holds_profile(tmp_path) is False
    live = _profile_with_lock(tmp_path / "live", f"{socket.gethostname()}-{os.getpid()}")
    other_host = _profile_with_lock(tmp_path / "other", f"elsewhere.local-{os.getpid()}")
    stale = _profile_with_lock(tmp_path / "stale", f"{socket.gethostname()}-4000000")  # above pid limits
    assert [browser.chrome_holds_profile(p) for p in (live, other_host, stale)] == [True, True, False]


def lock(wait):
    """Enter and leave profile_lock(wait) once, from sync test code."""

    async def enter():
        async with browser.profile_lock(IDENT, wait=wait):
            pass

    asyncio.run(enter())


def test_profile_lock_blocks_a_second_run(home):
    async def nested():
        async with browser.profile_lock(IDENT, wait=0):
            with pytest.raises(browser.ProfileBusy, match="Another command is using the google:default profile"):
                async with browser.profile_lock(IDENT, wait=0):
                    pass

    asyncio.run(nested())
    lock(0)  # released after the first run
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


def _hold_lock(seconds: float, ready: threading.Event) -> None:
    async def hold():
        async with browser.profile_lock(IDENT, wait=0):
            ready.set()
            await asyncio.sleep(seconds)

    asyncio.run(hold())


def _holder(seconds: float) -> threading.Thread:
    ready = threading.Event()
    thread = threading.Thread(target=_hold_lock, args=(seconds, ready))
    thread.start()
    assert ready.wait(2)
    return thread


def test_lock_wait_gets_the_profile_once_released(home):
    holder = _holder(0.6)
    started = time.monotonic()
    lock(5)
    waited = time.monotonic() - started
    holder.join()
    assert 0.3 < waited < 4


def test_lock_wait_gives_up_after_the_deadline(home):
    holder = _holder(1.5)
    started = time.monotonic()
    with pytest.raises(browser.ProfileBusy, match=r"waited 0\.4s"):
        lock(0.4)
    assert time.monotonic() - started < 1.4
    holder.join()


def test_lock_wait_defaults_to_the_module_setting(home, monkeypatch):
    monkeypatch.setattr(browser, "lock_wait", 0.2)
    holder = _holder(1.0)
    with pytest.raises(browser.ProfileBusy, match=r"waited 0\.2s"):
        lock(None)
    holder.join()


@pytest.mark.parametrize("bad", ["nan", "inf", "-1", float("nan")])
def test_lock_wait_must_be_finite(bad):
    with pytest.raises(ValueError):
        browser.parse_wait(bad)
    assert browser.parse_wait("2.5") == 2.5


def test_profile_lock_refuses_when_chrome_is_running(home):
    _profile_with_lock(IDENT.profile, f"{socket.gethostname()}-{os.getpid()}")
    with pytest.raises(browser.ProfileBusy, match="Chrome is already running"):
        lock(0.3)


def test_sigterm_while_waiting_for_the_lock_is_not_lost(home):
    holder = _holder(2.0)

    async def command():
        os.kill(os.getpid(), signal.SIGTERM)
        async with browser.profile_lock(IDENT, wait=1.5):
            pytest.fail("got the lock despite SIGTERM")

    started = time.monotonic()
    with pytest.raises(browser.Interrupted):
        browser.run(command())
    assert time.monotonic() - started < 1.0  # cancelled mid-wait, not after the 1.5s deadline
    holder.join()


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_signal_unwinds_cleanup_and_releases_the_lock(home, signum):
    cleaned = []

    async def command():
        async with browser.profile_lock(IDENT, wait=0):
            try:
                os.kill(os.getpid(), signum)
                await asyncio.sleep(5)
            finally:
                cleaned.append("chrome closed")

    started = time.monotonic()
    with pytest.raises(browser.Interrupted) as caught:
        browser.run(command())
    assert caught.value.signum == signum
    assert cleaned == ["chrome closed"]
    assert time.monotonic() - started < 3
    lock(0)  # the lock was released on the way out
    assert signal.getsignal(signum) == signal.SIG_DFL  # handlers removed after the run


def test_run_returns_the_result_without_signals():
    async def ok():
        return 42

    assert browser.run(ok()) == 42


def test_profile_lock_lives_in_the_private_run_dir(home):
    lock(0)
    assert (config.home() / "run" / "google-default.profile.lock").exists()
    assert stat.S_IMODE((config.home() / "run").stat().st_mode) == 0o700


def test_two_identities_never_share_a_lock():
    async def both():
        async with browser.profile_lock(IDENT, wait=0):
            async with browser.profile_lock(Identity("google:work"), wait=0):
                return True

    assert asyncio.run(both())


def test_playwright_protocol_logging_is_switched_off(monkeypatch):
    """DEBUG=pw:protocol would print request headers (the bearer token) to stderr; chrome() drops it."""
    from contextlib import asynccontextmanager

    monkeypatch.setenv("DEBUG", "pw:protocol")

    @asynccontextmanager
    async def no_lock(ident, wait=None):
        raise RuntimeError("stop before Chrome starts")
        yield

    monkeypatch.setattr(browser, "profile_lock", no_lock)

    async def scenario():
        async with browser.open_page(None, mode="headless", ident=IDENT):
            pass

    with pytest.raises(RuntimeError, match="stop before Chrome"):
        asyncio.run(scenario())
    assert "DEBUG" not in os.environ


def test_user_agent_drops_headless(tmp_path):
    import plistlib
    plist = tmp_path / "Info.plist"
    plist.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "155.0.8059.80"}))
    ua = browser.user_agent(plist)
    assert "Chrome/155.0.0.0 Safari/537.36" in ua and "Headless" not in ua
    assert browser.user_agent(tmp_path / "missing.plist") is None
