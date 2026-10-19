"""Browser-side work for webcli youtube: status and watch history from two sources (ytcli's). READ-ONLY.

Every youtubei call runs inside the youtube.com page (page.evaluate + fetch). The SAPISIDHASH header is computed
there from document.cookie, so cookie values never reach Python, stdout, or logs. My Activity is read off its own
page: the first page embedded in it, the batchexecute pages it loads while scrolled, and its rendered DOM.

google:default is signed in through gemini, so YouTube itself may not be yet: signed_in() then follows YouTube's own
passive "Sign in" link once (no page to fill in; Google's existing session decides) before calling it signed out.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from playwright.async_api import TimeoutError as PlaywrightTimeout

from webcli.core import browser
from webcli.core.errors import AuthRequired
from webcli.sites.youtube import myactivity
from webcli.sites.youtube.endpoints import (
    APP_HOST,
    APP_URL,
    MAX_SCROLLS,
    MYACTIVITY_HOST,
    MYACTIVITY_URL,
    SIGN_IN_URL,
)
from webcli.sites.youtube.guard import POLICY
from webcli.sites.youtube.parse import account_from_menu, channel_names, history_page, name_channels, take_newer

if TYPE_CHECKING:
    from playwright.async_api import Page

SIGNED_OUT = {"signedIn": False, "accountName": None, "email": None, "handle": None}
YTCFG_READY_JS = "() => !!(window.ytcfg && ytcfg.get && ytcfg.get('INNERTUBE_API_KEY') && ytcfg.get('INNERTUBE_CONTEXT'))"
LOGGED_IN_JS = "() => !!(window.ytcfg && ytcfg.get && ytcfg.get('LOGGED_IN'))"


class VerificationRequired(AuthRequired):
    """Signed in, but My Activity hides entries until Google's "verify it's you" check."""

    MESSAGE = ("My Activity needs Google's \"verify it's you\" check, which needs a person and a window webcli never "
               "opens. Use --source youtube (day precision), or turn the check off: My Activity > \"Manage My "
               "Activity verification\" > Off")

    def __init__(self, message: str = MESSAGE) -> None:
        super().__init__(message)


# Only ever called with "browse", "account/account_menu", or "account/accounts_list" (the guard allows no other).
YT_CALL_JS = """
async ([endpoint, extra]) => {
  const cfg = (k) => window.ytcfg.get(k);
  const cookie = (name) => {
    for (const part of document.cookie.split('; ')) {
      const i = part.indexOf('=');
      if (part.slice(0, i) === name) return part.slice(i + 1);
    }
    return null;
  };
  const sapisid = cookie('SAPISID') || cookie('__Secure-3PAPISID');
  if (!sapisid) return {status: 401, json: null};
  const origin = 'https://www.youtube.com';
  const ts = Math.floor(Date.now() / 1000);
  const digest = await crypto.subtle.digest('SHA-1', new TextEncoder().encode(`${ts} ${sapisid} ${origin}`));
  const hash = Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('');
  const context = cfg('INNERTUBE_CONTEXT');
  const headers = {
    'Content-Type': 'application/json',
    'Authorization': `SAPISIDHASH ${ts}_${hash}`,
    'X-Origin': origin,
    'X-Goog-AuthUser': String(cfg('SESSION_INDEX') ?? 0),
    'X-Youtube-Client-Name': String(cfg('INNERTUBE_CONTEXT_CLIENT_NAME') ?? 1),
    'X-Youtube-Client-Version': String(cfg('INNERTUBE_CLIENT_VERSION') || context.client.clientVersion),
  };
  // Brand-account channels: DELEGATED_SESSION_ID, or DATASYNC_ID "channel||user".
  let pageId = cfg('DELEGATED_SESSION_ID');
  if (!pageId) {
    const [channel, user] = String(cfg('DATASYNC_ID') || '').split('||');
    if (channel && user) pageId = channel;
  }
  if (pageId) headers['X-Goog-PageId'] = String(pageId);
  const visitor = cfg('VISITOR_DATA') || (context.client && context.client.visitorData);
  if (visitor) headers['X-Goog-Visitor-Id'] = visitor;
  const key = encodeURIComponent(cfg('INNERTUBE_API_KEY'));
  const res = await fetch(`/youtubei/v1/${endpoint}?key=${key}&prettyPrint=false`, {
    method: 'POST', credentials: 'include', headers, body: JSON.stringify({context, ...extra}),
  });
  let json = null;
  try { json = await res.json(); } catch (e) {}
  return {status: res.status, json};
}
"""


async def yt_call(page: "Page", endpoint: str, body: dict) -> dict:
    result = await page.evaluate(YT_CALL_JS, [endpoint, body])
    status, data = result["status"], result["json"]
    if status in (401, 403):
        raise AuthRequired("YouTube rejected the signed-in request.")
    if status != 200 or not isinstance(data, dict):
        error = data.get("error") if isinstance(data, dict) else None
        detail = f" ({error.get('message')})" if isinstance(error, dict) and error.get("message") else ""
        raise RuntimeError(f"youtubei/v1/{endpoint} failed: HTTP {status}{detail}")
    return data


async def _ytcfg(page: "Page") -> bool:
    """Wait for youtube.com's ytcfg; its LOGGED_IN."""
    try:
        await page.wait_for_function(YTCFG_READY_JS, timeout=30_000)
    except PlaywrightTimeout:
        raise RuntimeError(f"youtube.com did not finish loading (stopped at {urlsplit(page.url).hostname}).") from None
    return bool(await page.evaluate(LOGGED_IN_JS))


async def signed_in(page: "Page") -> bool:
    """Load youtube.com signed in if the profile can be: as it is, else once through YouTube's passive sign-in."""
    await page.goto(APP_URL, wait_until="domcontentloaded", timeout=60_000)
    if await _ytcfg(page):
        return True
    await page.goto(SIGN_IN_URL, wait_until="domcontentloaded", timeout=60_000)
    return urlsplit(page.url).hostname == APP_HOST and await _ytcfg(page)


async def _account(page: "Page") -> dict:
    info = {"signedIn": True, **account_from_menu(await yt_call(page, "account/account_menu", {}))}
    if not info["email"]:
        try:  # account_menu usually omits the email; the account-switcher list has it
            info["email"] = account_from_menu(await yt_call(page, "account/accounts_list", {}))["email"]
        except Exception:
            pass  # email is optional; leave it null
    return info


async def status(*, headed: bool = False) -> dict:
    """{signedIn, accountName, email, handle}. headed is accepted for the daemon contract; data sites always run
    headless."""
    async with browser.site_page(POLICY) as page:
        if not await signed_in(page):
            return dict(SIGNED_OUT)
        return await _account(page)


async def youtube_history(page: "Page", *, limit: int | None, cutoff: datetime | None,
                          today: date | None = None) -> list[dict]:
    """FEhistory via youtubei browse + continuations. Day precision. limit=None: no row cap."""
    if not await signed_in(page):
        raise AuthRequired("Not signed in to YouTube.")
    today = today or datetime.now().astimezone().date()
    rows: list[dict] = []
    names: dict = {}
    day = None
    previous: set[tuple] = set()
    seen: set[str] = set()
    body: dict = {"browseId": "FEhistory"}
    while True:
        resp = await yt_call(page, "browse", body)
        page_rows, token, day = history_page(resp, today=today, day=day)
        names.update(channel_names(resp))
        # A continuation can repeat the previous page's last rows (same video, same day).
        fresh = [r for r in page_rows if (r["videoId"], r["watchedAt"]) not in previous]
        previous = {(r["videoId"], r["watchedAt"]) for r in page_rows}
        kept, reached = take_newer(fresh, cutoff)
        rows += kept
        if reached or (limit is not None and len(rows) >= limit) or not token or token in seen:
            return name_channels(rows[:limit], names)
        seen.add(token)
        body = {"continuation": token}


async def myactivity_history(page: "Page", *, limit: int | None, cutoff: datetime | None,
                             notes: list[str]) -> list[dict]:
    """Best-effort exact timestamps from My Activity: the page's own responses, else its rendered DOM.

    limit=None: no row cap; scrolls until the cutoff, the end of the list, or MAX_SCROLLS. Raises
    VerificationRequired when the "verify it's you" gate hides every entry.
    """
    bodies: list[str] = []

    async def on_response(response) -> None:
        parts = urlsplit(response.url)
        if parts.hostname == MYACTIVITY_HOST and parts.path.endswith("/batchexecute"):
            try:
                bodies.append(await response.text())
            except Exception:
                pass

    page.on("response", on_response)
    try:
        await page.goto(MYACTIVITY_URL, wait_until="domcontentloaded", timeout=60_000)
        if urlsplit(page.url).hostname != MYACTIVITY_HOST:
            raise AuthRequired("Not signed in to My Activity.")
        await page.wait_for_timeout(2000)
        html = await page.content()
        today = datetime.now().astimezone().date()
        rows: list[dict] = []
        stale = 0
        for _ in range(MAX_SCROLLS):
            # ponytail: re-parses every body and the whole DOM each scroll (quadratic); parse
            # incrementally if --limit or the --since window gets huge.
            # The page data has exact times, but if it misses entries the rendered DOM shows, use the DOM.
            found = max(myactivity.parse_bodies([html, *bodies]), myactivity.parse_dom(await page.content(), today),
                        key=len)
            if not found and myactivity.verification_required(await page.inner_text("body")):
                raise VerificationRequired()
            kept, reached = take_newer(found, cutoff)
            if reached or (limit is not None and len(kept) >= limit):
                return kept[:limit]
            stale = stale + 1 if len(kept) == len(rows) else 0
            rows = kept
            if stale >= 3:
                break
            await page.mouse.wheel(0, 5000)
            await page.wait_for_timeout(1500)
        if rows and myactivity.verification_required(await page.inner_text("body")):
            notes.append("My Activity shows only part of your history until Google's \"verify it's you\" check "
                         "passes (a person's job).")
        return rows[:limit]
    finally:
        page.remove_listener("response", on_response)


async def history(source: str = "auto", limit: int | None = None, since: str | None = None, *,
                  headed: bool = False) -> dict:
    """{rows, notes}: watches newest first, one row per watch. source "auto" tries My Activity (exact times), then
    falls back to YouTube history (day precision); notes says why it fell back. since: an ISO 8601 time with an
    offset; older watches are left out."""
    cutoff = datetime.fromisoformat(since) if since else None
    notes: list[str] = []
    async with browser.site_page(POLICY) as page:
        if source == "youtube":
            return {"rows": await youtube_history(page, limit=limit, cutoff=cutoff), "notes": notes}
        if source == "myactivity":
            return {"rows": await myactivity_history(page, limit=limit, cutoff=cutoff, notes=notes), "notes": notes}
        try:
            rows = await myactivity_history(page, limit=limit, cutoff=cutoff, notes=notes)
            reason = "no watch entries could be parsed"
        except VerificationRequired as exc:
            notes.append(f"{exc}. Using YouTube history (day precision).")
            return {"rows": await youtube_history(page, limit=limit, cutoff=cutoff), "notes": notes}
        except Exception as exc:  # best-effort source: any failure falls back
            rows, reason = [], (str(exc) or type(exc).__name__).splitlines()[0]
        if rows:
            return {"rows": rows, "notes": notes}
        notes.append(f"My Activity unavailable ({reason}); using YouTube history (day precision).")
        return {"rows": await youtube_history(page, limit=limit, cutoff=cutoff), "notes": notes}
