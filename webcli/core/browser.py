"""The installed Google Chrome on an identity's persistent profile: launch policy, the profile lock, the request
guard's routes, and signal handling that always closes Chrome. Ported from cgpt's browser.py (the stricter one).

Hidden-browser policy: every command runs Chrome headless, which shows no window, no Dock icon, and never takes
focus. Only `identity login` and an explicit --headed use a visible Chrome ("visible" mode). There is no headed
fallback: on macOS every headed variant tried (2026-10-18: --window-position=-32000,-32000, `open -g -j`, a hidden
app, CDP window bounds) still put a window on screen or took focus, so a check headless Chrome can't pass raises
Challenged instead. Inside the daemon (shared is set) a visible Chrome is refused outright.

H2: Playwright starts Chrome with --use-mock-keychain on macOS, which encrypts cookies with a fixed, public key.
Dropping it (IGNORED_DEFAULT_ARGS) makes Chrome keep cookies under its own "Chrome Safe Storage" Keychain key, like
any Chrome profile. webcli itself never reads the Keychain or any cookie value; Chrome does, as it always does.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import math
import os
import plistlib
import signal
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable

from webcli.core import config, guard
from webcli.core.errors import Interrupted, ProfileBusy
from webcli.core.guard import Policy
from webcli.core.identities import Identity

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page

MODES = ("headless", "visible")
STEALTH = ["--disable-blink-features=AutomationControlled"]
IGNORED_DEFAULT_ARGS = ["--enable-automation", "--use-mock-keychain"]
CHROME_PLIST = Path("/Applications/Google Chrome.app/Contents/Info.plist")
LOCK_WAIT_ENV = "WEBCLI_LOCK_WAIT"

# Seconds a command waits for another command (or its Chrome) to let go of the profile. Set by --lock-wait.
lock_wait: float = 30.0

# The identity this command runs as. Set by the site group (llmcli <site> [--identity ID]).
identity: Identity | None = None

# In the daemon: its long-lived Chrome (daemon.Shared). site_page takes pages from it.
shared: Any = None


def current() -> Identity:
    if identity is None:
        raise RuntimeError("No identity selected for this command.")
    return identity


def parse_wait(value: str | float) -> float:
    """A lock wait in seconds: finite and >= 0 (NaN would never time out)."""
    wait = float(value)
    if not math.isfinite(wait) or wait < 0:
        raise ValueError(f"lock wait must be a finite number of seconds >= 0, got {value!r}")
    return wait


def user_agent(plist: Path = CHROME_PLIST) -> str | None:
    """Chrome's own user agent without "HeadlessChrome": Cloudflare answers headless Chrome's UA with its "Just a
    moment..." check, which never clears. Chrome sends a reduced UA (major version, then .0.0.0); None (no
    override) when Chrome's version can't be read."""
    try:
        version = plistlib.loads(plist.read_bytes())["CFBundleShortVersionString"]
    except (OSError, KeyError, ValueError, plistlib.InvalidFileException):
        return None
    major = version.split(".")[0]
    if not major.isdigit():
        return None
    return (f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{major}.0.0.0 Safari/537.36")


def launch_options(profile: Path, mode: str, *, guarded: bool = True) -> dict:
    """launch_persistent_context's arguments. The one place headless is decided: mode "headless" (every command) or
    "visible" (`identity login`, --headed). Unguarded (only chatgpt's sign-in window) allows service workers;
    guarded blocks them so every request goes through the guard."""
    if mode not in MODES:
        raise ValueError(f"unknown Chrome mode {mode!r}")
    headless = mode == "headless"
    return {
        "user_data_dir": str(profile),
        "headless": headless,
        "channel": "chrome",  # the installed Google Chrome, never Playwright's bundled Chromium
        "args": list(STEALTH),
        "ignore_default_args": list(IGNORED_DEFAULT_ARGS),
        "service_workers": "block" if guarded else "allow",
        "viewport": {"width": 1280, "height": 900} if headless else None,
        "accept_downloads": False,
        "user_agent": user_agent() if headless else None,
    }


def chrome_holds_profile(profile: Path) -> bool:
    """True when Chrome's SingletonLock (a "host-pid" symlink) points at a live process."""
    try:
        target = os.readlink(profile / "SingletonLock")
    except FileNotFoundError:
        return False
    except OSError:
        return True  # exists but is not a symlink: let Chrome's owner sort it out
    host, _, pid = target.rpartition("-")
    if host != socket.gethostname() or not pid.isdigit():
        return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False  # stale lock from a crashed Chrome; Chrome reclaims it
    except PermissionError:
        return True
    return True


def _try_lock(handle, profile: Path) -> str | None:
    """One attempt. None means we hold the flock and no Chrome has the profile; else who has it."""
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return "webcli"
    if chrome_holds_profile(profile):
        fcntl.flock(handle, fcntl.LOCK_UN)  # a previous run's Chrome may still be exiting
        return "chrome"
    return None


@asynccontextmanager
async def profile_lock(ident: Identity, wait: float | None = None) -> AsyncIterator[None]:
    """Hold run/<identity>.profile.lock so two commands never share the identity's profile.

    Polls for up to `wait` seconds (default: lock_wait) while another command or its Chrome has it.
    The poll awaits, so SIGTERM/SIGHUP still cancel a command that is waiting here.
    """
    wait = parse_wait(lock_wait if wait is None else wait)
    config.private_dir(ident.path)  # the profile holds the session cookies
    config.run_dir()
    deadline = time.monotonic() + wait
    with open(ident.run_file(".profile.lock"), "w") as handle:
        while holder := _try_lock(handle, ident.profile):
            if time.monotonic() >= deadline:
                if holder == "chrome":
                    raise ProfileBusy(
                        f"Chrome is already running on {ident.profile}. Close that window and retry. "
                        f"If no Chrome is open, remove {ident.profile / 'SingletonLock'}."
                    )
                raise ProfileBusy(
                    f"Another command is using the {ident.id} profile (waited {wait:g}s). "
                    f"Retry, or raise --lock-wait / {LOCK_WAIT_ENV}."
                )
            await asyncio.sleep(0.25)
        yield


def run(coro):
    """asyncio.run, but SIGTERM/SIGHUP (tmux kill-session, tmuxlet stop) cancel the command first.

    Cancelling unwinds the page and Chrome, so Chrome closes and the profile lock is released. Then
    Interrupted is raised. Ctrl-C keeps asyncio's own handling (cancel, then KeyboardInterrupt).
    """
    received: list[int] = []

    async def main():
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()

        def on_signal(signum: int) -> None:
            received.append(signum)
            task.cancel()

        for signum in (signal.SIGTERM, signal.SIGHUP):
            loop.add_signal_handler(signum, on_signal, signum)
        try:
            return await coro
        finally:
            for signum in (signal.SIGTERM, signal.SIGHUP):
                loop.remove_signal_handler(signum)

    try:
        return asyncio.run(main())
    except asyncio.CancelledError:
        if received:
            raise Interrupted(received[0]) from None
        raise


@asynccontextmanager
async def chrome(ident: Identity | None = None, *, mode: str = "headless", guarded: bool = True,
                 wait: float | None = None) -> AsyncIterator["BrowserContext"]:
    """Launch system Chrome on the identity's profile, holding the profile lock, and yield its context. Always closes
    Chrome. Guarded (everything but chatgpt's sign-in window): the context route aborts every request that doesn't
    come from the running command's page (page_at), and every WebSocket."""
    from playwright.async_api import async_playwright

    ident = ident or current()
    if mode != "headless" and shared is not None:
        raise RuntimeError("The daemon never opens a visible Chrome.")
    # DEBUG=pw:protocol (or pw:*, or *) would make Playwright log protocol traffic, request headers and their bearer
    # token included, to stderr, past redaction. Nothing here needs it.
    os.environ.pop("DEBUG", None)
    options = launch_options(ident.profile, mode, guarded=guarded)
    async with profile_lock(ident, wait):
        config.private_dir(ident.profile)
        async with async_playwright() as p:
            try:
                context = await p.chromium.launch_persistent_context(**options)
            except Exception as exc:
                if "singleton" in str(exc).lower() or "already in use" in str(exc).lower():
                    raise ProfileBusy(f"Chrome refused the profile at {ident.profile}: it is in use.") from exc
                raise
            try:
                if guarded:
                    await context.route("**/*", guard.context_route)
                    await context.route_web_socket(lambda _url: True, guard.websocket)
                yield context
            finally:
                # Cleanup must also be bounded when an ask deadline cancels a UI wait.
                await asyncio.wait_for(context.close(), timeout=5)


Ready = Callable[["Page", str], Awaitable[None]]


@asynccontextmanager
async def page_at(context: "BrowserContext", policy: Policy | None, *, mode: str = "headless", url: str | None = None,
                  send: bool = False, download: bool = False, sign_in: bool = False, new: bool = False,
                  ready: Ready | None = None) -> AsyncIterator["Page"]:
    """One command's page in `context`: Chrome's first tab, or a fresh one closed afterwards (new=True: the daemon's
    long-lived Chrome). The guard's state is this page's for exactly as long as it is yielded: `policy` (None:
    unguarded, chatgpt's sign-in window), send only for `ask` / `-p`, download only for `--image-out`, sign_in only
    for `identity login`. With `url` the page goes there first, then `ready(page, mode)` may refuse it (chatgpt's
    Cloudflare check raises Challenged) before anything is yielded."""
    page = None
    try:
        page = await context.new_page() if new or not context.pages else context.pages[0]
        if policy is not None:
            lease = guard.state.begin(policy, send=send, download=download, sign_in=sign_in)
            await page.route("**/*", guard.page_route(lease))
        if url:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            if ready is not None:
                await ready(page, mode)
        yield page
    finally:
        guard.state.end()
        if new and page is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(page.close(), timeout=5)


@asynccontextmanager
async def open_page(policy: Policy | None, *, mode: str = "headless", ident: Identity | None = None,
                    **page_options) -> AsyncIterator["Page"]:
    """chrome() + page_at(): a Chrome of this command's own, closed when the page is done."""
    async with chrome(ident, mode=mode, guarded=policy is not None) as context:
        async with page_at(context, policy, mode=mode, **page_options) as page:
            yield page


@asynccontextmanager
async def site_page(policy: Policy, *, mode: str = "headless", **page_options) -> AsyncIterator["Page"]:
    """The page a site's command works in: a fresh tab in the daemon's Chrome inside `serve` (headless only), else
    a Chrome of this command's own: headless, or visible for --headed / sign-in."""
    if shared is not None:
        if mode != "headless":
            raise RuntimeError("The daemon never opens a visible Chrome.")
        async with shared.page(policy, **page_options) as page:
            yield page
        return
    async with open_page(policy, mode=mode, **page_options) as page:
        yield page
