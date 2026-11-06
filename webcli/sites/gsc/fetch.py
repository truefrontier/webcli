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
import json
import re
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, quote, urlsplit

from webcli.core import browser
from webcli.core.errors import AuthRequired, NotFound, RateLimited, UsageError
from webcli.sites import google
from webcli.sites.google import EMAIL_JS, SIGNED_OUT_HOSTS
from webcli.sites.gsc import guard, livesitemap, parse
from webcli.sites.gsc.endpoints import APP_HOST, APP_URL, BATCH_PATH, DEFAULT_EXAMPLES, DIMENSIONS, INDEXED_KEY, \
    INSPECT_MODE_INDEX, INSPECT_TIMEOUT_S, MAX_EXAMPLES, MAX_LIMIT, NOT_INDEXED, PAGE_SETS, PAGES_ALL_KNOWN, \
    PAGES_SITEMAP, PAGES_SUBMITTED, POLL_SECONDS, REQUEST_INDEXING_TIMEOUT_S, RESULT_DIALOG, RPC, SOURCE_PATH, \
    VIEW_INDEX_REASONS, VIEW_INDEX_SERIES, VIEW_REASON_URLS, WRITE_RPC, index_filters

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

# What Search Console's REQUEST INDEXING result dialog says (endpoints.py has the flow). Read from the modal dialog
# only: other panels print their own "Something went wrong".
RI_DONE = re.compile(r"indexing requested", re.I)
RI_QUOTA = re.compile(r"quota exceeded", re.I)
RI_FAILED = re.compile(r"problem submitting your indexing request|something went wrong", re.I)
RI_CANT = re.compile(r"indexing request rejected|url is not available to google|can'?t be indexed|"
                     r"indexing not allowed", re.I)
RI_SHOWN = {"done": RI_DONE, "quota": RI_QUOTA, "failed": RI_FAILED, "rejected": RI_CANT}
# The text of the open modal dialogs ("" when none is open).
DIALOG_JS = (f"() => [...document.querySelectorAll({json.dumps(RESULT_DIALOG)})]"
             ".filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')"
             ".map(e => e.innerText || '').join('\\n')")
ANSWER_READ_S = 15


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


async def _examples(page: "Page", prop: str, filters: list, key: str, examples: int) -> dict:
    """Up to `examples` URLs for one reason key (or INDEXED_KEY) -> {examples, available}. `available` is how many
    Search Console listed (it lists at most 1000)."""
    head = [] if key == INDEXED_KEY else [key]
    payload = [prop, VIEW_REASON_URLS, [[4, *head], *filters], None, None, None, "", [None, None, [13, key]]]
    urls = parse.reason_urls(await rpc(page, RPC["report"], payload, prop=prop))
    return {"examples": urls[:examples], "available": len(urls)}


def _page_set(sitemap: str | None, listed: list[str]) -> tuple[int, str]:
    if not sitemap:
        return PAGES_ALL_KNOWN, ""
    if sitemap == "all":
        return PAGES_SUBMITTED, ""
    if sitemap not in listed:
        raise UsageError(f"{sitemap} isn't one of the property's submitted sitemaps"
                         + (f" ({', '.join(listed)})." if listed else " (it has none)."),
                         hint="run: webcli gsc sitemaps PROPERTY")
    return PAGES_SITEMAP, sitemap


async def pages(prop: str, examples: int = DEFAULT_EXAMPLES, sitemap: str | None = None, indexed: bool = False, *,
                headed: bool = False) -> dict:
    """The Page indexing report: indexed / not indexed counts, its last update and whether it is stale, and each
    not-indexed reason with up to `examples` example URLs (Search Console's own order). sitemap: None (all known
    pages), "all" (all submitted pages), or one submitted sitemap's URL, Search Console's own page-set filter.
    indexed: also list indexed URLs (up to `examples`)."""
    prop = _prop(prop)
    examples = max(0, min(int(examples), MAX_EXAMPLES))
    async with browser.site_page(guard.READ) as page:
        await open_app(page, prop)
        await _known(page, prop)
        listed = []
        if sitemap and sitemap != "all":
            listed = [r["sitemap"] for r in parse.sitemaps(await rpc(page, RPC["sitemaps"], [prop, prop], prop=prop))]
        pages_set, sitemap_url = _page_set(sitemap, listed)
        filters = index_filters(pages_set, sitemap_url)
        counts = parse.index_counts(await rpc(page, RPC["series"], [prop, VIEW_INDEX_SERIES, filters], prop=prop))
        reasons = parse.reasons(await rpc(page, RPC["report"], [prop, VIEW_INDEX_REASONS, [*filters, NOT_INDEXED]],
                                          prop=prop))
        crawls = []
        for reason in reasons:
            reason["examples"], available = [], 0
            if examples and reason["pages"] and reason["key"]:
                got = await _examples(page, prop, filters, reason["key"], examples)
                reason["examples"], available = got["examples"], got["available"]
            reason["examplesAvailable"] = available
            reason["examplesTruncated"] = len(reason["examples"]) < reason["pages"]
            crawls += [e["lastCrawled"] for e in reason["examples"]]
        out: dict[str, Any] = {"property": prop,
                               "filter": {"pages": PAGE_SETS[pages_set], "sitemap": sitemap_url or None}}
        total = (counts["indexed"] + counts["notIndexed"]) if counts["indexed"] is not None else None
        out.update(counts)
        out["total"] = total
        if indexed:
            got = {"examples": [], "available": 0}
            if examples and counts["indexed"]:
                got = await _examples(page, prop, filters, INDEXED_KEY, examples)
            out["indexedExamples"] = got["examples"]
            out["indexedExamplesAvailable"] = got["available"]
            out["indexedExamplesTruncated"] = len(got["examples"]) < (counts["indexed"] or 0)
            crawls += [e["lastCrawled"] for e in got["examples"]]
        stale, why = parse.staleness(counts["lastUpdated"], crawls, parse.report_today())
        out.update({"stale": stale, "staleReasons": why, "warning": parse.stale_warning(prop, why) if stale else None,
                    "examplesLimit": examples, "reasons": reasons})
        return out


async def sitemaps(prop: str, *, headed: bool = False) -> list[dict]:
    prop = _prop(prop)
    async with browser.site_page(guard.READ) as page:
        await open_app(page, prop)
        await _known(page, prop)
        return parse.sitemaps(await rpc(page, RPC["sitemaps"], [prop, prop], prop=prop))


async def performance(prop: str, start: str, end: str, dimension: str = "query", limit: int = 25, *,
                      headed: bool = False) -> dict:
    """Search results performance (web search) over [start, end], both YYYY-MM-DD: {property, searchType, start,
    end, dimension, lastUpdated, totals (the whole range, Search Console's own), rows (one dimension, top `limit`),
    rowsTruncated}."""
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
        try:
            summary = parse.performance_totals(await rpc(page, RPC["series"],
                                                         parse.performance_series_request(prop, first, last),
                                                         prop=prop))
        except ValueError:
            raise RuntimeError(f"Search Console's Performance answer has no totals. {SNIFF_HINT}") from None
        payload = parse.performance_request(prop, DIMENSIONS[dimension], first, last)
        rows = parse.performance(await rpc(page, RPC["report"], payload, prop=prop), dimension)
        shown = rows[-limit:] if dimension == "date" else rows[:limit]
        return {"property": prop, "searchType": "web", "start": first.isoformat(), "end": last.isoformat(),
                "dimension": dimension, "lastUpdated": summary["lastUpdated"], "totals": summary["totals"],
                "rows": shown, "rowsTruncated": len(shown) < len(rows)}


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


async def _inspection(page: "Page", url: str, prop: str) -> dict:
    """One index-status inspection (RVtklb mode 2, polled, then HZPzjb) -> parse.inspection."""
    job = parse.job_id(await rpc(page, RPC["inspect_start"], [url, None, 4, INSPECT_MODE_INDEX, prop], prop=prop))
    deadline = time.monotonic() + INSPECT_TIMEOUT_S
    while not parse.job_done(await rpc(page, RPC["inspect_poll"], [job, prop], prop=prop)):
        if time.monotonic() > deadline:
            raise RuntimeError(f"URL Inspection didn't finish in {INSPECT_TIMEOUT_S}s; try again.")
        await asyncio.sleep(POLL_SECONDS)
    return parse.inspection(await rpc(page, RPC["inspect_index"], [job, prop], prop=prop), url, prop)


async def inspect(url: str, prop: str | None = None, check_live_sitemap: bool = False, live_sitemap: bool = True, *,
                  headed: bool = False) -> dict:
    """URL Inspection's indexed data (never a live test): coverage, last crawl, canonicals, sitemaps, and notes
    where its sitemap fields look contradictory. check_live_sitemap: also read the property's submitted sitemaps
    live from the site (plain HTTPS, no Google session) and report inLiveSitemap.

    When Google's answer may lag (parse.needs_recheck: "URL is unknown to Google", or not on Google and in no
    sitemap), the live sitemap is read anyway, and if it lists the URL, Google is asked once more and the second
    answer used unless it went back to unknown (recheckedAfterUnknown). live_sitemap=False (--no-live-sitemap): no
    automatic read; a recheck then follows only when check_live_sitemap read the sitemap (the CLI refuses both)."""
    async with browser.site_page(guard.READ) as page:
        await open_app(page)
        prop = await _resolve(page, url, prop)
        first = info = await _inspection(page, url, prop)
        suspect = parse.needs_recheck(first)
        live = None
        if check_live_sitemap or (live_sitemap and suspect):
            submitted = [r["sitemap"] for r in parse.sitemaps(await rpc(page, RPC["sitemaps"], [prop, prop],
                                                                        prop=prop))]
            live = {"submittedSitemaps": submitted,
                    **await asyncio.to_thread(livesitemap.check, url, submitted, prop)}
        second = None
        if suspect and live and live.get("inLiveSitemap"):
            second = await _inspection(page, url, prop)
            info = parse.pick_answer(first, second)
        info = {**info, **(live or {}), "recheckedAfterUnknown": second is not None}
        note = parse.recheck_note(first, second, live) if suspect else None
        info["notes"] = parse.inspection_notes(info) + ([note] if note else [])
        return info


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


def ri_shown(dialog: str, page_text: str, base: dict) -> tuple[str, str, str] | None:
    """What Search Console shows since the click: (kind, text, where), kind one of RI_SHOWN. The open modal dialog
    decides; the whole page counts only for "indexing requested", and only while no modal dialog is open. base:
    {"dialog": counts, "page": counts} from before the click, so a notice already showing doesn't count."""
    if dialog.strip():
        for kind, pattern in RI_SHOWN.items():
            if len(pattern.findall(dialog)) > base["dialog"][kind]:
                return kind, " ".join(dialog.split())[:240], "dialog"
        return None
    if len(RI_DONE.findall(page_text)) > base["page"]["done"]:
        return "done", RI_DONE.search(page_text).group(0), "page"
    return None


def _counts(text: str) -> dict[str, int]:
    return {kind: len(pattern.findall(text)) for kind, pattern in RI_SHOWN.items()}


async def _texts(page: "Page") -> tuple[str, str]:
    dialog = await page.evaluate(DIALOG_JS) or ""
    return dialog, ("" if dialog.strip() else await page.evaluate(TEXT_JS) or "")


async def _wait_shown(page: "Page", timeout_s: float, base: dict) -> tuple[str, str, str] | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if shown := ri_shown(*await _texts(page), base):
            return shown
        await asyncio.sleep(1.0)
    return None


def _is_request(request) -> bool:
    """The Fj3Owf call: a POST to batchexecute labelled rpcids=Fj3Owf (the guard lets it out only alone)."""
    parts = urlsplit(request.url)
    ids = [v for k, v in parse_qsl(parts.query) if k == "rpcids"]
    return request.method == "POST" and parts.path == BATCH_PATH and ids == [WRITE_RPC["request_indexing"]]


def _keep_answers(answers: list):
    def on_response(response) -> None:
        try:
            if _is_request(response.request):
                answers.append(response)
        except Exception:
            pass  # an answer webcli can't place counts as unseen

    return on_response


async def _answer(answers: list) -> tuple[int | None, str | None]:
    """Fj3Owf's HTTP status and what was wrong with its answer (parse.rpc_error; None: nothing); (None, None) when
    webcli saw no answer. The body is read once, checked, and dropped: never kept, logged, or printed."""
    if not answers:
        return None, None
    response = answers[-1]
    try:
        body = await asyncio.wait_for(response.text(), ANSWER_READ_S)
    except Exception:
        return response.status, "unreadable"
    return response.status, parse.rpc_error(body, WRITE_RPC["request_indexing"])


def ri_message(outcome: str, shown: tuple[str, str, str] | None, sent: int, status: int | None,
               error: str | None) -> str:
    """The plain message for each outcome: whether Fj3Owf left the browser, what Google answered (status and error
    code only), and what Search Console showed."""
    if sent:
        went = "the request left the browser (Fj3Owf sent once)"
        answer = ("webcli didn't see Google's answer" if status is None else f"Google answered HTTP {status}"
                  + ("" if error is None else f", error {error}" if error.startswith("code") else f" ({error})"))
    else:
        went, answer = "the request never left the browser (Fj3Owf not sent), so nothing was submitted", None
    said = (f"no result showed in {REQUEST_INDEXING_TIMEOUT_S}s" if shown is None
            else f'the dialog said "{shown[1]}"' if shown[2] == "dialog"
            else f'the page said "{shown[1]}" (no result dialog open)')
    detail = "; ".join(x for x in (went, answer, said) if x)
    if outcome == "confirmed":
        return f"Indexing requested: {detail}."
    if outcome == "quota":
        return f"Search Console: indexing request quota exceeded for today: {detail}."
    if outcome == "unconfirmed":
        return (f"Search Console didn't confirm the indexing request: {detail}. Google may or may not have queued "
                "it; check URL Inspection in Search Console before retrying.")
    return f"Search Console didn't take the indexing request: {detail}.{google.blocked()}"


async def request_indexing(url: str, prop: str | None = None, *, confirm: bool = False,
                           headed: bool = False) -> dict:
    """URL Inspection's REQUEST INDEXING, clicked in Search Console's own page: it runs a live test, then sends one
    indexing request (Fj3Owf, allowed once by the guard). After the click it returns {requested, url, property,
    outcome, confirmed, dialog, fj3owfSent, fj3owfStatus, fj3owfError, message}; outcome (parse.ri_outcome:
    confirmed, quota, unconfirmed, not_sent) sets the exit code in cli.py. Before the click, errors raise."""
    if not confirm:
        raise UsageError("inspect --request-indexing writes only with --yes.")
    rid = WRITE_RPC["request_indexing"]
    budget = guard.Budget({rid: 1})
    answers: list = []
    async with browser.site_page(guard.request_indexing_policy(budget)) as page:
        page.on("response", _keep_answers(answers))
        await open_app(page)
        prop = await _resolve(page, url, prop)
        await open_app(page, prop)
        box = page.locator("input[type=text]").first
        await box.fill(url)
        await box.press("Enter")
        found = await _wait_text(page, [re.compile(r"^request indexing$", re.I | re.M)], INSPECT_TIMEOUT_S)
        if found is None:
            raise RuntimeError(f"URL Inspection showed no REQUEST INDEXING for {url}.{google.blocked()}")
        base = {"dialog": _counts(await page.evaluate(DIALOG_JS) or ""),
                "page": _counts(await page.evaluate(TEXT_JS) or "")}
        await page.get_by_text(re.compile(r"^request indexing$", re.I)).first.click()
        shown = await _wait_shown(page, REQUEST_INDEXING_TIMEOUT_S, base)
        sent = budget.close().get(rid, 0)  # no Fj3Owf from here on, so `sent` stays true
        status, error = await _answer(answers) if sent else (None, None)
        kind, where = (shown[0], shown[2]) if shown else (None, None)
        outcome = parse.ri_outcome(kind, where, sent, status, error)
        return {"requested": outcome == "confirmed", "url": url, "property": prop, "outcome": outcome,
                "confirmed": shown[2] if outcome == "confirmed" else None, "dialog": shown[1] if shown else None,
                "fj3owfSent": sent, "fj3owfStatus": status, "fj3owfError": error,
                "message": ri_message(outcome, shown, sent, status, error)}
