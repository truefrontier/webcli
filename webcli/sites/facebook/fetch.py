"""Browser-side work for webcli facebook (fbc's fetch.py on the shared core): status, the Friends tab, and About
sections. READ-ONLY.

Everything is a full page load (GET) under the guard, then the rendered [role="main"] HTML is handed to parse.py.
Nothing here clicks, types, or submits: it navigates, waits, reads, and scrolls the window. The only GraphQL the
guard lets through is the Friends-tab pagination query, during `sync` (endpoints.SYNC_QUERIES). There is no login:
meta:default signs in through muse (`webcli identity login meta:default`).

People cross the daemon as {key, username, numericId} dicts.
"""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from webcli.core import browser, guard
from webcli.core.errors import AuthRequired, NotFound
from webcli.sites.facebook import parse
from webcli.sites.facebook.endpoints import (
    ABOUT_QUERIES,
    ABOUT_SECTIONS,
    AUTH_PATHS,
    FRIENDS_URL,
    MAX_SCROLLS,
    ME_URL,
    PAGE_PAUSE,
    SCROLL_PAUSE,
    SELECTORS,
    STALL_SCROLLS,
    SYNC_QUERIES,
)
from webcli.sites.facebook.guard import POLICY, read_policy

if TYPE_CHECKING:
    from playwright.async_api import Page

APP_URL = ME_URL  # what `python -m webcli.core.capture facebook` loads
SELECTOR_HINT = ("Facebook's page may have changed: run `python -m webcli.core.capture facebook` and fix "
                 "webcli/sites/facebook/endpoints.py or parse.py.")
SETTLE_POLL = 0.5
SETTLE_STABLE = 3  # main's text length must hold for this many polls
SETTLE_MAX = 15.0

# All of these run inside the page and only read or scroll: no clicks, no typing, no form submits.
MAIN_JS = "(sel) => { const el = document.querySelector(sel); return el ? el.outerHTML : null; }"
MAIN_TEXT_LEN_JS = "(sel) => { const el = document.querySelector(sel); return el ? el.innerText.length : -1; }"
SIGNED_OUT_JS = "(sel) => !!document.querySelector(sel)"
SCROLL_JS = """
(sel) => {
  const main = document.querySelector(sel) || document.body;
  const links = main.querySelectorAll('a[href]');
  if (links.length) links[links.length - 1].scrollIntoView({block: 'end'});
  const root = document.scrollingElement || document.documentElement;
  window.scrollTo(0, root.scrollHeight);
  return root.scrollHeight;
}
"""


async def pause(span: tuple[float, float]) -> None:
    """Polite pacing: a random wait in [lo, hi] seconds."""
    await asyncio.sleep(random.uniform(*span))


def signed_out() -> dict:
    return {"signedIn": False, "name": None, "key": None, "profileUrl": None}


def blocked_summary(limit: int = 30) -> list[str]:
    """Distinct requests the guard aborted this command, most frequent first (already redacted)."""
    counts: dict[str, int] = {}
    for line in guard.state.blocked:
        counts[line] = counts.get(line, 0) + 1
    return [f"{line} x{n}" if n > 1 else line for line, n in sorted(counts.items(), key=lambda kv: -kv[1])][:limit]


def person(data: dict) -> parse.Person:
    return parse.Person(data["key"], data.get("username"), data.get("numericId"))


def person_dict(found: parse.Person) -> dict:
    return {"key": found.key, "username": found.username, "numericId": found.numeric_id}


def _auth_path(url: str) -> bool:
    parts = urlsplit(url)
    return (parts.hostname or "").endswith("facebook.com") and any(
        parts.path == p or parts.path.startswith(p.rstrip("/") + "/") for p in AUTH_PATHS)


async def _check_signed_in(page: "Page") -> None:
    if _auth_path(page.url):
        raise AuthRequired(f"Facebook sent Chrome to {urlsplit(page.url).path}; the session is missing, expired, or "
                           "held at a checkpoint.")
    if await page.evaluate(SIGNED_OUT_JS, SELECTORS["login_form"]):
        raise AuthRequired("Facebook shows a login form; the session is missing or expired.")


async def open_fb(page: "Page", url: str) -> None:
    """One full page load. AuthRequired when Facebook wants a sign-in."""
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    host = urlsplit(page.url).hostname or ""
    if not host.endswith("facebook.com"):
        raise RuntimeError(f"Facebook redirected to {host}. {SELECTOR_HINT}")
    await _check_signed_in(page)


async def main_html(page: "Page", *, settle: bool = True) -> str | None:
    """The [role=main] outerHTML once its text stops growing (Facebook renders after load)."""
    sel = SELECTORS["main"]
    try:
        await page.wait_for_selector(sel, state="attached", timeout=15_000)
    except Exception:
        return None
    if settle:
        last, stable, waited = -2, 0, 0.0
        while stable < SETTLE_STABLE and waited < SETTLE_MAX:
            await asyncio.sleep(SETTLE_POLL)
            waited += SETTLE_POLL
            size = await page.evaluate(MAIN_TEXT_LEN_JS, sel)
            stable = stable + 1 if size == last and size > 0 else 0
            last = size
    return await page.evaluate(MAIN_JS, sel)


async def own_profile(page: "Page") -> parse.Person | None:
    """Who /me resolved to. The redirect can happen client-side, so wait up to 10s for it."""
    for _ in range(20):
        if found := parse.person_from_url(page.url):
            return found
        await asyncio.sleep(0.5)
    return None


async def session(page: "Page") -> dict:
    """{signedIn, name, key, profileUrl} from facebook.com/me, which redirects to your own profile."""
    try:
        await open_fb(page, ME_URL)
    except AuthRequired:
        return signed_out()
    me = await own_profile(page)
    if me is None:
        return signed_out()
    html = await main_html(page) or ""
    return {"signedIn": True, "name": parse.profile_name(html, await page.title()), "key": me.key,
            "profileUrl": me.url}


async def status(*, headed: bool = False) -> dict:
    """headed is accepted for the daemon contract; data sites always run headless."""
    async with browser.site_page(POLICY) as page:
        return await session(page)


async def sync(limit: int | None = None, *, headed: bool = False) -> dict:
    """Your Friends tab, scrolled until `limit` friends or the list stops growing.

    Parses after every scroll and merges by profile key, so friends Facebook drops from the DOM as the list grows are
    kept. {friends, scrolls, complete, blocked}. complete is False when the limit or MAX_SCROLLS stopped it, or when
    the guard blocked a pagination query (the list may only look finished).
    """
    async with browser.site_page(read_policy(SYNC_QUERIES)) as page:
        await open_fb(page, FRIENDS_URL)
        if await own_profile(page) is None:  # without the owner's id, the owner would be listed as a friend
            raise RuntimeError(f"/me/friends didn't resolve to your profile (stayed at {urlsplit(page.url).path}). "
                               f"{SELECTOR_HINT}")
        rows: dict[str, dict] = {}
        stall = scrolls = 0
        complete = False
        html = await main_html(page)
        while True:
            fresh = 0
            for row in parse.friends(html or "", page.url):
                if row["key"] not in rows:
                    fresh += 1
                    rows[row["key"]] = row
                elif row["subtitle"] and not rows[row["key"]]["subtitle"]:
                    rows[row["key"]]["subtitle"] = row["subtitle"]
            if limit and len(rows) >= limit:
                break
            stall = 0 if fresh else stall + 1
            if stall >= STALL_SCROLLS:
                # Stopped growing. That's the end of the list only if no scroll query was blocked.
                complete = not any("PaginationQuery" in line for line in guard.state.blocked)
                break
            if scrolls >= MAX_SCROLLS:
                break
            await page.evaluate(SCROLL_JS, SELECTORS["main"])
            scrolls += 1
            await pause(SCROLL_PAUSE)
            html = await main_html(page, settle=False)
        friends = list(rows.values())
        return {"friends": friends[:limit] if limit else friends, "scrolls": scrolls, "complete": complete,
                "blocked": blocked_summary()}


async def _about(page: "Page", who: parse.Person) -> dict:
    sections: dict[str, str | None] = {}
    final_url, title = None, None
    for n, section in enumerate(ABOUT_SECTIONS):
        if n:
            await pause(PAGE_PAUSE)
        await open_fb(page, parse.about_url(who, section))
        html = await main_html(page)
        if html and parse.unavailable(html):
            raise NotFound(f"Profile {who.key} isn't available to you (deleted, private, or blocked).",
                           hint="Check the id with `webcli facebook search --name ...`.")
        sections[section] = html
        if final_url is None and parse.person_from_url(page.url):
            final_url, title = page.url, await page.title()
    if not any(sections.values()):
        raise RuntimeError(f"No About content loaded for {who.key}. {SELECTOR_HINT}")
    record = parse.about(sections, final_url or who.url, title)
    seen = parse.person_from_url(final_url or "") or who
    username = seen.username or who.username
    numeric = seen.numeric_id or who.numeric_id
    full = parse.Person(username or numeric, username, numeric)
    return {**record, "key": full.key, "username": username, "numericId": numeric, "profileUrl": full.url,
            "blocked": blocked_summary()}


async def about(who: dict, *, headed: bool = False) -> dict:
    """One profile's About tab (work, education, places, links), read as one full page load per section."""
    async with browser.site_page(read_policy(ABOUT_QUERIES)) as page:
        return await _about(page, person(who))


async def about_many(people: list[dict], *, headed: bool = False) -> list[dict]:
    """About records for several people in one page, with a pause between people: [{person, record}] or
    [{person, error}] per person, in order. A missing or unreadable profile is an error entry and the rest go on;
    signed out mid-run (AuthRequired) stops everything."""
    out = []
    async with browser.site_page(read_policy(ABOUT_QUERIES)) as page:
        for n, who in enumerate(people):
            if n:
                await pause(PAGE_PAUSE)
            try:
                out.append({"person": who, "record": await _about(page, person(who))})
            except AuthRequired:
                raise  # signed out mid-run: stop, don't keep loading pages (it subclasses RuntimeError)
            except (NotFound, RuntimeError) as exc:
                out.append({"person": who, "error": str(exc)})
    return out
