"""Browser-side work for webcli loom (loom-cli's on the shared core): login, status, the library, one video, and its
transcript. READ-ONLY.

`videos` and `video` run the same GraphQL queries Loom's library runs (GetLoomsForLibrary, GetVideoCardDetails) as
an in-page fetch from the signed-in library (COOKIE). `transcript` opens the share page and reads the official
phrase file the page fetches for itself (INTERCEPT). Every command but `login` sits behind guard.POLICY, which lets
only GraphQL queries through.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlsplit

from webcli.core import browser, guard
from webcli.core.errors import AuthRequired, NotFound
from webcli.sites.loom import parse
from webcli.sites.loom.endpoints import (
    APP_URL,
    GRAPHQL_HEADERS,
    GRAPHQL_URL,
    LOGIN_URL,
    SHARE_URL,
    TRANSCRIPT_MARK,
    TRANSCRIPT_SECONDS,
)
from webcli.sites.loom.guard import POLICY

if TYPE_CHECKING:
    from playwright.async_api import Page

SNIFF_HINT = ("Loom's page may have changed: run `python -m webcli.core.capture loom` and fix "
              "webcli/sites/loom/endpoints.py.")
EXPIRED = "Loom's session is missing or expired."
GRAPHQL_JS = """
async ([url, headers, payload]) => {
  const res = await fetch(url, {method: 'POST', credentials: 'include', headers, body: JSON.stringify(payload)});
  let body = null;
  try { body = await res.json(); } catch (e) { body = null; }
  return {status: res.status, body};
}
"""


def blocked() -> str:
    lines = list(dict.fromkeys(guard.state.blocked))[:6]
    return f" The guard blocked: {'; '.join(lines)}." if lines else ""


async def open_app(page: "Page", url: str = APP_URL) -> None:
    """Load a Loom page. AuthRequired when Loom sends the profile to sign in."""
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    if parse.signed_out_url(page.url) or not parse.on_loom(page.url):
        raise AuthRequired(f"{EXPIRED} (Loom sent Chrome to {urlsplit(page.url).hostname}"
                           f"{urlsplit(page.url).path}.)")


async def graphql(page: "Page", name: str, variables: dict) -> dict:
    """One of the bundled queries, run in the page with its cookies. Errors never quote the answer."""
    doc = parse.query(name)
    payload = {"operationName": name, "query": doc["query"], "variables": variables}
    result = await page.evaluate(GRAPHQL_JS, [GRAPHQL_URL, GRAPHQL_HEADERS, payload])
    status, body = result.get("status"), result.get("body")
    if status in (401, 403):
        raise AuthRequired(f"{name}: HTTP {status}. {EXPIRED}")
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"{name} failed (HTTP {status}).{blocked()} {SNIFF_HINT}")
    if body.get("errors"):
        raise RuntimeError(f"{name} failed: Loom answered with errors; the session may be expired "
                           f"(a person runs `webcli identity login loom:default`). {SNIFF_HINT}")
    return body


async def status(*, headed: bool = False) -> dict:
    """{signedIn}: signed in when the library loads without a trip to Loom's sign-in. headed is accepted for the
    daemon contract; data sites always run headless."""
    async with browser.site_page(POLICY) as page:
        try:
            await open_app(page)
        except AuthRequired:
            return {"signedIn": False}
        return {"signedIn": True}


async def videos(limit: int | None = None, *, headed: bool = False) -> list[dict]:
    """Your library, newest first, following the cursor until `limit` videos or the end."""
    base = dict(parse.query("GetLoomsForLibrary").get("variables") or {})
    rows: list[dict] = []
    cursor = None
    async with browser.site_page(POLICY) as page:
        await open_app(page)
        while True:
            batch, cursor = parse.videos_page(await graphql(page, "GetLoomsForLibrary", {**base, "cursor": cursor}))
            rows += batch
            if (limit and len(rows) >= limit) or not cursor or not batch:
                return rows[:limit] if limit else rows


async def video(vid: str, *, headed: bool = False) -> dict:
    """One video's card. NotFound when Loom has none you may see."""
    variables = {**(parse.query("GetVideoCardDetails").get("variables") or {}), "videoId": vid}
    async with browser.site_page(POLICY) as page:
        await open_app(page)
        found = parse.video(await graphql(page, "GetVideoCardDetails", variables), vid)
    if found is None:
        raise NotFound(f"No video {vid}.", hint="See `webcli loom list --id-only`.")
    return found


async def transcript(vid: str, *, headed: bool = False) -> list[dict]:
    """The official transcript's phrases [{ts, text}], read from the file the share page fetches. NotFound when none
    comes within TRANSCRIPT_SECONDS (no transcript yet, or no such video)."""
    seen = asyncio.get_running_loop().create_future()
    reading: set[asyncio.Task] = set()

    async def read(response) -> None:
        try:
            data = await response.json()
        except Exception:
            return
        if not seen.done():
            seen.set_result(data)

    def on_response(response) -> None:
        if TRANSCRIPT_MARK in response.url and vid in response.url and not seen.done():
            task = asyncio.ensure_future(read(response))
            reading.add(task)
            task.add_done_callback(reading.discard)

    async with browser.site_page(POLICY) as page:
        page.on("response", on_response)
        await open_app(page, SHARE_URL.format(id=vid))
        try:
            data = await asyncio.wait_for(asyncio.shield(seen), TRANSCRIPT_SECONDS)
        except asyncio.TimeoutError:
            raise NotFound(f"No official transcript for {vid} within {TRANSCRIPT_SECONDS}s.{blocked()}",
                           hint="Check `webcli loom info ID`: Loom makes a transcript only for some videos.") from None
        finally:
            for task in list(reading):
                task.cancel()
    return parse.phrases(data)


async def login(timeout: int, on_state: Callable[[str], None] = lambda _state: None) -> dict:
    """A visible Chrome on Loom's sign-in page with no guard (sign-in may cross Google, Atlassian, or SSO); wait until
    a tab shows the library, a video, or settings. Needs a person; only `identity login` calls it."""
    on_state("signing in")
    async with browser.open_page(None, mode="visible") as page:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await asyncio.sleep(2)
            tabs = [p for p in page.context.pages if not p.is_closed()]
            if not tabs:
                raise AuthRequired("The Chrome window closed before sign-in finished.")
            if any(parse.signed_in_url(tab.url) for tab in tabs):
                return {"signedIn": True}
        raise AuthRequired(f"Timed out after {timeout}s waiting for Loom sign-in.")
