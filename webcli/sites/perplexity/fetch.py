"""Browser-side work for webcli perplexity: Discover's feed, signed in first, else public.

The identity is perplexity:default (a person signs it in: `webcli perplexity login`). Every read tries it signed in
first; when the profile isn't signed in (never was, or the session expired) the same reads answer as the public,
signed-out Discover feed, and every row says auth "public" instead of "signed_in". The command's hidden Chrome opens
www.perplexity.ai/discover as an origin, with every script aborted by the guard, and reads the feed with fetch()
from that page (GET, same origin, the profile's cookies). Nothing is ever sent to Perplexity but those GETs: no
query, no like, follow, or bookmark.

A challenge or block (Cloudflare's "Just a moment", HTTP 403/429/503, an HTML answer where JSON belongs) stops the
command with exit 5. webcli never works around one.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from webcli.core import browser
from webcli.core.errors import AuthRequired, NotFound, UsageError
from webcli.sites.google import blocked
from webcli.sites.perplexity.endpoints import (
    API_VERSION,
    DISCOVER_URL,
    LOGIN_URL,
    FEED_PATH,
    MAX_LIMIT,
    PAGE_PAUSE_MS,
    PAGE_SIZE,
    SESSION_PATH,
    TOPICS,
    TOPICS_PATH,
)
from webcli.sites.perplexity.guard import POLICY
from webcli.sites.perplexity.parse import feed_rows, topic_id, topic_slug

if TYPE_CHECKING:
    from playwright.async_api import Page

CHALLENGE_MARKERS = ("just a moment", "verify you are human", "attention required", "access denied")

# GET only, same origin, no redirect followed: {status, json (null when the answer isn't JSON), type}.
GET_JSON_JS = """
async (path) => {
  const res = await fetch(path, {method: 'GET', credentials: 'include', redirect: 'error',
                                 headers: {'Accept': 'application/json'}});
  const type = res.headers.get('content-type') || '';
  let json = null;
  if (type.includes('json')) { try { json = await res.json(); } catch (e) {} }
  return {status: res.status, json, type};
}
"""
TITLE_JS = "() => document.title || ''"


class Blocked(RuntimeError):
    """Perplexity challenged or refused the read. Exit 5: webcli stops instead of working around it."""


def _q(path: str, **params) -> str:
    from urllib.parse import urlencode

    return f"{path}?{urlencode({**params, 'version': API_VERSION, 'source': 'default'})}"


async def _open(page: "Page") -> None:
    response = await page.goto(DISCOVER_URL, wait_until="domcontentloaded", timeout=60_000)
    status = response.status if response is not None else None
    title = (await page.evaluate(TITLE_JS)).lower()
    if status in (403, 429, 503) or any(marker in title for marker in CHALLENGE_MARKERS):
        raise Blocked(f"Perplexity answered /discover with a challenge or block (HTTP {status}). webcli stops here "
                      "and does not work around it; try again later.")
    if status is None or status >= 400:
        raise RuntimeError(f"www.perplexity.ai/discover answered HTTP {status}.{blocked()}")


async def _get(page: "Page", path: str) -> dict:
    result = await page.evaluate(GET_JSON_JS, path)
    status, data = result["status"], result["json"]
    name = path.split("?", 1)[0]
    if status in (403, 429, 503) or (status == 200 and data is None):
        raise Blocked(f"Perplexity answered {name} with HTTP {status} ({result['type'] or 'no content type'}), a "
                      "challenge or block, not the feed. webcli stops here and does not work around it.")
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError(f"Perplexity {name} failed: HTTP {status}.{blocked()}")
    return data


def _user(session: dict) -> dict | None:
    user = session.get("user") if isinstance(session, dict) else None
    return user if isinstance(user, dict) and user else None


async def _auth(page: "Page") -> tuple[str, dict | None]:
    """("signed_in", user) or ("public", None), from the session the profile's cookies carry."""
    user = _user(await _get(page, _q(SESSION_PATH)))
    return ("signed_in", user) if user else ("public", None)


async def status(*, headed: bool = False) -> dict:
    """{signedIn, auth, name, email, discover, topics}. auth "public": Discover still answers, signed out. headed:
    the daemon contract."""
    async with browser.site_page(POLICY) as page:
        await _open(page)
        auth, user = await _auth(page)
        topics = await _get(page, _q(TOPICS_PATH))
        slugs = [t.get("slug") for t in topics.get("all_topics") or [] if isinstance(t, dict) and t.get("slug")]
        return {"signedIn": auth == "signed_in", "auth": auth, "name": (user or {}).get("name"),
                "email": (user or {}).get("email"), "discover": True, "topics": ["top", *slugs]}


async def login(timeout: int, on_state=lambda _state: None) -> dict:
    """A visible Chrome on Perplexity's sign-in with no guard (sign-in may cross Google, Apple, or an email link).
    Done once the page's own session shows a user; then a fresh hidden Chrome on the same profile must be signed in
    too, as every command will be. Needs a person; only `webcli perplexity login` / `identity login` call it."""
    on_state("signing in")
    async with browser.open_page(None, mode="visible") as page:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                result = await page.evaluate(GET_JSON_JS, _q(SESSION_PATH))
            except Exception:  # navigating mid sign-in
                result = {}
            if _user((result or {}).get("json") or {}):
                break
            if loop.time() >= deadline:
                raise AuthRequired(f"Timed out after {timeout}s waiting for Perplexity sign-in.")
            await asyncio.sleep(2)
    info = await status()
    if not info["signedIn"]:
        raise AuthRequired("Perplexity signed in in the window, but a fresh hidden Chrome on the same profile is "
                           "signed out.")
    return info


async def discover(topic: str = "top", limit: int = 30, *, headed: bool = False) -> list[dict]:
    """Up to `limit` Discover cards for `topic`, as Perplexity orders them, each with auth "signed_in" or "public".
    Exit 3 for an unknown topic slug, 5 for a challenge."""
    if topic not in TOPICS:
        raise UsageError(f"--topic must be one of {', '.join(TOPICS)}.")
    limit = max(1, min(int(limit), MAX_LIMIT))
    slug = topic_slug(topic)
    async with browser.site_page(POLICY) as page:
        await _open(page)
        auth, _user_info = await _auth(page)
        tid = "top"
        if slug != "top":
            tid = topic_id(await _get(page, _q(TOPICS_PATH)), slug)
            if tid is None:
                raise NotFound(f"Perplexity Discover lists no {slug!r} topic now.")
        rows: list[dict] = []
        seen: set[str] = set()
        offset = 0
        while len(rows) < limit:
            if offset:
                await asyncio.sleep(PAGE_PAUSE_MS / 1000)
            feed = await _get(page, _q(FEED_PATH, limit=PAGE_SIZE, offset=offset, topic=tid))
            items = feed.get("items") or []
            fresh = [{**r, "auth": auth} for r in feed_rows(feed, slug) if r["id"] not in seen]
            seen.update(r["id"] for r in fresh)
            rows += fresh
            offset += len(items)
            if not items or not fresh or not feed.get("next_token"):
                break
        return rows[:limit]
