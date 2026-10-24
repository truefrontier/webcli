"""webcli amazon, offline: the HARD read-only guard, the parsers over what the real in-page readers returned for
Amazon-shaped pages (fixtures/amazon/readers.json, made by fixtures/amazon/make.py in a headless Chrome), the flows on
a scripted tab, and the commands over a faked daemon. Nothing reaches Amazon; no Chrome starts.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from webcli import sites
from webcli.apps import webcli as webcli_app
from webcli.core import daemon, guard
from webcli.core.errors import AuthRequired, NotFound
from webcli.sites.amazon import ADAPTER, fetch, parse, scripts
from webcli.sites.amazon.guard import POLICY, pager_body, read_path, request_allowed

from conftest import make_profile
from google_fakes import ScriptedPage, install

READERS = json.loads((Path(__file__).parent / "fixtures" / "amazon" / "readers.json").read_text())
OID = "111-2223334-5556667"
A = "https://www.amazon.com"
PAGER_BODY = READERS["pager_posts"][0]["body"]  # what Chrome really posted when NEXT_PAGE clicked Next page


# The guard.

@pytest.mark.parametrize(("method", "url", "body", "ok"), [
    ("GET", f"{A}/your-orders/orders?timeFilter=last30&startIndex=0", None, True),
    ("GET", f"{A}/gp/your-account/order-history/ref=ppx_yo_dt_b_pagination_1_2?startIndex=10", None, True),
    ("GET", f"{A}/gp/your-account/order-details?orderID={OID}", None, True),
    ("GET", f"{A}/your-orders/order-details?orderID={OID}", None, True),
    ("GET", f"{A}/gp/css/summary/print.html?orderID={OID}", None, True),
    ("GET", f"{A}/gp/your-account/ship-track?itemId=a&orderId={OID}", None, True),
    ("GET", f"{A}/progress-tracker/package/ref=ppx_yo_dt_b_track_package?orderId={OID}", None, True),
    ("GET", f"{A}/cpe/yourpayments/transactions", None, True),
    ("GET", f"{A}/hz/wishlist/ls/AAAAAAAAAAAA?viewType=list", None, True),
    ("GET", f"{A}/hz/wishlist/slv/items?filter=unpurchased&paginationToken=t", None, True),
    ("HEAD", "https://amazon.com/your-orders/orders", None, True),
    ("GET", "https://m.media-amazon.com/images/I/a.jpg", None, True),
    ("GET", "https://images-na.ssl-images-amazon.com/images/G/01/x.css", None, True),
    ("POST", f"{A}/cpe/yourpayments/transactions", PAGER_BODY, True),
    ("POST", f"{A}/payments-portal/data/widgets2/v1/customer/A1B2C3/continueWidget", PAGER_BODY, True),
    # writes: never
    ("GET", f"{A}/gp/aws/cart/add.html?ASIN.1=B000000010&Quantity.1=1", None, False),  # a GET that adds to cart
    ("GET", f"{A}/gp/cart/view.html", None, False),
    ("GET", f"{A}/cart/add-to-cart", None, False),
    ("GET", f"{A}/gp/buy/spc/handlers/display.html", None, False),
    ("GET", f"{A}/checkout/entry/cart", None, False),
    ("GET", f"{A}/gp/buyagain?ats=x", None, False),
    ("GET", f"{A}/spr/returns/gift?orderId={OID}", None, False),
    ("GET", f"{A}/returns/cart?orderId={OID}", None, False),
    ("GET", f"{A}/gp/your-account/order-details/cancel?orderID={OID}", None, False),
    ("GET", f"{A}/your-orders/order-details/return-items", None, False),
    ("GET", f"{A}/hz/wishlist/ls/AAAA/delete", None, False),
    ("GET", f"{A}/auto-deliveries/subscriptionList", None, False),
    ("GET", f"{A}/gp/flex/sign-out.html", None, False),
    ("GET", f"{A}/dp/B000000010", None, False),  # product pages carry buy buttons: not needed, not allowed
    ("GET", f"{A}/gp/css/summary/edit.html?orderID={OID}", None, False),
    ("GET", f"{A}/a/addresses", None, False),
    ("POST", f"{A}/gp/product/handle-buy-box/ref=dp_start-bbf_1_glance", "ASIN=B0&quantity=1", False),
    ("POST", f"{A}/cart/add-to-cart/ref=x", "items[0.base][asin]=B0", False),
    ("POST", f"{A}/hz/wishlist/ls/AAAAAAAAAAAA", "ppw-widgetEvent:DefaultNextPageNavigationEvent=1", False),
    ("POST", f"{A}/cpe/yourpayments/transactions", "ppw-widgetEvent%3ARemovePaymentMethodEvent=1", False),
    ("POST", f"{A}/cpe/yourpayments/transactions",
     PAGER_BODY + "&ppw-widgetEvent%3ASetDefaultPaymentMethodEvent=1", False),
    ("POST", f"{A}/cpe/yourpayments/transactions", "ppw-widgetState=st", False),  # no event at all
    ("POST", f"{A}/cpe/yourpayments/transactions", PAGER_BODY + "&action=delete", False),  # a non-ppw field
    ("POST", f"{A}/cpe/yourpayments/transactions", None, False),
    ("POST", "https://m.media-amazon.com/cpe/yourpayments/transactions", PAGER_BODY, False),
    ("PUT", f"{A}/cpe/yourpayments/transactions", PAGER_BODY, False),
    ("DELETE", f"{A}/hz/wishlist/ls/AAAAAAAAAAAA", None, False),
    ("POST", "https://unagi.amazon.com/1/events/com.amazon.csm.csa.prod", "{}", False),
    ("GET", "https://fls-na.amazon.com/1/batch/1/OE/", None, False),
    ("GET", "https://aax-us-east.amazon-adsystem.com/x", None, False),
    ("GET", f"http://www.amazon.com/your-orders/orders", None, False),
    ("GET", f"https://www.amazon.com:8443/your-orders/orders", None, False),
    ("GET", f"https://user@www.amazon.com/your-orders/orders", None, False),
    ("GET", "https://www.amazon.com.evil.example/your-orders/orders", None, False),
    ("GET", "https://smile.amazon.com/your-orders/orders", None, False),
])
def test_the_policy(method, url, body, ok):
    assert request_allowed(method, url, body) is ok


def test_the_real_pager_post_is_the_only_post_shape():
    assert READERS["next_clicked"] is True and len(READERS["pager_posts"]) == 1
    assert READERS["pager_posts"][0]["url"] == f"{A}/cpe/yourpayments/transactions"
    assert pager_body(PAGER_BODY)
    assert not pager_body("ppw-widgetEvent%3ADefaultNextPageNavigationEvent=1&x=%%")  # unreadable form


def test_read_paths_refuse_write_words_even_below_a_read_path():
    assert read_path("/hz/wishlist/ls/AAAA")
    assert not read_path("/hz/wishlist/ls/AAAA/move")
    assert not read_path("/your-orders/orders-cancel")
    assert not read_path("/your-ordersX")


def test_a_blocked_request_is_named_without_its_query_or_body():
    request = SimpleNamespace(method="POST", url=f"{A}/gp/product/handle-buy-box?session=abc0123456789secret",
                              post_data="ASIN=B0&quantity=1")
    guard.state.begin(POLICY)
    assert guard.check(request, own_page=True) is False
    assert guard.state.blocked == ["POST www.amazon.com/gp/product/handle-buy-box"]
    guard.state.end()


def test_the_site_is_registered_read_only():
    assert sites.get("amazon") is ADAPTER and ADAPTER.provider == "amazon" and ADAPTER.login is not None
    assert all(callable(getattr(fetch, op)) for op in ADAPTER.ops)
    assert not set(ADAPTER.ops) & daemon.WRITE_OPS
    assert "click()" not in "".join(getattr(scripts, n) for n in dir(scripts) if n.isupper() and n != "NEXT_PAGE")


# Money, dates, ids, cards.

@pytest.mark.parametrize(("text", "value"), [
    ("$33.90", 3390), ("-$33.90", -3390), ("+$15.00", 1500), ("$1,204.17", 120417), ("33.9", 3390),
    ("33", 3300), ("Grand Total: $0.99", 99), (33.9, 3390), ("", None), ("none", None), (None, None),
])
def test_cents(text, value):
    assert parse.cents(text) == value


def test_parse_amount():
    assert parse.parse_amount("33.90") == parse.parse_amount("$33.90") == parse.parse_amount("-33.9") == 3390
    for bad in ("abc", "0", ""):
        with pytest.raises(ValueError):
            parse.parse_amount(bad)


def test_dates():
    today = date(2026, 10, 24)
    assert parse.to_date("October 20, 2026") == date(2026, 10, 20)
    assert parse.to_date("Sept. 3, 2025") == date(2025, 9, 3)
    assert parse.to_date("Delivered Thursday, October 22", today=today) == date(2026, 10, 22)
    assert parse.to_date("Delivered December 30", today=date(2027, 1, 2)) == date(2026, 12, 30)
    assert parse.to_date("2026-10-01") == date(2026, 10, 1) and parse.to_date("soon") is None
    assert parse.parse_since("30d", today) == date(2026, 9, 24) == parse.parse_since("2026-09-24", today)
    with pytest.raises(ValueError):
        parse.parse_since("last tuesday", today)


@pytest.mark.parametrize(("kwargs", "filters"), [
    ({}, ["months-3"]), ({"year": 2024}, ["year-2024"]), ({"since": date(2026, 10, 1)}, ["last30"]),
    ({"since": date(2026, 8, 1)}, ["months-3"]), ({"since": date(2024, 6, 1)}, ["year-2026", "year-2025", "year-2024"]),
])
def test_time_filters(kwargs, filters):
    assert parse.time_filters(today=date(2026, 10, 24), **kwargs) == filters


def test_order_ids():
    assert parse.order_id(f"https://www.amazon.com/gp/your-account/order-details?orderID={OID}") == OID
    assert parse.order_id("D01-1234567-1234567") == "D01-1234567-1234567"
    with pytest.raises(ValueError):
        parse.order_id("12345")


@pytest.mark.parametrize(("text", "alts", "display"), [
    ("Visa ****4242", (), "Visa ****4242"),
    ("Visa ending in 4242", (), "Visa ****4242"),
    ("Visa | Last digits: 4242", (), "Visa ****4242"),
    ("•••• 4242", ("Visa",), "Visa ****4242"),
    ("Prime Visa ending in 0005", (), "Prime Visa ****0005"),
    ("MasterCard ****5555", (), "Mastercard ****5555"),
    ("Amex xxxx1001", (), "American Express ****1001"),
    ("ending in 9999", (), "Card ****9999"),
])
def test_cards_are_brand_and_last4(text, alts, display):
    assert parse.card(text, alts)["display"] == display


def test_card_numbers_are_masked_everywhere_but_ids_and_tracking():
    record = {"note": "paid with 4111 1111 1111 1234 and 4111111111111234 and 3782-822463-10005",
              "order_id": OID, "tracking_number": "9400111899223817563921", "nested": [{"x": "4111-1111-1111-1234"}],
              "fedex": "tracking 123456789012345"}
    out = parse.scrub(record)
    assert out["note"] == "paid with ****1234 and ****1234 and ****0005"
    assert out["order_id"] == OID and out["tracking_number"] == "9400111899223817563921"
    assert out["nested"] == [{"x": "****1234"}]


# The readers' answers (real Chrome over Amazon-shaped pages) -> records.

def test_the_gate_catches_sign_in_and_captchas():
    assert READERS["signin"]["gate"]["signin"] and not READERS["signin"]["gate"]["captcha"]
    assert READERS["captcha"]["gate"]["captcha"] and READERS["captcha_inline"]["gate"]["captcha"]
    assert READERS["orders"]["gate"] == {"captcha": False, "signin": False, "host": "www.amazon.com",
                                         "path": "/your-orders/orders"}


def test_order_cards():
    rows = [parse.order_card(o) for o in READERS["orders"]["orders"]]
    assert rows[0] == {
        "order_id": OID, "date": "2026-10-20", "total": 33.9, "ship_to": "Pat Example",
        "status": "Delivered October 22",
        "items": [{"title": "The First Book: A Novel", "asin": "0000000001", "qty": 2, "price": None},
                  {"title": "Second Book (Paperback)", "asin": "0000000002", "qty": 1, "price": 9.98}],
        "details_url": f"{A}/gp/your-account/order-details?orderID={OID}&ref=ppx_yo2ov_dt_b_fed_order_details"}
    assert rows[1]["total"] == 1204.17 and rows[1]["date"] == "2026-09-30"
    assert all("B0RECOMMEND" not in str(r) for r in rows)  # carousels are not order items
    assert READERS["orders"]["next"].endswith("startIndex=10")


def test_order_detail_merges_the_page_and_the_invoice():
    record = parse.order_detail(OID, READERS["order"], READERS["invoice"]["text"])
    assert record["items"] == [{"title": "The First Book: A Novel", "qty": 2, "price": 10.77},
                               {"title": "Second Book (Paperback)", "qty": 1, "price": 9.98}]
    assert (record["subtotal"], record["shipping"], record["total_before_tax"], record["tax"], record["total"]) == (
        31.52, 0.0, 31.52, 2.38, 33.9)
    assert record["payment"] == [{"brand": "Visa", "last4": "4242", "display": "Visa ****4242"}]
    assert record["date"] == "2026-10-20" and record["ship_to"] == "Pat Example"
    assert record["status"] == "Delivered October 22" and len(record["shipments"]) == 1
    assert "ship-track" in record["shipments"][0]["track_url"]
    assert "Example St" not in json.dumps(record)  # the name, never the address
    without = parse.order_detail(OID, READERS["order"], "")  # no invoice: the page alone
    assert without["total"] == 33.9 and without["payment"][0]["display"] == "Visa ****4242"
    assert without["items"][0] == {"title": "The First Book: A Novel", "asin": "0000000001", "qty": 2, "price": 10.77}


def test_transactions():
    rows = [parse.transaction(r) for r in READERS["transactions"]["rows"]]
    assert rows == [
        {"date": None, "amount": 12.49, "type": "charge",
         "card": {"brand": "Mastercard", "last4": "5555", "display": "Mastercard ****5555"},
         "order_ids": ["113-0000000-0000009"], "merchant": "AMZN Mktp US", "status": "pending"},
        {"date": "2026-10-21", "amount": 33.9, "type": "charge",
         "card": {"brand": "Visa", "last4": "4242", "display": "Visa ****4242"},
         "order_ids": [OID], "merchant": "AMAZON.COM", "status": "completed"},
        {"date": "2026-10-02", "amount": 15.0, "type": "refund",
         "card": {"brand": "Visa", "last4": "4242", "display": "Visa ****4242"},
         "order_ids": ["112-0000000-0000001"], "merchant": "AMAZON.COM", "status": "completed"}]
    assert READERS["transactions"]["has_next"] is True


def test_transactions_from_text_when_the_line_items_have_no_classes():
    text = "\n".join(r["text"] if i else r["text"] for i, r in enumerate(READERS["transactions"]["rows"]))
    text = ("In Progress\n" + READERS["transactions"]["rows"][0]["text"] + "\nCompleted\nOctober 21, 2026\n"
            + READERS["transactions"]["rows"][1]["text"] + "\nOctober 2, 2026\n"
            + READERS["transactions"]["rows"][2]["text"])
    rows = parse.transactions_from_text(text)
    assert [(r["date"], r["amount"], r["type"], r["order_ids"]) for r in rows] == [
        (None, 12.49, "charge", ["113-0000000-0000009"]), ("2026-10-21", 33.9, "charge", [OID]),
        ("2026-10-02", 15.0, "refund", ["112-0000000-0000001"])]


def test_track():
    assert parse.track(READERS["track"], today=date(2026, 10, 24)) == {
        "status": "Delivered Thursday, October 22", "delivered": True, "carrier": "UPS",
        "tracking_number": "1Z999AA10123456784", "delivery_date": "2026-10-22",
        "delivery_text": "Delivered Thursday, October 22 Your package was left near the front door."}


def test_wishlists_and_items():
    assert [(x["id"], x["name"], x["is_default"]) for x in READERS["wishlists"]["lists"]] == [
        ("AAAAAAAAAAAA", "Shopping List", True), ("BBBBBBBBBBBB", "Books to read", False)]
    items = [parse.wishlist_item(r) for r in READERS["wishlist"]["items"] + READERS["wishlist_more"]["items"]]
    assert [(i["title"], i["price"], i["added"], i["available"]) for i in items] == [
        ("Cast Iron Skillet, 12 inch", 24.99, "2026-10-03", True), ("Discontinued Gadget", None, "2026-08-01", False),
        ("Kitchen Timer", 7.5, "2026-07-09", True)]
    assert "showMoreUrl" not in READERS["wishlist"]["next"] and "/hz/wishlist/slv/items" in READERS["wishlist"]["next"]
    assert READERS["wishlist_more"]["next"] is None  # #endOfListMarker


def test_match_finds_the_charge_and_its_orders_items():
    txs = [parse.transaction(r) for r in READERS["transactions"]["rows"]]
    orders = [parse.order_card(o) for o in READERS["orders"]["orders"]]
    found = parse.match(3390, txs, orders, start=date(2026, 10, 13), end=date(2026, 10, 27))
    assert len(found) == 1 and found[0]["source"] == "transaction" and found[0]["order_ids"] == [OID]
    assert [i["title"] for i in found[0]["orders"][0]["items"]] == ["The First Book: A Novel", "Second Book (Paperback)"]
    assert parse.match(3390, txs, orders, start=date(2026, 11, 1), end=date(2026, 11, 30)) == []
    only_order = parse.match(120417, txs, orders, start=date(2026, 9, 1), end=date(2026, 10, 24))
    assert [(m["source"], m["order_ids"]) for m in only_order] == [("order", ["112-0000000-0000001"])]
    assert parse.match(1249, txs, [], start=date(2026, 10, 1), end=date(2026, 10, 24))[0]["orders"] == [
        {"order_id": "113-0000000-0000009"}]  # pending: no date, still a candidate


# The flows, on a scripted tab.

class AmazonPage(ScriptedPage):
    """A tab whose readers answer from READERS by the page it is on: pages maps a URL substring -> reader answer."""

    def __init__(self, pages: dict, **kwargs) -> None:
        super().__init__(**kwargs)
        self.pages, self.evaluated = pages, []

    async def evaluate(self, script: str, arg=None):
        for key, name in (("ordersContainer", "ORDERS"), ("viewPaymentPlanSummaryWidget", "ORDER"),
                          ("primaryStatus", "TRACK"), ("apx-transaction-date-container", "TRANSACTIONS"),
                          ("next.click()", "NEXT_PAGE"), ("wl-list-entry-title", "WISHLISTS"),
                          ("readItems(document)", "WISHLIST"), ("DOMParser", "WISHLIST_MORE"),
                          ("txt(document.body).slice(0, 60000)", "PAGE_TEXT")):
            if key in script:
                self.evaluated.append(name)
                for mark, answer in self.pages.items():
                    if mark == name or (mark in self.url and not mark.isupper()):
                        value = answer(arg) if callable(answer) else answer
                        if isinstance(value, dict) and name in value:
                            value = value[name]
                        return value
                return {}
        raise AssertionError("unscripted evaluate")


def test_orders_follow_next_and_stop_at_since(monkeypatch):
    first = READERS["orders"]
    second = {**first, "next": None, "orders": [{**first["orders"][1], "header": first["orders"][1]["header"].replace(
        "September 30, 2026", "August 2, 2026").replace("112-0000000-0000001", "112-0000000-0000002"),
        "order_id": "112-0000000-0000002"}]}
    full = {**first, "orders": first["orders"] * 5}  # a full page of 10 (5 x 2): there is a next page
    full["orders"] = [{**o, "order_id": f"11{i}-0000000-000000{i}"} for i, o in enumerate(full["orders"])]
    page = install(monkeypatch, AmazonPage({"startIndex=0": full, "startIndex=10": second}))
    rows = asyncio.run(fetch.orders(None, "2026-09-01", None, False))
    assert page.policy is POLICY
    assert page.gotos == [f"{A}/your-orders/orders?timeFilter=months-3&startIndex=0",
                          f"{A}/your-orders/orders?timeFilter=months-3&startIndex=10"] or page.gotos[0].endswith(
        "startIndex=0")
    assert len(rows) == 10 and all(r["date"] >= "2026-09-01" for r in rows)  # the August order stopped it
    install(monkeypatch, AmazonPage({"startIndex=0": first}))
    assert [r["order_id"] for r in asyncio.run(fetch.orders(2026, None, 1, False))] == [OID]


def test_orders_details_fill_items_and_payment_from_the_invoice(monkeypatch):
    first = {**READERS["orders"], "next": None, "orders": READERS["orders"]["orders"][:1]}
    page = install(monkeypatch, AmazonPage({"your-orders": first, "print.html": READERS["invoice"]}))
    rows = asyncio.run(fetch.orders(None, None, None, True))
    assert rows[0]["items"][0] == {"title": "The First Book: A Novel", "qty": 2, "price": 10.77}
    assert rows[0]["payment"][0]["display"] == "Visa ****4242"
    assert page.gotos[-1] == f"{A}/gp/css/summary/print.html?orderID={OID}"


def test_signed_out_and_captcha_are_auth_required_never_empty(monkeypatch):
    install(monkeypatch, AmazonPage({}, redirects={f"{A}/your-orders/orders?timeFilter=last30&startIndex=0":
                                                   f"{A}/ap/signin?openid.return_to=x"}))
    out = asyncio.run(fetch.status())
    assert out["signedIn"] is False and "webcli amazon login" in out["reason"] and "challenge" not in out
    install(monkeypatch, AmazonPage({"your-orders": READERS["captcha_inline"]}))
    out = asyncio.run(fetch.status())
    assert out["signedIn"] is False and out["challenge"] is True and "captcha" in out["reason"]
    install(monkeypatch, AmazonPage({"your-orders": READERS["captcha_inline"]}))
    with pytest.raises(AuthRequired, match="never solves or skips"):
        asyncio.run(fetch.orders(None, None, None, False))
    install(monkeypatch, AmazonPage({}, redirects={f"{A}/cpe/yourpayments/transactions":
                                                   f"{A}/errors/validateCaptcha"}))
    with pytest.raises(AuthRequired, match="captcha"):
        asyncio.run(fetch.transactions(None, None))
    install(monkeypatch, AmazonPage({"your-orders": READERS["orders"]}))
    assert asyncio.run(fetch.status()) == {"signedIn": True}


def test_order_and_a_missing_order(monkeypatch):
    page = install(monkeypatch, AmazonPage({"order-details": READERS["order"], "print.html": READERS["invoice"]}))
    record = asyncio.run(fetch.order(OID))
    assert record["total"] == 33.9 and record["payment"][0]["last4"] == "4242"
    assert page.gotos == [f"{A}/gp/your-account/order-details?orderID={OID}",
                          f"{A}/gp/css/summary/print.html?orderID={OID}"]
    install(monkeypatch, AmazonPage({"order-details": {**READERS["order"], "text": "We're unable to load your order",
                                                       "items": []}}))
    with pytest.raises(NotFound):
        asyncio.run(fetch.order("999-9999999-9999999"))


def test_track_reads_each_shipments_tracking_page(monkeypatch):
    page = install(monkeypatch, AmazonPage({"order-details": READERS["order"], "ship-track": READERS["track"]}))
    out = asyncio.run(fetch.track(OID))
    assert out["order_id"] == OID and len(out["shipments"]) == 1
    ship = out["shipments"][0]
    assert (ship["carrier"], ship["tracking_number"], ship["delivered"]) == ("UPS", "1Z999AA10123456784", True)
    assert ship["items"] == ["The First Book: A Novel", "Second Book (Paperback)"]
    assert "ship-track" in page.gotos[-1]


def test_transactions_click_next_page_until_since(monkeypatch):
    first = READERS["transactions"]
    older = {**first, "first": "older", "has_next": True, "rows": [
        {**first["rows"][1], "date": "September 20, 2026", "text": first["rows"][1]["text"].replace("33.90", "8.00")}]}
    states = {"page": 0}

    def tx(_arg):
        return first if states["page"] == 0 else older

    def click(_arg):
        states["page"] += 1
        return True

    page = install(monkeypatch, AmazonPage({"TRANSACTIONS": tx, "NEXT_PAGE": click}))
    rows = asyncio.run(fetch.transactions("2026-10-01", None))
    assert [r["amount"] for r in rows] == [12.49, 33.9, 15.0]  # the Sept 20 row is past --since
    assert page.evaluated.count("NEXT_PAGE") == 1 and states["page"] == 1
    assert page.gotos == [f"{A}/cpe/yourpayments/transactions"]
    install(monkeypatch, AmazonPage({"TRANSACTIONS": first, "NEXT_PAGE": False}))
    assert len(asyncio.run(fetch.transactions(None, 2))) == 2


def test_match_end_to_end(monkeypatch):
    first = {**READERS["transactions"], "has_next": False}
    orders = {**READERS["orders"], "next": None}
    install(monkeypatch, AmazonPage({"TRANSACTIONS": first, "your-orders": orders}))
    out = asyncio.run(fetch.match(3390, "2026-10-21", None, True))
    assert out["matched"] is True and out["window_days"] == 7 and out["start"] == "2026-10-14"
    assert out["matches"][0]["orders"][0]["items"][1]["title"] == "Second Book (Paperback)"
    assert out["searched"] == {"transactions": 2, "orders": 1}  # in the window: pending + Oct 21; Oct 20
    install(monkeypatch, AmazonPage({"TRANSACTIONS": first}))
    out = asyncio.run(fetch.match(4444, "2026-10-21", 3, False))
    assert out["matched"] is False and out["matches"] == [] and out["searched"]["orders"] == 0


def test_wishlist_by_name_follows_show_more(monkeypatch):
    page = install(monkeypatch, AmazonPage({"WISHLISTS": READERS["wishlists"], "WISHLIST": READERS["wishlist"],
                                            "WISHLIST_MORE": READERS["wishlist_more"]}))
    out = asyncio.run(fetch.wishlist("shopping list"))
    assert out["id"] == "AAAAAAAAAAAA" and out["name"] == "Shopping List" and out["count"] == 3
    assert [i["price"] for i in out["items"]] == [24.99, None, 7.5]
    assert page.gotos == [f"{A}/hz/wishlist/ls", f"{A}/hz/wishlist/ls/AAAAAAAAAAAA?viewType=list"]
    with pytest.raises(NotFound):
        asyncio.run(fetch.wishlist("no such list"))
    lists = asyncio.run(fetch.wishlists())
    assert [x["name"] for x in lists] == ["Shopping List", "Books to read"]


# The commands, over a faked daemon.

@pytest.fixture
def calls(monkeypatch):
    make_profile("amazon:default")
    seen, answers = [], {}

    async def call(ident, site, op, *args, **kwargs):
        seen.append((op, args))
        return answers[op]

    monkeypatch.setattr(daemon, "call", call)
    return seen, answers


def run(*args):
    return CliRunner().invoke(webcli_app.build(), ["amazon", *args])


def test_commands_pass_their_arguments(calls):
    seen, answers = calls
    answers.update(orders=[parse.order_card(READERS["orders"]["orders"][0])], order={"order_id": OID},
                   transactions=[], match={"amount": 33.9, "matched": True, "matches": []},
                   track={"order_id": OID, "shipments": []}, wishlists=[], wishlist={"id": "A", "name": "L",
                                                                                    "count": 0, "items": []})
    assert json.loads(run("orders", "--year", "2026", "--json").stdout)[0]["order_id"] == OID
    assert run("orders", "--since", "2026-10-01", "--limit", "5", "--details", "--json").exit_code == 0
    assert json.loads(run("order", f"{A}/gp/your-account/order-details?orderID={OID}", "--json").stdout) == {
        "order_id": OID}
    assert run("transactions", "--since", "2026-09-01", "--json").exit_code == 0
    assert json.loads(run("match", "$33.90", "--date", "2026-10-21", "--json").stdout)["matched"] is True
    assert run("match", "33.9", "--window", "30", "--no-orders", "--json").exit_code == 0
    assert run("track", OID, "--json").exit_code == 0
    assert run("wishlists", "--json").exit_code == 0
    assert json.loads(run("wishlist", "Books to read", "--json").stdout)["id"] == "A"
    assert seen == [("orders", (2026, None, None, False)), ("orders", (None, "2026-10-01", 5, True)),
                    ("order", (OID,)), ("transactions", ("2026-09-01", None)),
                    ("match", (3390, "2026-10-21", None, True)), ("match", (3390, None, 30, False)),
                    ("track", (OID,)), ("wishlists", ()), ("wishlist", ("Books to read", None))]


def test_usage_errors_exit_2(calls):
    assert run("order", "not-an-order").exit_code == 2
    assert run("match", "abc").exit_code == 2
    assert run("orders", "--year", "2026", "--since", "30d").exit_code == 2
    assert run("transactions", "--since", "someday").exit_code == 2


def test_no_profile_exits_4_without_a_browser():
    result = run("orders", "--json")
    assert result.exit_code == 4 and "webcli identity login amazon:default" in result.output
    assert run("status", "--json").exit_code == 4


def test_agent_help_says_read_only():
    result = run("agent-help")
    assert result.exit_code == 0 and "HARD read-only" in result.output and "match" in result.output
