"""Browser-side work for webcli gnews: Google News "For you" (and its sections), read off the rendered page.

Runs as google:<account> (gemini's login signs it in; webcli never signs in), signed in first. When the profile isn't
signed in (missing or expired), For you falls back to the public Google News home (and a section to its public
page), and every row says auth "public" instead of "signed_in". The guard passes GETs only, so the page
can render what the server sent but no RPC ever leaves it: no follow, hide, "fewer like this", or save. A challenge
or block (Google's "unusual traffic" page, HTTP 403/429) stops the command with exit 5; webcli never works around it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from webcli.core import browser
from webcli.core.errors import AuthRequired, NotFound, UsageError
from webcli.sites.gnews.endpoints import APP_HOST, FORYOU_URL, MAX_LIMIT, PUBLIC_URL, RENDER_WAIT_MS, SECTIONS
from webcli.sites.gnews.guard import POLICY
from webcli.sites.gnews.parse import rows, section_href
from webcli.sites.google import EMAIL_JS, SIGNED_OUT_HOSTS, blocked

if TYPE_CHECKING:
    from playwright.async_api import Page

# Each story's title link (a ./read/ link with text), and the text lines, time, and image of its card: the nearest
# ancestor holding a <time>.
STORIES_JS = """() => {
  const out = [];
  for (const a of document.querySelectorAll('a[href^="./read/"], a[href^="./articles/"]')) {
    const title = (a.textContent || '').trim();
    if (!title) continue;
    let box = a;
    for (let i = 0; i < 8 && box.parentElement; i++) { box = box.parentElement; if (box.querySelector('time')) break; }
    const t = box.querySelector('time');
    const img = box.querySelector('img[src]');
    out.push({href: a.getAttribute('href'), title, lines: (box.innerText || '').split('\\n').slice(0, 8),
              time: t ? t.getAttribute('datetime') : null, img: img ? img.getAttribute('src') : null});
  }
  return out;
}"""
NAV_JS = """() => [...document.querySelectorAll('a[href^="./topics/"]')]
  .map(a => [(a.textContent || '').trim(), a.getAttribute('href')])"""
SIGNED_OUT_JS = "() => !!document.querySelector('a[href*=\"ServiceLogin\"], a[href*=\"accounts.google.com/signin\"]')"
CHALLENGE_JS = "() => /unusual traffic|not a robot|captcha/i.test(document.body ? document.body.innerText.slice(0, 2000) : '')"


class Blocked(RuntimeError):
    """Google challenged or refused the page. Exit 5: webcli stops instead of working around it."""


async def _open(page: "Page", url: str) -> None:
    response = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    status = response.status if response is not None else None
    landed = urlsplit(page.url).hostname or ""
    if status in (403, 429) or "/sorry/" in page.url or await page.evaluate(CHALLENGE_JS):
        raise Blocked(f"Google News answered with a challenge or block (HTTP {status}). webcli stops here and does "
                      "not work around it; try again later.")
    if landed in SIGNED_OUT_HOSTS:
        raise AuthRequired(f"news.google.com sent Chrome to {landed}: the Google session is missing or expired.")
    if landed != APP_HOST or status is None or status >= 400:
        raise RuntimeError(f"news.google.com answered HTTP {status} at {landed}.{blocked()}")


async def _signed_out(page: "Page") -> bool:
    return bool(await page.evaluate(SIGNED_OUT_JS))


async def status(*, headed: bool = False) -> dict:
    """{signedIn, email}. headed: the daemon contract (always headless)."""
    async with browser.site_page(POLICY) as page:
        try:
            await _open(page, FORYOU_URL)
        except AuthRequired:
            return {"signedIn": False, "email": None}
        if await _signed_out(page):
            return {"signedIn": False, "email": None}
        try:
            email = await page.evaluate(EMAIL_JS)
        except Exception:
            email = None
        return {"signedIn": True, "email": email}


async def foryou(section: str = "foryou", limit: int = 30, *, headed: bool = False) -> list[dict]:
    """Up to `limit` stories from For you (or one of its sections), in page order, each with auth "signed_in", or
    "public" when the profile is signed out (For you is then the public home)."""
    if section not in SECTIONS:
        raise UsageError(f"--section must be one of {', '.join(SECTIONS)}.")
    limit = max(1, min(int(limit), MAX_LIMIT))
    async with browser.site_page(POLICY) as page:
        auth = "signed_in"
        try:
            await _open(page, FORYOU_URL)
            if await _signed_out(page):
                auth = "public"
        except AuthRequired:
            auth = "public"
        if auth == "public":
            await _open(page, PUBLIC_URL)
        label = SECTIONS[section]
        if label:
            url = section_href(await page.evaluate(NAV_JS), label)
            if not url:
                raise NotFound(f"Google News shows no {label!r} section link.")
            await _open(page, url)
        await page.wait_for_timeout(RENDER_WAIT_MS)
        return [{**r, "auth": auth} for r in rows(await page.evaluate(STORIES_JS), section, limit)]
