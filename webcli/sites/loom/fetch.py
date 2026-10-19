"""Browser-side work for webcli loom (loom-cli's on the shared core): login, status, the library, one video, and its
transcript. READ-ONLY.

Signed in means Loom's API answers the library query as signed in (library()): status, login, and `list` all use it.
A cookie can't show it: a profile can hold connect.sid and still be sent to /login.

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
    if _unauthenticated(body.get("errors")):
        raise AuthRequired(f"{name}: Loom answered UNAUTHENTICATED. {EXPIRED}")
    if body.get("errors"):
        raise RuntimeError(f"{name} failed: Loom answered with errors; the session may be expired "
                           f"(a person runs `webcli identity login loom:default`). {SNIFF_HINT}")
    return body


def _unauthenticated(errors) -> bool:
    """GraphQL's (Apollo's) error code for "no signed-in user". GUESS for Loom: if it uses another, the answer stays an
    exit 5 with the sign-in hint. FORBIDDEN is not here: a signed-in account can lack access to one video."""
    return isinstance(errors, list) and any(isinstance(e, dict) and isinstance(e.get("extensions"), dict)
                                            and e["extensions"].get("code") == "UNAUTHENTICATED" for e in errors)


async def library(page: "Page") -> None:
    """AuthRequired unless Loom's API answers the query `list` runs (GetLoomsForLibrary) with a library; an empty one
    counts."""
    body = await graphql(page, "GetLoomsForLibrary", dict(parse.query("GetLoomsForLibrary").get("variables") or {}))
    data = body.get("data")
    if not (isinstance(data, dict) and isinstance(data.get("getLooms"), dict)):
        raise AuthRequired(f"GetLoomsForLibrary answered without a library. {EXPIRED}")


async def status(*, headed: bool = False) -> dict:
    """{signedIn}, plus {reason} when signed out: signed in when the library loads without a trip to Loom's sign-in
    and Loom's API answers the library query, as `list` needs. headed is accepted for the daemon contract; data sites
    always run headless."""
    async with browser.site_page(POLICY) as page:
        try:
            await open_app(page)
            await library(page)
        except AuthRequired as exc:
            return {"signedIn": False, "reason": str(exc)}
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


async def _library_tab(page: "Page") -> bool:
    """True once a tab shows the library or settings and Loom's API answers the library query there."""
    tabs = [p for p in page.context.pages if not p.is_closed()]
    if not tabs:
        raise AuthRequired("The Chrome window closed before sign-in finished.")
    for tab in tabs:  # sign-in may finish in another tab
        if not parse.signed_in_url(tab.url):
            continue
        try:
            await library(tab)
        except Exception:  # Loom hasn't finished signing in, or the tab is mid-navigation
            continue
        return True
    return False


async def login(timeout: int, on_state: Callable[[str], None] = lambda _state: None) -> dict:
    """A visible Chrome on Loom's sign-in page with no guard (sign-in may cross Google, Atlassian, or SSO). Done only
    once a tab shows the library AND Loom's API answers the library query as signed in: with Google through
    Atlassian, a tab can reach /looms before Loom has finished signing in, and closing Chrome then can cut it short.
    Then, with the window closed, a fresh headless Chrome on the same profile (no daemon runs during sign-in) must be
    signed in too, as every command will be. Needs a person; only `identity login` calls it."""
    on_state("signing in")
    async with browser.open_page(None, mode="visible") as page:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not await _library_tab(page):
            if loop.time() >= deadline:
                raise AuthRequired(f"Timed out after {timeout}s waiting for Loom sign-in.")
            await asyncio.sleep(2)
    info = await status()
    if not info["signedIn"]:
        raise AuthRequired("Loom's library loaded in the sign-in window, but a fresh hidden Chrome on the same profile "
                           f"is signed out: {info['reason']}")
    return info
