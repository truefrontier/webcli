"""The installed Google Chrome on an identity's persistent profile: launch policy, the profile lock, the request
guard's routes, and signal handling that always closes Chrome. Ported from cgpt's browser.py (the stricter one).

Hidden-browser policy: every command runs Chrome headless, which shows no window, no Dock icon, and never takes
focus. Only `identity login` and an explicit --headed use a visible Chrome ("visible" mode). There is no headed
fallback: on macOS every headed variant tried (2026-10-18: --window-position=-32000,-32000, `open -g -j`, a hidden
app, CDP window bounds) still put a window on screen or took focus, so a check headless Chrome can't pass raises
Challenged instead. Inside the daemon (shared is set) a visible Chrome is refused outright.

Fingerprint: headless Chrome's own user agent says "HeadlessChrome", so every headless page presents as the installed
Chrome instead (user_agent, ua_metadata, present_as_chrome). The cf_clearance a person earns in chatgpt's visible
window has to hold up for headless commands, so that window presents exactly what headless does (match_headless):
the same user agent, client hints, viewport, screen and devicePixelRatio.

H2: Playwright starts Chrome with --use-mock-keychain on macOS, which encrypts cookies with a fixed, public key.
Dropping it (IGNORED_DEFAULT_ARGS) makes Chrome keep cookies under its own "Chrome Safe Storage" Keychain key, like
any Chrome profile. webcli itself never reads the Keychain or any cookie value; Chrome does, as it always does.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import itertools
import json
import math
import os
import platform
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
# Every headless page's size. Playwright also emulates screen = viewport and devicePixelRatio 1 for it.
VIEWPORT = {"width": 1280, "height": 900}
# present_as_chrome: each new cross-site iframe and worker waits, paused, for its override.
AUTO_ATTACH = {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": False}
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


def chrome_version(plist: Path = CHROME_PLIST) -> str | None:
    """The installed Chrome's full version ("155.0.8059.80"), or None when it can't be read."""
    try:
        version = plistlib.loads(plist.read_bytes())["CFBundleShortVersionString"]
    except (OSError, KeyError, ValueError, plistlib.InvalidFileException):
        return None
    return version if version.split(".")[0].isdigit() else None


def user_agent(plist: Path = CHROME_PLIST) -> str | None:
    """Chrome's own user agent without "HeadlessChrome": Cloudflare answers headless Chrome's UA with its "Just a
    moment..." check, which never clears. Chrome sends a reduced UA (major version, then .0.0.0); None (no
    override) when Chrome's version can't be read."""
    version = chrome_version(plist)
    if version is None:
        return None
    return (f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{version.split('.')[0]}.0.0.0 Safari/537.36")


def brands(version: str, *, full: bool = False) -> list[dict]:
    """Google Chrome's brand list for `version`, its GREASE brand included, as Chrome builds it (Chromium's
    GenerateBrandVersionList, seeded by the major version): Sec-CH-UA, or with `full` Sec-CH-UA-Full-Version-List."""
    seed = int(version.split(".")[0])
    chars = " (:-./);=?_"
    grease = ("8", "99", "24")[seed % 3]
    entries = [(f"Not{chars[seed % 11]}A{chars[(seed + 1) % 11]}Brand", f"{grease}.0.0.0" if full else grease),
               ("Chromium", version if full else str(seed)), ("Google Chrome", version if full else str(seed))]
    order = ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0))[seed % 6]
    ordered = [{}] * 3
    for slot, (brand, brand_version) in zip(order, entries):
        ordered[slot] = {"brand": brand, "version": brand_version}
    return ordered


def ua_metadata(version: str) -> dict:
    """The User-Agent Client Hints this Mac's visible Chrome `version` sends. Playwright derives them from
    user_agent()'s frozen "Intel Mac OS X 10_15_7", so on its own a headless page claims x86 and macOS "10_15_7"
    (a form no real Chrome sends) while a visible one says arm and "26.6.2". The brands are spelled out: a worker
    inheriting an override without them reports none."""
    macos = (platform.mac_ver()[0].split(".") + ["0", "0"])[:3]  # Chrome always sends major.minor.patch
    return {"brands": brands(version), "fullVersionList": brands(version, full=True), "fullVersion": version,
            "platform": "macOS", "platformVersion": ".".join(macos),
            "architecture": "arm" if platform.machine() == "arm64" else "x86",
            "model": "", "mobile": False, "bitness": "64", "wow64": False}


async def present_as_chrome(context: "BrowserContext", page: "Page") -> None:
    """Pin `page`'s user agent and client hints to user_agent() and ua_metadata(), what a real Chrome sends:
    Cloudflare checks them together. Every headless page gets this, and so does a match_headless visible window
    (chatgpt's), so the cf_clearance a person earns there was earned with what headless commands present later.
    Auto-attach pauses each cross-site iframe (Cloudflare's Turnstile), however deeply nested, and each worker until it
    has the same override, so none runs with Playwright's own (x86, macOS "10_15_7"). Non-flat sessions: Playwright's
    CDPSession can't address flat child sessions, so a nested target is reached through each session above it."""
    version, ua = chrome_version(), user_agent()
    if version is None or ua is None:
        return
    override = {"userAgent": ua, "userAgentMetadata": ua_metadata(version)}
    cdp = await context.new_cdp_session(page)
    ids = itertools.count(1)

    async def send(path: tuple[str, ...], method: str, params: dict) -> None:
        """`method` to the target at the end of `path` (session ids, outermost first), wrapped once per level."""
        for session_id in reversed(path):
            params = {"sessionId": session_id,
                      "message": json.dumps({"id": next(ids), "method": method, "params": params})}
            method = "Target.sendMessageToTarget"
        with contextlib.suppress(Exception):  # a worker has no Emulation, a target may be gone: neither is fatal
            await cdp.send(method, params)

    async def attached(path: tuple[str, ...]) -> None:
        try:
            await send(path, "Emulation.setUserAgentOverride", override)
            await send(path, "Target.setAutoAttach", AUTO_ATTACH)  # its own cross-site iframes and workers
        finally:
            await send(path, "Runtime.runIfWaitingForDebugger", {})  # unreleased, it stays paused

    async def received(event: dict, path: tuple[str, ...] = ()) -> None:
        """An event from a nested target: its attachments are paused targets one level deeper."""
        path, message = (*path, event["sessionId"]), json.loads(event["message"])
        if message.get("method") == "Target.attachedToTarget":
            await attached((*path, message["params"]["sessionId"]))
        elif message.get("method") == "Target.receivedMessageFromTarget":
            await received(message["params"], path)

    cdp.on("Target.attachedToTarget", lambda event: attached((event["sessionId"],)))
    cdp.on("Target.receivedMessageFromTarget", received)
    await cdp.send("Emulation.setUserAgentOverride", override)
    await cdp.send("Target.setAutoAttach", AUTO_ATTACH)


def launch_options(profile: Path, mode: str, *, guarded: bool = True, match_headless: bool = False) -> dict:
    """launch_persistent_context's arguments. The one place headless is decided: mode "headless" (every command) or
    "visible" (`identity login`, --headed). Unguarded (only chatgpt's sign-in window) allows service workers;
    guarded blocks them so every request goes through the guard.

    match_headless (chatgpt's visible windows) keeps the headless VIEWPORT, so the page's screen, inner size and
    devicePixelRatio read as they do headless. Its user agent and client hints are set per page instead
    (present_as_chrome, from page_at): Playwright's launch-level user agent brings client hints it derives itself
    (x86, macOS "10_15_7", no brands) to every other tab, where a visible Chrome's own are already right."""
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
        "viewport": dict(VIEWPORT) if headless or match_headless else None,
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
                 wait: float | None = None, match_headless: bool = False) -> AsyncIterator["BrowserContext"]:
    """Launch system Chrome on the identity's profile, holding the profile lock, and yield its context. Always closes
    Chrome. Guarded (everything but chatgpt's sign-in window): the context route aborts every request that doesn't
    come from the running command's page (page_at), and every WebSocket. match_headless: see launch_options."""
    from playwright.async_api import async_playwright

    ident = ident or current()
    if mode != "headless" and shared is not None:
        raise RuntimeError("The daemon never opens a visible Chrome.")
    # DEBUG=pw:protocol (or pw:*, or *) would make Playwright log protocol traffic, request headers and their bearer
    # token included, to stderr, past redaction. Nothing here needs it.
    os.environ.pop("DEBUG", None)
    options = launch_options(ident.profile, mode, guarded=guarded, match_headless=match_headless)
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
                  ready: Ready | None = None, match_headless: bool = False) -> AsyncIterator["Page"]:
    """One command's page in `context`: Chrome's first tab, or a fresh one closed afterwards (new=True: the daemon's
    long-lived Chrome). The guard's state is this page's for exactly as long as it is yielded: `policy` (None:
    unguarded, chatgpt's sign-in window), send only for `ask` / `-p`, download only for `--image-out`, sign_in only
    for `identity login`. With `url` the page goes there first, then `ready(page, mode)` may refuse it (chatgpt's
    Cloudflare check raises Challenged) before anything is yielded. A policy that lists sockets gets them on this
    page only, closed when it is done. A headless page, or a match_headless visible one, presents as Chrome
    (present_as_chrome) before it loads anything."""
    page = None
    try:
        page = await context.new_page() if new or not context.pages else context.pages[0]
        if mode == "headless" or match_headless:
            await present_as_chrome(context, page)
        if policy is not None:
            lease = guard.state.begin(policy, send=send, download=download, sign_in=sign_in)
            await page.route("**/*", guard.page_route(lease))
            if policy.sockets is not None:
                await page.route_web_socket(lambda _url: True, guard.page_websocket(lease))
        if url:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            if ready is not None:
                await ready(page, mode)
        yield page
    finally:
        guard.state.end()  # first: revokes the lease, so no socket connects while the command's own close
        try:
            await guard.close_sockets()
        finally:
            if new and page is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(page.close(), timeout=5)


@asynccontextmanager
async def open_page(policy: Policy | None, *, mode: str = "headless", ident: Identity | None = None,
                    match_headless: bool = False, **page_options) -> AsyncIterator["Page"]:
    """chrome() + page_at(): a Chrome of this command's own, closed when the page is done."""
    async with chrome(ident, mode=mode, guarded=policy is not None, match_headless=match_headless) as context:
        async with page_at(context, policy, mode=mode, match_headless=match_headless, **page_options) as page:
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
