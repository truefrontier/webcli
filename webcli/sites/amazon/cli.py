"""`webcli amazon`: your Amazon.com orders, card transactions, tracking, and wish lists, HARD read-only, from your
own signed-in, hidden Chrome. Built so an agent can match a card charge (say $33.90) to the order(s) behind it."""

from __future__ import annotations

import functools
from datetime import date

import click

from webcli.core.output import (
    EXIT_AUTH,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_USAGE,
    agent_output_options,
    die,
    emit_csv,
    emit_json,
    emit_rows,
    kv_table,
    resolve_format,
    select_fields,
)
from webcli.sites import data
from webcli.sites.amazon import ADAPTER as SITE
from webcli.sites.amazon import parse

ORDER_FIELDS = ["order_id", "date", "total", "status", "ship_to", "items", "details_url"]
ORDER_COMPACT = ("order_id", "date", "total", "status")
ORDER_COLUMNS = ["order_id", "date", "total", "status", "ship_to"]
TX_FIELDS = ["date", "amount", "type", "card", "order_ids", "merchant", "status"]
TX_COMPACT = ("date", "amount", "type", "order_ids")
TX_COLUMNS = ["date", "amount", "type", "card", "order_ids", "merchant", "status"]
LIST_FIELDS = ["id", "name", "privacy", "is_default", "url"]
ITEM_FIELDS = ["title", "price", "asin", "byline", "added", "available", "item_id", "url"]
NOTES = [
    "Identity amazon:default, Amazon.com's own sign-in: a person runs `webcli amazon login` once (the same as "
    "`webcli identity login amazon:default`; a visible Chrome; Amazon may ask for a one-time code). Commands run one "
    "at a time in that identity's daemon, a hidden headless Chrome: no window, no Dock icon, no focus change.",
    "Matching a card charge: `webcli amazon match 33.90 --date 2026-10-20` reads Your Payments > Transactions (each "
    "charge: date, amount, card brand + last 4, order ids) and the order history around that date, and returns the "
    "transactions (and orders) for exactly that amount, each with its orders' items. `matched` is false when none.",
    "`orders` reads the order history newest first: --year YYYY, or back to --since (YYYY-MM-DD or 30d/8w/6m), else "
    "the last 3 months; --limit stops early. Item prices are often not on the history page: --details fills items, "
    "prices, and payment from each order's invoice (one more page each).",
    "`order ORDER_ID` is everything for one order: items (qty, price), subtotal, shipping, tax, total, payment "
    "(card brand + last 4 only), shipments. `track ORDER_ID`: each shipment's status, carrier, tracking number, "
    "delivery date.",
    "`transactions` reads Your Payments > Transactions back to --since (default 30 days), following the page's own "
    "Next page button. amount is positive; type is charge or refund; status pending (In Progress) or completed.",
    "`wishlists` lists your lists; `wishlist LIST` (an id or a name) lists its items with prices.",
    "Card numbers never leave as more than brand + last 4: every record is scrubbed of card-number-like digit runs.",
    "Not signed in exits 4 (a person runs `webcli amazon login`). A captcha or bot check also exits 4 and is never "
    "solved or bypassed: a person clears it in `webcli amazon login`'s window. No such order or list exits 3.",
]
WRITES = ["Nothing. HARD read-only: no purchase, cart change, return, cancellation, subscription, wish list edit, or "
          "account edit. The guard lets GETs through only for the order, invoice, tracking, transactions, and wish "
          "list pages (never a cart, buy, checkout, return, cancel, or subscribe path) and Amazon's static hosts, and "
          "exactly one POST shape, the transactions page's next/previous page form. Everything else is aborted."]

group = data.site_group(SITE, "Amazon.com: orders, card transactions, match a charge, tracking, wish lists. HARD "
                              "read-only (unofficial). Identity amazon:default.")


def json_flag(f):
    """--json: the same as --format json."""

    @click.option("--json", "as_json", is_flag=True, help="JSON output (same as --format json).")
    @functools.wraps(f)
    def wrapper(*args, as_json=False, **kwargs):
        if as_json:
            kwargs["fmt"] = "json"
        return f(*args, **kwargs)

    return wrapper


def _order_arg(ctx: click.Context, param: click.Parameter, value: str) -> str:
    try:
        return parse.order_id(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from None


def _since_arg(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return parse.parse_since(value).isoformat()
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from None


def _date_arg(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    if value is None:
        return None
    found = parse.to_date(value)
    if found is None:
        raise click.BadParameter(f"{value!r} is not a date: use YYYY-MM-DD")
    return found.isoformat()


def _amount_arg(ctx: click.Context, param: click.Parameter, value: str) -> int:
    try:
        return parse.parse_amount(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from None


def _call(op: str, *args):
    data.need_profile()
    return data.call(SITE, op, *args)


def _record(title: str, record: dict, fmt, select, as_csv=False) -> None:
    payload = select_fields(record, select) if select else record
    out = "csv" if as_csv else resolve_format(fmt)
    if out == "csv":
        emit_csv([payload])
    elif out == "json":
        emit_json(payload)
    else:
        kv_table(title, payload, out)


LIMIT = click.option("--limit", type=click.IntRange(min=1), default=None, help="Stop after this many rows.")


@group.command("login")
@click.option("--timeout", default=600, show_default=True, type=click.IntRange(min=10), help="Seconds to wait.")
def login_cmd(timeout: int) -> None:
    """Open a VISIBLE Chrome on Amazon's sign-in so a PERSON can sign in (Amazon may ask for a one-time code or a
    puzzle). Waits until the order history shows. Same as `webcli identity login amazon:default`; never run it
    unattended."""
    from webcli.apps.common import login_and_report

    login_and_report(data.identity(), timeout)


@group.command("status")
@json_flag
@agent_output_options(rows=False)
def status_cmd(fmt, quiet) -> None:
    """Whether amazon:default is signed in to Amazon.com (exit 4 if not, or if Amazon shows a captcha)."""
    info = data.call(SITE, "status") if data.identity().profile_exists() else {"signedIn": False}
    data.emit_record("Amazon", info, fmt)
    if not info["signedIn"]:
        die("Not signed in to Amazon.com." if not info.get("challenge") else "Amazon is showing a captcha.",
            EXIT_AUTH, hint="a person runs: webcli amazon login")
    raise SystemExit(EXIT_OK)


@group.command("orders")
@click.option("--year", type=click.IntRange(min=1995, max=date.today().year + 1), default=None,
              help="One calendar year.")
@click.option("--since", callback=_since_arg, default=None, metavar="DATE",
              help="Back to this date (YYYY-MM-DD, or 30d / 8w / 6m).")
@LIMIT
@click.option("--details", is_flag=True, help="Fill items, prices, and payment from each order's invoice (slower).")
@json_flag
@agent_output_options()
def orders_cmd(year, since, limit, details, **opts) -> None:
    """Your orders, newest first: id, date, total, items (title, qty, price), ship-to name, status. Default: the
    last 3 months."""
    if year and since:
        die("Give --year or --since, not both.", EXIT_USAGE)
    rows = _call("orders", year, since, limit, details)
    emit_rows(rows, title="Amazon orders", fields=ORDER_FIELDS + (["payment"] if details else []),
              compact_fields=ORDER_COMPACT, columns=ORDER_COLUMNS, noun="orders", **opts)
    raise SystemExit(EXIT_OK)


@group.command("order")
@click.argument("order_id", callback=_order_arg, metavar="ORDER_ID")
@json_flag
@agent_output_options(rows=False)
@click.option("--select", default=None, help="Comma-separated fields.")
def order_cmd(order_id, fmt, quiet, select) -> None:
    """One order: items, subtotal, shipping, tax, total, payment (card brand + last 4), shipments. Exit 3 if the
    account has no such order."""
    _record(f"Order {order_id}", _call("order", order_id), fmt, select)
    raise SystemExit(EXIT_OK)


@group.command("transactions")
@click.option("--since", callback=_since_arg, default="30d", show_default=True, metavar="DATE",
              help="Back to this date (YYYY-MM-DD, or 30d / 8w / 6m).")
@LIMIT
@json_flag
@agent_output_options()
def transactions_cmd(since, limit, **opts) -> None:
    """Your Payments > Transactions: each card charge or refund with date, amount, card (brand + last 4), order
    id(s), merchant. The way to match a statement line to orders."""
    rows = _call("transactions", since, limit)
    emit_rows(rows, title="Amazon transactions", fields=TX_FIELDS, compact_fields=TX_COMPACT, columns=TX_COLUMNS,
              noun="transactions", **opts)
    raise SystemExit(EXIT_OK)


@group.command("match")
@click.argument("amount", callback=_amount_arg, metavar="AMOUNT")
@click.option("--date", "on", callback=_date_arg, default=None, metavar="DATE",
              help="The charge's date (YYYY-MM-DD): search --window days either side of it.")
@click.option("--window", type=click.IntRange(min=0, max=400), default=None,
              help="Days either side of --date (default 7), or back from today without --date (default 60).")
@click.option("--no-orders", is_flag=True, help="Only search transactions (skip the order history).")
@json_flag
@agent_output_options(rows=False)
def match_cmd(amount, on, window, no_orders, fmt, quiet) -> None:
    """Find the transactions (and orders) for exactly AMOUNT (e.g. 33.90), with each one's orders and items.
    Exits 0 either way; `matched` says whether anything matched."""
    result = _call("match", amount, on, window, not no_orders)
    out = resolve_format(fmt)
    if out == "json":
        emit_json(result)
    else:
        kv_table(f"Match ${result['amount']:.2f}", {k: v for k, v in result.items() if k != "matches"}, out)
        for m in result["matches"]:
            kv_table(f"{m['source']} {m['date']}", m, out)
    raise SystemExit(EXIT_OK)


@group.command("track")
@click.argument("order_id", callback=_order_arg, metavar="ORDER_ID")
@json_flag
@agent_output_options(rows=False)
def track_cmd(order_id, fmt, quiet) -> None:
    """Each shipment of an order: status, carrier, tracking number, delivery date. Exit 3 if no such order."""
    result = _call("track", order_id)
    out = resolve_format(fmt)
    if out == "json":
        emit_json(result)
    else:
        for i, ship in enumerate(result["shipments"], 1):
            kv_table(f"{order_id} shipment {i}", ship, out)
    raise SystemExit(EXIT_OK)


@group.command("wishlists")
@json_flag
@agent_output_options()
def wishlists_cmd(**opts) -> None:
    """Your wish lists: id, name, privacy, default."""
    rows = _call("wishlists")
    emit_rows(rows, title="Amazon wish lists", fields=LIST_FIELDS, compact_fields=("id", "name"),
              columns=["id", "name", "privacy", "is_default"], noun="lists", **opts)
    raise SystemExit(EXIT_OK)


@group.command("wishlist")
@click.argument("list_ref", metavar="LIST")
@LIMIT
@json_flag
@agent_output_options()
def wishlist_cmd(list_ref, limit, fmt, compact, select, quiet, as_csv) -> None:
    """One wish list's items with prices. LIST is a list id or name (see `wishlists`). Exit 3 if there is none.
    --format json gives {id, name, count, items}."""
    result = _call("wishlist", list_ref, limit)
    if resolve_format(fmt) == "json" and not (select or compact or as_csv):
        emit_json(result)
    else:
        emit_rows(result["items"], title=result["name"] or result["id"], fields=ITEM_FIELDS,
                  compact_fields=("title", "price"), columns=["title", "price", "added", "available"], noun="items",
                  fmt=fmt, compact=compact, select=select, quiet=quiet, as_csv=as_csv)
    raise SystemExit(EXIT_OK)


data.add_agent_help(group, notes=NOTES, writes=WRITES)
