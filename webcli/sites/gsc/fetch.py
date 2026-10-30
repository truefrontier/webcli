"""Search Console in the google:default profile: load https://search.google.com/search-console once, then make
its own batchexecute RPCs from inside the page (the `at` token never leaves the page; Python sees response
bodies only). One exception: `request_indexing` drives Search Console's own URL Inspection page, because REQUEST
INDEXING carries an attestation the page's own JS mints for the click (no RPC answer contains it), so webcli never
builds or sees that request; the guard lets exactly one through.

A challenge or block (HTTP 403 / 429, /sorry/, "unusual traffic") stops with exit 5: webcli does not work around
it. A signed-out profile is exit 4: a person runs `webcli identity login google:default`.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

from webcli.core import browser
from webcli.core.errors import AuthRequired, NotFound, RateLimited, UsageError
from webcli.sites import google
from webcli.sites.google import EMAIL_JS, SIGNED_OUT_HOSTS
from webcli.sites.gsc import guard, parse
from webcli.sites.gsc.endpoints import APP_HOST, APP_URL, BATCH_PATH, DEFAULT_EXAMPLES, DIMENSIONS, \
    INDEX_FILTERS, INSPECT_MODE_INDEX, INSPECT_TIMEOUT_S, MAX_EXAMPLES, MAX_LIMIT, NOT_INDEXED, POLL_SECONDS, \
    REQUEST_INDEXING_TIMEOUT_S, RPC, SOURCE_PATH, VIEW_INDEX_REASONS, VIEW_INDEX_SERIES, VIEW_REASON_URLS, WRITE_RPC

if TYPE_CHECKING:
    from playwright.async_api import Page

SNIFF_HINT = ("Search Console's API may have changed; compare with a capture (`python -m webcli.core.capture gsc`) "
              "and fix webcli/sites/gsc/endpoints.py or parse.py.")
PROPERTIES_HINT = "run: webcli gsc properties"
CHALLENGE_JS = ("() => /unusual traffic|not a robot|captcha/i.test(document.body ? "
                "document.body.innerText.slice(0, 2000) : '')")
HAS_TOKEN_JS = "() => !!(window.WIZ_global_data && window.WIZ_global_data.SNlM0e)"
TEXT_JS = "() => document.body ? document.body.innerText : ''"
RPC_TIMEOUT_MS = 60_000

# One RPC, in the page. Returns {status, body}; status 0: no token (signed out) or a redirect (never followed).
_CALL = """
async ({path, freq, rpcid, sourcePath, timeoutMs}) => {
  const wiz = window.WIZ_global_data || {};
  if (!wiz.SNlM0e) return {status: 0, body: ''};
  const params = new URLSearchParams({rpcids: rpcid, 'source-path': sourcePath,
    hl: document.documentElement.lang || 'en', _reqid: String(100000 + Math.floor(Math.random() * 900000)), rt: 'c'});
  if (wiz.cfb2h) params.set('bl', wiz.cfb2h);
  if (wiz.FdrFJe) params.set('f.sid', wiz.FdrFJe);
  const body = new URLSearchParams({'f.req': freq, at: wiz.SNlM0e});
  try {
    const res = await fetch(path + '?' + params, {method: 'POST', credentials: 'include', redirect: 'manual',
      signal: AbortSignal.timeout(timeoutMs),
      headers: {'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8', 'X-Same-Domain': '1'}, body});
    return {status: res.type === 'opaqueredirect' ? 0 : res.status, body: await res.text()};
  } catch (e) {
    return {status: -1, body: String(e)};
  }
}
"""

# What Search Console's REQUEST INDEXING dialog says when it's done.
RI_DONE = re.compile(r"indexing requested", re.I)
RI_QUOTA = re.compile(r"quota exceeded", re.I)
RI_FAILED = re.compile(r"problem submitting your indexing request|something went wrong", re.I)
RI_CANT = re.compile(r"url is not available to google|can'?t be indexed|indexing not allowed", re.I)


class Blocked(RuntimeError):
    """Google challenged or refused the page. Exit 5: webcli stops instead of working around it."""


async def open_app(page: "Page", prop: str | None = None) -> None:
    """Load Search Console (on `prop` when given). AuthRequired when signed out; Blocked on a challenge."""
    url = APP_URL + (f"?resource_id={quote(prop, safe='')}" if prop else "")
    response = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    status = response.status if response is not None else None
    parts = urlsplit(page.url)
    landed = parts.hostname or ""
    if status in (403, 429) or "/sorry/" in page.url or await page.evaluate(CHALLENGE_JS):
        raise Blocked(f"Google answered Search Console with a challenge or block (HTTP {status}). webcli stops here "
                      "and does not work around it; try again later.")
    if landed in SIGNED_OUT_HOSTS or parts.path.startswith("/search-console/about"):
        raise AuthRequired(f"Search Console sent Chrome to {landed}{parts.path}: the Google session is missing or "
                           "expired.")
    if landed != APP_HOST or status is None or status >= 400:
        raise RuntimeError(f"Search Console answered HTTP {status} at {landed}.{google.blocked()}")
    try:
        await page.wait_for_function(HAS_TOKEN_JS, timeout=20_000)
    except Exception:
        raise AuthRequired("Search Console's page has no session token: the Google session is missing or "
                           "expired.") from None


async def rpc(page: "Page", rpcid: str, payload: list, *, prop: str | None = None, source: str = SOURCE_PATH) -> Any:
    """One batchexecute RPC from inside the page -> its decoded payload."""
    res = await page.evaluate(_CALL, {"path": BATCH_PATH, "freq": parse.freq(rpcid, payload), "rpcid": rpcid,
                                      "sourcePath": source, "timeoutMs": RPC_TIMEOUT_MS})
    status = res["status"]
    if status in (0, 401):
        raise AuthRequired(f"Search Console refused {rpcid} (HTTP {status}); the session is missing or expired.")
    if status in (403, 429):
        raise Blocked(f"Google refused {rpcid} (HTTP {status}). webcli stops here and does not work around it; "
                      "try again later.")
    if status == -1:
        raise RuntimeError(f"{rpcid} failed in the page ({res['body'][:200]}).{google.blocked()}")
    if not 200 <= status < 300:
        raise RuntimeError(f"{rpcid} failed: HTTP {status}.{google.blocked()} {SNIFF_HINT}")
    try:
        return parse.rpc_payload(res["body"], rpcid)
    except parse.RpcError as exc:
        if exc.code in parse.AUTH_CODES:
            if prop:
                raise NotFound(f"{prop} isn't a Search Console property this account can read.",
                               hint=PROPERTIES_HINT) from None
            raise AuthRequired(f"Search Console refused {rpcid} (code {exc.code}).") from None
        if exc.code in parse.NOT_FOUND_CODES | {3} and prop:
            raise NotFound(f"Search Console has nothing for that in {prop} (code {exc.code}).",
                           hint=PROPERTIES_HINT) from None
        if exc.code == 8:
            raise RateLimited(f"Search Console refused {rpcid}: a usage limit (code 8).", sent=False) from None
        raise RuntimeError(f"{exc}{google.blocked()} {SNIFF_HINT}") from None
    except ValueError:
        raise RuntimeError(f"{rpcid} answered something that isn't batchexecute JSON. {SNIFF_HINT}") from None


def _prop(value: str) -> str:
    try:
        return parse.normalize_property(value)
    except ValueError as exc:
        raise UsageError(str(exc), hint=PROPERTIES_HINT) from None


async def _properties(page: "Page") -> list[dict]:
    return parse.properties(await rpc(page, RPC["properties"], []))


async def _known(page: "Page", prop: str) -> None:
    if prop not in {p["property"] for p in await _properties(page)}:
        raise NotFound(f"{prop} isn't one of this account's Search Console properties.", hint=PROPERTIES_HINT)


# Reads

async def status(*, headed: bool = False) -> dict:
    """{signedIn, email, properties}. headed: the daemon contract (always headless)."""
    async with browser.site_page(guard.READ) as page:
        try:
            await open_app(page)
        except AuthRequired:
            return {"signedIn": False, "email": None, "properties": None}
        try:
            email = await page.evaluate(EMAIL_JS)
        except Exception:
            email = None
        return {"signedIn": True, "email": email, "properties": len(await _properties(page))}


async def properties(*, headed: bool = False) -> list[dict]:
    async with browser.site_page(guard.READ) as page:
        await open_app(page)
        return await _properties(page)


async def pages(prop: str, examples: int = DEFAULT_EXAMPLES, *, headed: bool = False) -> dict:
    """The Page indexing report: indexed / not indexed counts, and each not-indexed reason with up to `examples`
    example URLs (Search Console's own order)."""
    prop = _prop(prop)
    examples = max(0, min(int(examples), MAX_EXAMPLES))
    async with browser.site_page(guard.READ) as page:
        await open_app(page, prop)
        await _known(page, prop)
        counts = parse.index_counts(await rpc(page, RPC["series"], [prop, VIEW_INDEX_SERIES, INDEX_FILTERS],
                                              prop=prop))
        reasons = parse.reasons(await rpc(page, RPC["report"], [prop, VIEW_INDEX_REASONS,
                                                                [*INDEX_FILTERS, NOT_INDEXED]], prop=prop))
        for reason in reasons:
            reason["examples"] = []
            if examples and reason["pages"] and reason["key"]:
                key = reason["key"]
                payload = [prop, VIEW_REASON_URLS, [[4, key], *INDEX_FILTERS], None, None, None, "",
                           [None, None, [13, key]]]
                reason["examples"] = parse.reason_urls(await rpc(page, RPC["report"], payload, prop=prop))[:examples]
        return {"property": prop, **counts, "reasons": reasons}


async def sitemaps(prop: str, *, headed: bool = False) -> list[dict]:
    prop = _prop(prop)
    async with browser.site_page(guard.READ) as page:
        await open_app(page, prop)
        await _known(page, prop)
        return parse.sitemaps(await rpc(page, RPC["sitemaps"], [prop, prop], prop=prop))


async def performance(prop: str, start: str, end: str, dimension: str = "query", limit: int = 25, *,
                      headed: bool = False) -> list[dict]:
    """Search results performance (web search) for one dimension over [start, end], both YYYY-MM-DD."""
    prop = _prop(prop)
    if dimension not in DIMENSIONS:
        raise UsageError(f"--dimension must be one of {', '.join(DIMENSIONS)}.")
    try:
        first, last = parse.check_date(start, "--start"), parse.check_date(end, "--end")
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    if first > last:
        raise UsageError("--start is after --end.")
    limit = max(1, min(int(limit), MAX_LIMIT))
    async with browser.site_page(guard.READ) as page:
        await open_app(page, prop)
        await _known(page, prop)
        payload = parse.performance_request(prop, DIMENSIONS[dimension], first, last)
        rows = parse.performance(await rpc(page, RPC["report"], payload, prop=prop), dimension)
        return rows[-limit:] if dimension == "date" else rows[:limit]


async def _resolve(page: "Page", url: str, prop: str | None) -> str:
    parts = urlsplit(url or "")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UsageError(f"Not a URL: {url!r} (use https://...).")
    props = [p["property"] for p in await _properties(page)]
    if prop:
        prop = _prop(prop)
        if prop not in props:
            raise NotFound(f"{prop} isn't one of this account's Search Console properties.", hint=PROPERTIES_HINT)
        if not parse.url_in_property(url, prop):
            raise UsageError(f"{url} isn't inside {prop}.")
        return prop
    found = parse.property_for(url, props)
    if not found:
        raise NotFound(f"None of this account's properties covers {url}.", hint=PROPERTIES_HINT)
    return found


async def inspect(url: str, prop: str | None = None, *, headed: bool = False) -> dict:
    """URL Inspection's indexed data (never a live test): coverage, last crawl, canonicals, sitemaps."""
    async with browser.site_page(guard.READ) as page:
        await open_app(page)
        prop = await _resolve(page, url, prop)
        job = parse.job_id(await rpc(page, RPC["inspect_start"], [url, None, 4, INSPECT_MODE_INDEX, prop], prop=prop))
        deadline = time.monotonic() + INSPECT_TIMEOUT_S
        while not parse.job_done(await rpc(page, RPC["inspect_poll"], [job, prop], prop=prop)):
            if time.monotonic() > deadline:
                raise RuntimeError(f"URL Inspection didn't finish in {INSPECT_TIMEOUT_S}s; try again.")
            await asyncio.sleep(POLL_SECONDS)
        return parse.inspection(await rpc(page, RPC["inspect_index"], [job, prop], prop=prop), url, prop)


# Writes (each only after the CLI's --yes; each with its own one-shot guard budget)

async def submit_sitemap(prop: str, sitemap: str, *, confirm: bool = False, headed: bool = False) -> dict:
    """Submit one sitemap (the Sitemaps page's SUBMIT: one CyYwI call), then read the list back."""
    if not confirm:
        raise UsageError("sitemaps submit writes only with --yes.")
    prop = _prop(prop)
    try:
        sitemap = parse.check_sitemap_url(sitemap, prop)
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    budget = guard.Budget({WRITE_RPC["sitemap_submit"]: 1})
    async with browser.site_page(guard.submit_policy(prop, sitemap, budget)) as page:
        await open_app(page, prop)
        await _known(page, prop)
        await rpc(page, WRITE_RPC["sitemap_submit"], [prop, sitemap], prop=prop, source="/search-console/sitemaps")
        listed = parse.sitemaps(await rpc(page, RPC["sitemaps"], [prop, prop], prop=prop))
        row = next((r for r in listed if r["sitemap"] == sitemap), None)
        return {"submitted": True, "property": prop, "sitemap": sitemap, "listed": row is not None, "row": row}


def _hits(text: str, patterns: list[re.Pattern]) -> list[int]:
    return [len(p.findall(text)) for p in patterns]


async def _wait_text(page: "Page", patterns: list[re.Pattern], timeout_s: float,
                     baseline: list[int] | None = None) -> tuple[int, str] | None:
    """(index, text) of the first pattern that shows more often than in `baseline` (a notice already on the page
    before the click doesn't count), or None after timeout_s."""
    base = baseline or [0] * len(patterns)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        text = await page.evaluate(TEXT_JS) or ""
        for i, (pattern, n) in enumerate(zip(patterns, _hits(text, patterns))):
            if n > base[i]:
                return i, pattern.search(text).group(0)
        await asyncio.sleep(1.0)
    return None


async def request_indexing(url: str, prop: str | None = None, *, confirm: bool = False,
                           headed: bool = False) -> dict:
    """URL Inspection's REQUEST INDEXING, clicked in Search Console's own page: it runs a live test, then sends one
    indexing request (Fj3Owf, allowed once by the guard). {requested, url, property, message}."""
    if not confirm:
        raise UsageError("inspect --request-indexing writes only with --yes.")
    budget = guard.Budget({WRITE_RPC["request_indexing"]: 1})
    async with browser.site_page(guard.request_indexing_policy(budget)) as page:
        await open_app(page)
        prop = await _resolve(page, url, prop)
        await open_app(page, prop)
        box = page.locator("input[type=text]").first
        await box.fill(url)
        await box.press("Enter")
        found = await _wait_text(page, [re.compile(r"^request indexing$", re.I | re.M)], INSPECT_TIMEOUT_S)
        if found is None:
            raise RuntimeError(f"URL Inspection showed no REQUEST INDEXING for {url}.{google.blocked()}")
        outcomes = [RI_DONE, RI_QUOTA, RI_FAILED, RI_CANT]
        baseline = _hits(await page.evaluate(TEXT_JS) or "", outcomes)
        await page.get_by_text(re.compile(r"^request indexing$", re.I)).first.click()
        outcome = await _wait_text(page, outcomes, REQUEST_INDEXING_TIMEOUT_S, baseline)
        sent = budget.sent.get(WRITE_RPC["request_indexing"], 0)
        if outcome is None:
            raise RuntimeError(f"Search Console didn't confirm the indexing request in {REQUEST_INDEXING_TIMEOUT_S}s "
                               f"(request sent: {bool(sent)}). Check URL Inspection before trying again.")
        index, message = outcome
        if index == 1:
            raise RateLimited("Search Console: indexing request quota exceeded for today.", sent=bool(sent))
        if index in (2, 3) or not sent:
            raise RuntimeError(f"Search Console didn't take the indexing request: {message} "
                               f"(request sent: {bool(sent)}).{google.blocked()}")
        return {"requested": True, "url": url, "property": prop, "message": "Indexing requested",
                "requestsSent": sent}
