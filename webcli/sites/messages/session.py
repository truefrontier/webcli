"""One Google Messages web session at a time, across every process and identity.

Google Messages keeps one active web session per phone: a second tab or computer that opens it takes the session (or
asks to "Use here"), and the household rule is that concurrent sessions steal pairing. Every `webcli messages`
command already runs one at a time per identity (the daemon's turn, and the profile lock whenever Chrome is open),
but a second identity, a `WEBCLI_DAEMON=0` run, or a second daemon would each open their own session. So every page
that loads messages.google.com holds run/messages.session.lock (an exclusive flock) for as long as the app is loaded:
taken before the page navigates there, released only after the page has left it (about:blank).

A command that finds the lock held waits (polling, so SIGTERM still cancels it) for its --lock-wait, then fails with
ProfileBusy, error "messages_busy" (exit 5), naming the holder's pid when it wrote one.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from webcli.core import browser, config
from webcli.core.errors import ProfileBusy
from webcli.sites.messages.endpoints import SESSION_LOCK

POLL_SECONDS = 0.25
BUSY = "messages_busy"


def lock_path() -> Path:
    return config.run_dir() / SESSION_LOCK


def _wait() -> float:
    """This command's --lock-wait: the request's own inside the daemon, else the CLI's."""
    if browser.shared is not None:
        from webcli.core import daemon

        return daemon.request_wait.get()
    return browser.lock_wait


def _holder(path: Path) -> str:
    try:
        pid = path.read_text().strip()
    except OSError:
        return ""
    return f" (pid {pid})" if pid.isdigit() else ""


@asynccontextmanager
async def hold(wait: float | None = None) -> AsyncIterator[None]:
    """Hold the Messages session lock for the block. ProfileBusy (error "messages_busy") after `wait` seconds."""
    wait = browser.parse_wait(_wait() if wait is None else wait)
    path = lock_path()
    deadline = time.monotonic() + wait
    with open(path, "a+") as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ProfileBusy(
                        f"Google Messages is busy: another webcli messages command{_holder(path)} has the one "
                        f"Messages web session open (waited {wait:g}s). Nothing was opened. Retry when it finishes, "
                        f"or raise --lock-wait / {browser.LOCK_WAIT_ENV}.", error=BUSY) from None
                await asyncio.sleep(POLL_SECONDS)
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n")
            handle.flush()
            yield
        finally:
            with contextlib.suppress(OSError):
                handle.seek(0)
                handle.truncate()
                handle.flush()
            fcntl.flock(handle, fcntl.LOCK_UN)
