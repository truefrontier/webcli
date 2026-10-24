"""www.amazon.com (webcli amazon): your Amazon.com orders, card transactions, shipment tracking, and wish lists, for
matching card charges to orders. Identity provider "amazon" (amazon:default, its own sign-in: a person runs
`webcli amazon login`, the same as `webcli identity login amazon:default`). HARD read-only: the guard allows GETs
of the read pages only, and one POST shape (the transactions page's next/previous page)."""

from __future__ import annotations

from webcli.sites.base import Site, Strategy


def _cli():
    from webcli.sites.amazon.cli import group

    return group


async def _login(timeout: int, on_state=lambda _state: None) -> dict:
    from webcli.sites.amazon import fetch

    return await fetch.login(timeout, on_state)


ADAPTER = Site(
    name="amazon",
    kind="data",
    provider="amazon",
    summary="Amazon.com, read-only: orders, order, transactions (card charges with order ids), match AMOUNT, track, "
            "wishlists, wishlist, status",
    cli=_cli,
    fetch="webcli.sites.amazon.fetch",
    ops=("status", "orders", "order", "transactions", "match", "track", "wishlists", "wishlist"),
    verbs={"orders": Strategy.UI, "order": Strategy.UI, "transactions": Strategy.UI, "match": Strategy.UI,
           "track": Strategy.UI, "wishlists": Strategy.UI, "wishlist": Strategy.COOKIE, "status": Strategy.UI},
    login=_login,
    status_op="status",
)
