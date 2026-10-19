"""Browser-side work for webcli keep: status and the notes sync. READ-ONLY.

keep.google.com syncs its notes over notes/v1/changes while it loads. `notes` opens the app in a fresh headless tab
and reads those answers (INTERCEPT); it never sends a request of its own. The guard lets a changes POST through only
when it uploads nothing (endpoints.py).
"""

from __future__ import annotations

import asyncio

from webcli.core import browser
from webcli.sites import google
from webcli.sites.keep import parse
from webcli.sites.keep.endpoints import APP_URL, SETTLE_MS, SYNC_SECONDS
from webcli.sites.keep.guard import POLICY, is_changes

SNIFF_HINT = ("Keep's page may have changed; run `python -m webcli.core.capture keep` and fix "
              "webcli/sites/keep/endpoints.py.")


async def status(*, headed: bool = False) -> dict:
    """{signedIn, email}. headed is accepted for the daemon contract; data sites always run headless."""
    return await google.status(POLICY, APP_URL)


async def notes(*, headed: bool = False) -> dict:
    """{notes, complete}: every top-level note and list Keep's sync sent (archived included), most recently edited
    first. complete is false when Keep never said its sync was done (truncated: false) within SYNC_SECONDS: the list
    may be short."""
    found: dict[str, dict] = {}
    done = asyncio.get_running_loop().create_future()
    answers = 0
    reading: set[asyncio.Task] = set()

    def on_response(response) -> None:
        if response.request.method == "POST" and is_changes(response.url):
            task = asyncio.ensure_future(read(response))
            reading.add(task)
            task.add_done_callback(reading.discard)

    async def read(response) -> None:
        nonlocal answers
        try:
            data = await response.json()
        except Exception:
            return
        if not isinstance(data, dict):
            return
        answers += 1
        for node in data.get("nodes") or []:
            row = parse.note(node)
            if row and row["id"]:
                found[row["id"]] = row
        if data.get("truncated") is False and not done.done():
            done.set_result(True)

    async with browser.site_page(POLICY) as page:
        page.on("response", on_response)
        await google.open_app(page, APP_URL)
        try:
            await asyncio.wait_for(asyncio.shield(done), SYNC_SECONDS)
        except asyncio.TimeoutError:
            pass
        await page.wait_for_timeout(SETTLE_MS)
        await asyncio.gather(*reading, return_exceptions=True)  # answers still being read, before the tab closes
        if not answers:
            raise RuntimeError(f"Keep sent no notes sync within {SYNC_SECONDS}s.{google.blocked()} {SNIFF_HINT}")
    rows = sorted(found.values(), key=lambda row: row["updated"], reverse=True)
    return {"notes": rows, "complete": done.done()}
