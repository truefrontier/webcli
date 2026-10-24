"""Browser-side work for webcli amazon: login, status, orders, one order, transactions, match, track, and wish lists.
HARD READ-ONLY.

Amazon serves these pages as HTML, so every command loads a read page in the identity's hidden Chrome and reads the
rendered DOM with one of scripts.py's readers (UI), then parse.py makes records. Pagination follows the order
history's own Next link (a GET), the wish list's own "show more" fragment (a GET, COOKIE), and the transactions
page's own Next page button (the one click any command makes; the guard allows that form's POST and no other).

Every command but `login` sits behind guard.POLICY. A sign-in page exits 4 with the login hint; a captcha or bot
check exits 4 too and is never touched: a person clears it in `webcli amazon login`'s window.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlsplit

from webcli.core import browser, guard
from webcli.core.errors import AuthRequired, NotFound
from webcli.sites.amazon import parse, scripts
from webcli.sites.amazon.endpoints import (
    INVOICE_URL,
    LOGIN_URL,
    MAX_PAGES,
    ORDER_URL,
    ORDERS_PAGE_SIZE,
    ORDERS_URL,
    PAGER_SECONDS,
    TRANSACTIONS_URL,
    WISHLIST_URL,
    WISHLISTS_URL,
)
from webcli.sites.amazon.guard import POLICY, request_allowed

if TYPE_CHECKING:
    from playwright.async_api import Page

LOGIN_CMD = "webcli amazon login"
EXPIRED = f"Amazon's session is missing or expired: a person runs `{LOGIN_CMD}` (Amazon may ask for a one-time code)."
CAPTCHA = (f"Amazon is showing a captcha or bot check on {{where}}. webcli never solves or skips one: a person runs "
           f"`{LOGIN_CMD}` and clears it in that window, then retries.")
SNIFF_HINT = ("Amazon's page may have changed: fix the readers in webcli/sites/amazon/scripts.py (and "
              "endpoints.py).")
READY_TRIES = 10  # ~1 s apart: how long a reader waits for a page to finish rendering
ORDERS_CAP = 500  # the most orders one command reads


def blocked() -> str:
    lines = list(dict.fromkeys(guard.state.blocked))[:6]
    return f" The guard blocked: {'; '.join(lines)}." if lines else ""


def _where(page: "Page") -> str:
    parts = urlsplit(page.url)
    return f"{parts.hostname}{parts.path}"


def check(page: "Page", result: dict | None = None) -> None:
    """AuthRequired when the page (or the reader's gate) is a sign-in or a captcha / bot check."""
    gate = (result or {}).get("gate") or {}
    if parse.captcha_url(page.url) or gate.get("captcha"):
        raise AuthRequired(CAPTCHA.format(where=_where(page)))
    if parse.signed_out_url(page.url) or gate.get("signin"):
        raise AuthRequired(f"{EXPIRED} (Amazon sent Chrome to {_where(page)}.)")


async def goto(page: "Page", url: str) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    check(page)


async def read(page: "Page", script: str, arg=None) -> dict:
    """Run a reader; while the page says it isn't ready (still rendering, or mid-navigation), wait and retry."""
    result: dict = {}
    for attempt in range(READY_TRIES):
        try:
            result = await (page.evaluate(script) if arg is None else page.evaluate(script, arg)) or {}
        except Exception as exc:  # the page navigated under the reader
            if "context" not in str(exc).lower() and "navigat" not in str(exc).lower():
                raise
            result = {}
        if result:
            check(page, result)
            if result.get("ready", True):
                return result
        await page.wait_for_timeout(1000)
    if result:
        return result
    raise RuntimeError(f"Amazon's page at {_where(page)} never rendered.{blocked()} {SNIFF_HINT}")


# Status and login.

async def status(*, headed: bool = False) -> dict:
    """{signedIn}, plus {reason} (and {challenge: true} for a captcha) when not: signed in when the order history
    renders without a trip to sign-in. headed is accepted for the daemon contract; data sites run headless."""
    async with browser.site_page(POLICY) as page:
        try:
            await goto(page, LOGIN_URL)
            result = await read(page, scripts.ORDERS)
        except AuthRequired as exc:
            info = {"signedIn": False, "reason": str(exc)}
            return {**info, "challenge": True} if "captcha" in str(exc) else info
        if not result.get("ready"):
            return {"signedIn": False, "reason": f"The order history didn't render.{blocked()}"}
        return {"signedIn": True}


async def _orders_tab(page: "Page") -> bool:
    """True once a tab shows the order history, rendered (not a sign-in, code, or captcha page)."""
    tabs = [p for p in page.context.pages if not p.is_closed()]
    if not tabs:
        raise AuthRequired("The Chrome window closed before sign-in finished.")
    for tab in tabs:
        if not parse.signed_in_url(tab.url):
            continue
        try:
            result = await tab.evaluate(scripts.ORDERS)
        except Exception:
            continue
        gate = (result or {}).get("gate") or {}
        if result and result.get("ready") and not gate.get("captcha") and not gate.get("signin"):
            return True
    return False


async def login(timeout: int, on_state: Callable[[str], None] = lambda _state: None) -> dict:
    """A visible Chrome on Amazon's order history, which sends a signed-out profile to sign in (no guard: sign-in
    may ask for a one-time code or a puzzle, which the person handles). Done once a tab shows the order history;
    then a fresh hidden, guarded Chrome on the same profile must be signed in too. Needs a person; only `identity
    login` and `amazon login` call it."""
    on_state("signing in")
    async with browser.open_page(None, mode="visible") as page:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not await _orders_tab(page):
            if loop.time() >= deadline:
                raise AuthRequired(f"Timed out after {timeout}s waiting for Amazon sign-in.")
            await asyncio.sleep(2)
    info = await status()
    if not info["signedIn"]:
        raise AuthRequired("The order history loaded in the sign-in window, but a fresh hidden Chrome on the same "
                           f"profile is not signed in: {info['reason']}")
    return info


# Orders.

async def _orders(page: "Page", *, year: int | None, since: date | None, limit: int | None) -> list[dict]:
    rows: list[dict] = []
    cap = min(limit or ORDERS_CAP, ORDERS_CAP)
    for name in parse.time_filters(year=year, since=since):
        start = 0
        for _ in range(MAX_PAGES):
            await goto(page, ORDERS_URL.format(filter=name, start=start))
            result = await read(page, scripts.ORDERS)
            if not result.get("ready") and not result.get("orders"):
                raise RuntimeError(f"The order history didn't render.{blocked()} {SNIFF_HINT}")
            cards = [parse.order_card(raw) for raw in result.get("orders") or []]
            for card in cards:
                when = parse.to_date(card["date"])
                if since and when and when < since:
                    return rows
                if card["order_id"] and all(r["order_id"] != card["order_id"] for r in rows):
                    rows.append(card)
                if len(rows) >= cap:
                    return rows
            if not result.get("next") or len(cards) < ORDERS_PAGE_SIZE:
                break
            start += ORDERS_PAGE_SIZE
    return rows


async def _invoice(page: "Page", oid: str) -> str:
    await goto(page, INVOICE_URL.format(id=oid))
    result = await read(page, scripts.PAGE_TEXT)
    text = result.get("text") or ""
    return text if oid in text else ""


async def orders(year: int | None = None, since: str | None = None, limit: int | None = None,
                 details: bool = False, *, headed: bool = False) -> list[dict]:
    """Your orders, newest first: --year, or back to --since (YYYY-MM-DD), else the last 3 months; with details,
    each order's items, prices, and payment from its invoice (one more page per order)."""
    async with browser.site_page(POLICY) as page:
        rows = await _orders(page, year=year, since=date.fromisoformat(since) if since else None, limit=limit)
        if details:
            for row in rows:
                text = await _invoice(page, row["order_id"])
                if text:
                    row["items"] = parse.invoice_items(text) or row["items"]
                    row["payment"] = parse.payment_methods(text)
                    row["total"] = parse.summary(text)["total"] or row["total"]
    return parse.scrub(rows)


async def _order_page(page: "Page", oid: str) -> dict:
    await goto(page, ORDER_URL.format(id=oid))
    result = await read(page, scripts.ORDER)
    if oid not in (result.get("text") or "") and not result.get("items"):
        raise NotFound(f"No order {oid} on this account.", hint="See `webcli amazon orders --year YYYY`.")
    return result


async def order(oid: str, *, headed: bool = False) -> dict:
    """One order: items, charges, payment (brand + last 4), shipments. NotFound when the account has no such
    order."""
    async with browser.site_page(POLICY) as page:
        result = await _order_page(page, oid)
        invoice = await _invoice(page, oid)
    return parse.scrub(parse.order_detail(oid, result, invoice))


async def track(oid: str, *, headed: bool = False) -> dict:
    """Each shipment's tracking page: status, carrier, tracking number, delivery date."""
    async with browser.site_page(POLICY) as page:
        result = await _order_page(page, oid)
        ships = [parse.shipment(s) for s in result.get("shipments") or []]
        if not any(s["track_url"] for s in ships):
            urls = list(dict.fromkeys(u for u in result.get("track_urls") or [] if u))
            ships = [{"status": None, "items": [], "track_url": u} for u in urls] or ships
        out = []
        for ship in ships:
            row = {"status": ship["status"], "delivered": None, "carrier": None, "tracking_number": None,
                   "delivery_date": None, "delivery_text": None, "items": ship["items"]}
            url = ship["track_url"]
            if url and request_allowed("GET", url):
                await goto(page, url)
                found = parse.track(await read(page, scripts.TRACK))
                row.update({k: v for k, v in found.items() if v is not None or k not in ("status",)})
            out.append(row)
    return parse.scrub({"order_id": oid, "shipments": out})


# Transactions and match.

async def _transactions(page: "Page", *, since: date | None, limit: int | None) -> list[dict]:
    await goto(page, TRANSACTIONS_URL)
    rows: list[dict] = []
    result = await read(page, scripts.TRANSACTIONS)
    for _ in range(MAX_PAGES):
        batch = [t for t in (parse.transaction(r) for r in result.get("rows") or []) if t]
        if not batch and result.get("text"):
            batch = parse.transactions_from_text(result["text"])
        if not batch and not result.get("ready"):
            raise RuntimeError(f"The transactions page didn't render.{blocked()} {SNIFF_HINT}")
        rows += batch
        dated = [parse.to_date(t["date"]) for t in batch if t.get("date")]
        if (limit and len(rows) >= limit) or (since and dated and min(dated) < since):
            break
        if not result.get("has_next") or not batch:
            break
        first = result.get("first")
        if not await page.evaluate(scripts.NEXT_PAGE):
            break
        nxt: dict = {}
        for _ in range(PAGER_SECONDS):
            await page.wait_for_timeout(1000)
            try:
                nxt = await page.evaluate(scripts.TRANSACTIONS) or {}
            except Exception:  # the form navigated the page under the reader
                nxt = {}
                continue
            check(page, nxt)
            if nxt.get("rows") and nxt.get("first") != first:
                break
            nxt = {}
        if not nxt:
            raise RuntimeError(f"The transactions page didn't show its next page within {PAGER_SECONDS}s."
                               f"{blocked()} {SNIFF_HINT}")
        result = nxt
    if since:
        rows = [t for t in rows if not t.get("date") or parse.to_date(t["date"]) >= since]
    return rows[:limit] if limit else rows


async def transactions(since: str | None = None, limit: int | None = None, *, headed: bool = False) -> list[dict]:
    """Your Payments > Transactions, newest first: each card charge or refund with its date, amount, card (brand +
    last 4), order ids, and merchant; back to --since, following the page's Next page button."""
    async with browser.site_page(POLICY) as page:
        rows = await _transactions(page, since=date.fromisoformat(since) if since else None, limit=limit)
    return parse.scrub(rows)


async def match(amount_cents: int, on: str | None = None, window: int | None = None, with_orders: bool = True, *,
                headed: bool = False) -> dict:
    """Transactions (and orders) for exactly this amount: within `window` days of `on` (default 7), else in the
    last `window` days (default 60)."""
    today = date.today()
    if on:
        center = date.fromisoformat(on)
        days = 7 if window is None else window
        start, end = center - timedelta(days=days), center + timedelta(days=days)
    else:
        days = 60 if window is None else window
        start, end = today - timedelta(days=days), today
    async with browser.site_page(POLICY) as page:
        txs = await _transactions(page, since=start, limit=None)
        found_orders = await _orders(page, year=None, since=start, limit=None) if with_orders else []
    matches = parse.match(amount_cents, txs, found_orders, start=start, end=end)
    return parse.scrub({"amount": round(amount_cents / 100, 2), "date": on, "window_days": days,
                        "start": start.isoformat(), "end": end.isoformat(), "matched": bool(matches),
                        "matches": matches, "searched": {"transactions": len(txs), "orders": len(found_orders)}})


# Wish lists.

async def _lists(page: "Page") -> list[dict]:
    await goto(page, WISHLISTS_URL)
    result = await read(page, scripts.WISHLISTS)
    lists = result.get("lists") or []
    if not lists and result.get("shown"):
        lists = [{"id": result["shown"], "name": result.get("shown_name") or "", "privacy": "", "is_default": True}]
    return [{"id": x["id"], "name": x.get("name") or "", "privacy": x.get("privacy") or None,
             "is_default": bool(x.get("is_default")), "url": WISHLIST_URL.format(id=x["id"]).split("?")[0]}
            for x in lists]


async def wishlists(*, headed: bool = False) -> list[dict]:
    """Your wish lists: id, name, privacy, default."""
    async with browser.site_page(POLICY) as page:
        return parse.scrub(await _lists(page))


def pick_list(lists: list[dict], ref: str) -> dict:
    """A list by id, exact name (case-insensitive), or the one name containing `ref`. NotFound otherwise."""
    for x in lists:
        if x["id"] == ref.strip():
            return x
    low = ref.strip().lower()
    exact = [x for x in lists if x["name"].lower() == low]
    if exact:
        return exact[0]
    partial = [x for x in lists if low in x["name"].lower()]
    if len(partial) == 1:
        return partial[0]
    raise NotFound(f"No wish list named {ref!r}" + (" (several match)." if partial else "."),
                   hint="See `webcli amazon wishlists`.")


async def wishlist(ref: str, limit: int | None = None, *, headed: bool = False) -> dict:
    """One wish list's items with prices, following its "show more" fragment to the end (or `limit`)."""
    async with browser.site_page(POLICY) as page:
        if parse.LIST_ID_RE.fullmatch(ref.strip()):
            chosen = {"id": ref.strip(), "name": ""}
        else:
            chosen = pick_list(await _lists(page), ref)
        await goto(page, WISHLIST_URL.format(id=chosen["id"]))
        result = await read(page, scripts.WISHLIST)
        if not result.get("ready") and not result.get("items"):
            raise NotFound(f"No wish list {chosen['id']}.", hint="See `webcli amazon wishlists`.")
        raw = list(result.get("items") or [])
        nxt = result.get("next")
        for _ in range(MAX_PAGES):
            if not nxt or (limit and len(raw) >= limit) or not request_allowed("GET", nxt):
                break
            more = await page.evaluate(scripts.WISHLIST_MORE, nxt) or {}
            if more.get("status") != 200:
                break
            raw += more.get("items") or []
            nxt = more.get("next")
    seen, items = set(), []
    for r in raw:
        key = r.get("item_id") or r.get("asin") or r.get("title")
        if key in seen:
            continue
        seen.add(key)
        items.append(parse.wishlist_item(r))
    items = items[:limit] if limit else items
    return parse.scrub({"id": chosen["id"], "name": chosen.get("name") or result.get("name") or "",
                        "count": len(items), "items": items})
