"""Pure helpers for webcli amazon: money, dates, order ids, card masking, and the readers' text turned into records.
No browser. Every record leaves here scrubbed: a card number is only ever its brand and last 4."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit

from webcli.sites.amazon.endpoints import CAPTCHA_PATHS, SIGNED_IN_PATHS, SIGNED_OUT_PATHS, SITE_HOSTS

ORDER_ID_RE = re.compile(r"\b(\d{3}-\d{7}-\d{7}|D\d{2}-\d{7}-\d{7})\b")
MONEY_RE = re.compile(r"([-+−]?)\s*\$\s?(\d{1,3}(?:,\d{3})*|\d+)(?:\.(\d{2}))?")
LIST_ID_RE = re.compile(r"^[A-Z0-9]{10,20}$")
# A long digit run that looks like a card number (13-19 digits, optionally in groups). Order ids (3-7-7) and
# 20+-digit tracking numbers don't match; tracking_number fields are never scrubbed.
CARD_RUN_RE = re.compile(r"(?<![\d-])(?:\d{13,19}|\d{4}(?:[ -]\d{4}){2}[ -]\d{1,7}|\d{4}[ -]\d{6}[ -]\d{5})(?![\d-])")
BRANDS = ("Prime Visa", "Amazon Visa", "Amazon Store Card", "Amazon Secured Card", "Prime Store Card",
          "American Express", "Amex", "Visa", "Mastercard", "MasterCard", "Discover", "JCB", "Diners Club",
          "UnionPay")
LAST4_RE = re.compile(r"(?:ending in|ending with|last digits:?|last 4:?|[*•·xX]{2,}|\.{3,})\s*(\d{4})\b", re.I)
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
MON = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
FULL_DATE_RE = re.compile(rf"\b((?:{MONTHS}|{MON})\.? \d{{1,2}}, \d{{4}})\b")
SHORT_DATE_RE = re.compile(rf"\b((?:{MONTHS}|{MON})\.? \d{{1,2}})\b(?!,? \d{{4}})")
UNSCRUBBED = frozenset({"order_id", "order_ids", "tracking_number", "asin", "item_id", "id", "url", "details_url",
                        "track_url"})


# Money and dates.

def cents(text: Any) -> int | None:
    """"$1,234.56" / "-$33.90" / "33.9" / 33.9 -> signed cents; None when there is no amount."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(round(float(text) * 100))
    s = str(text).strip()
    m = MONEY_RE.search(s)
    if not m:
        bare = re.fullmatch(r"([-+]?)(\d+)(?:\.(\d{1,2}))?", s.replace(",", ""))
        if not bare:
            return None
        sign, whole, frac = bare.groups()
        value = int(whole) * 100 + int((frac or "0").ljust(2, "0"))
        return -value if sign == "-" else value
    sign, whole, frac = m.groups()
    value = int(whole.replace(",", "")) * 100 + int(frac or 0)
    return -value if sign in ("-", "−") else value


def money(text: Any) -> float | None:
    """An amount as a number of dollars (unsigned), or None."""
    c = cents(text)
    return None if c is None else round(abs(c) / 100, 2)


def parse_amount(value: str) -> int:
    """A charge amount for `match` ("33.90", "$33.90") -> cents. ValueError when it isn't one."""
    c = cents(value)
    if c is None or c == 0:
        raise ValueError(f"{value!r} is not an amount like 33.90")
    return abs(c)


def to_date(text: Any, *, today: date | None = None) -> date | None:
    """"October 20, 2026" / "Oct 20, 2026" / "2026-10-20" -> a date. A date without a year ("October 22") takes the
    year that puts it nearest before `today` + 60 days. None when there is none."""
    if not text:
        return None
    s = re.sub(r"\s+", " ", str(text)).strip()
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    m = FULL_DATE_RE.search(s)
    if m:
        raw = m.group(1).replace(".", "").replace("Sept ", "Sep ")
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
    m = SHORT_DATE_RE.search(s)
    if m:
        today = today or date.today()
        raw = m.group(1).replace(".", "").replace("Sept ", "Sep ")
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                found = datetime.strptime(f"{raw} {today.year}", fmt).date()
            except ValueError:
                continue
            if found > today + timedelta(days=60):
                found = found.replace(year=found.year - 1)
            return found
    return None


def iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def parse_since(value: str, today: date | None = None) -> date:
    """--since: YYYY-MM-DD, or Nd / Nw / Nm (days, weeks, 30-day months back). ValueError otherwise."""
    today = today or date.today()
    s = (value or "").strip().lower()
    m = re.fullmatch(r"(\d{1,4})\s*([dwm])", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return today - timedelta(days=n * {"d": 1, "w": 7, "m": 30}[unit])
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise ValueError(f"{value!r} is not a date: use YYYY-MM-DD or 30d / 8w / 6m") from None


def time_filters(*, year: int | None = None, since: date | None = None, today: date | None = None) -> list[str]:
    """The order history filters to read, newest first: --year is one year; --since the smallest window that covers
    it (last30, months-3, then each year back to its year); neither: months-3."""
    today = today or date.today()
    if year:
        return [f"year-{year}"]
    if since is None:
        return ["months-3"]
    days = (today - since).days
    if days <= 30:
        return ["last30"]
    if days <= 90:
        return ["months-3"]
    return [f"year-{y}" for y in range(today.year, since.year - 1, -1)]


# Ids and URLs.

def order_id(value: str) -> str:
    """The order id in an id or an Amazon order URL. ValueError when there is none."""
    m = ORDER_ID_RE.search(value or "")
    if not m:
        raise ValueError("expected an Amazon order id like 111-1234567-1234567")
    return m.group(1)


def order_ids(text: str) -> list[str]:
    return list(dict.fromkeys(ORDER_ID_RE.findall(text or "")))


def on_amazon(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() in SITE_HOSTS


def signed_out_url(url: str) -> bool:
    return on_amazon(url) and urlsplit(url).path.lower().startswith(SIGNED_OUT_PATHS)


def captcha_url(url: str) -> bool:
    return on_amazon(url) and urlsplit(url).path.lower().startswith(CAPTCHA_PATHS)


def signed_in_url(url: str) -> bool:
    """A tab on the order history (login also checks the page rendered it, not a sign-in form)."""
    path = urlsplit(url).path.lower()
    return on_amazon(url) and not signed_out_url(url) and not captcha_url(url) and path.startswith(SIGNED_IN_PATHS)


# Cards.

def mask_digits(text: str) -> str:
    """Any card-number-like digit run -> "****" + its last 4."""
    def repl(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return f"****{digits[-4:]}"
    return CARD_RUN_RE.sub(repl, text)


def scrub(value: Any, key: str | None = None) -> Any:
    """Every string in a record with card-number-like runs masked to the last 4 (ids, tracking numbers, and URLs
    are left alone)."""
    if isinstance(value, dict):
        return {k: scrub(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, key) for v in value]
    if isinstance(value, str) and key not in UNSCRUBBED:
        return mask_digits(value)
    return value


def card(text: str, alts: Iterable[str] = ()) -> dict | None:
    """"Visa ****1234" / "Visa ending in 1234" / "Visa | Last digits: 1234" -> {brand, last4, display}; None when no
    last 4 is shown. The brand comes from the text before the digits, else from an image's alt text."""
    m = LAST4_RE.search(text or "")
    if not m:
        return None
    before = (text or "")[max(0, m.start() - 60):m.start()]
    brand = next((b for b in BRANDS if b.lower() in before.lower()), None)
    if brand is None:
        brand = next((b for alt in alts for b in BRANDS if b.lower() in (alt or "").lower()), None)
    brand = {"MasterCard": "Mastercard", "Amex": "American Express"}.get(brand, brand)
    last4 = m.group(1)
    return {"brand": brand, "last4": last4, "display": f"{brand or 'Card'} ****{last4}"}


def payment_methods(text: str, alts: Iterable[str] = ()) -> list[dict]:
    """Every card (brand + last 4 only) in a payment section's text, plus gift card balance and points when used."""
    out, seen = [], set()
    alts = list(alts)
    for m in LAST4_RE.finditer(text or ""):
        found = card((text or "")[max(0, m.start() - 60):m.end()], alts)
        if found and found["last4"] not in seen:
            seen.add(found["last4"])
            out.append(found)
    low = (text or "").lower()
    if "gift card" in low:
        out.append({"brand": "Amazon gift card balance", "last4": None, "display": "Amazon gift card balance"})
    if "reward points" in low or "shop with points" in low:
        out.append({"brand": "Reward points", "last4": None, "display": "Reward points"})
    return out


# Text: label / value.

def lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def after_label(text: str, label: str) -> str | None:
    """The value after `label` (case-insensitive, at a line's start): the rest of that line, else the next line."""
    rows = lines(text)
    pattern = re.compile(rf"^{label}\s*:?\s*(.*)$", re.I)
    for i, row in enumerate(rows):
        m = pattern.match(row)
        if m:
            rest = m.group(1).strip()
            if rest:
                return rest
            return rows[i + 1] if i + 1 < len(rows) else None
    return None


SUMMARY_LABELS = (
    ("subtotal", r"(?:Item\(s\) Subtotal|Items? subtotal|Subtotal)"),
    ("shipping", r"(?:Shipping & Handling|Shipping and handling|Shipping)"),
    ("discounts", r"(?:Promotion Applied|Promotions? applied|Your Coupon Savings|Coupon savings|Discount|"
                  r"Subscribe & Save|Free Shipping)"),
    ("total_before_tax", r"Total before tax"),
    ("tax", r"(?:Estimated tax to be collected|Tax Collected|Estimated tax|Sales tax|Tax)"),
    ("gift_card", r"(?:Gift Card Amount|Gift card balance)"),
    ("grand_total", r"(?:Grand Total|Order Total|Payment Total)"),
    ("refund", r"Refund Total"),
)


def summary(text: str) -> dict:
    """The order's charges from a details page or invoice: {subtotal, shipping, discounts, total_before_tax, tax,
    total, refund} in dollars (None when absent). The last of each wins: the invoice repeats per-shipment figures
    before the whole order's summary at its foot."""
    found: dict[str, int] = {}
    rows = lines(text)
    for i, row in enumerate(rows):
        for key, label in SUMMARY_LABELS:
            m = re.match(rf"^{label}\s*:?\s*(.*)$", row, re.I)
            if not m:
                continue
            rest = m.group(1).strip()
            value = cents(rest) if rest else None
            if value is None and not re.search(r"[A-Za-z]{3,}", rest) and i + 1 < len(rows):
                value = cents(rows[i + 1]) if MONEY_RE.fullmatch(rows[i + 1].strip()) else None
            if value is not None:
                found[key] = value
            break
    out = {key: (round(abs(found[key]) / 100, 2) if key in found else None)
           for key in ("subtotal", "shipping", "discounts", "total_before_tax", "tax", "gift_card", "refund")}
    out["total"] = round(abs(found["grand_total"]) / 100, 2) if "grand_total" in found else None
    return out


def invoice_items(text: str) -> list[dict]:
    """The printable invoice's "N of: Title" lines -> [{title, qty, price}] (price: the first amount after it)."""
    rows = lines(text)
    items = []
    for i, row in enumerate(rows):
        m = re.match(r"^(\d+)\s+of:\s*(.+)$", row)
        if not m:
            continue
        price = None
        for nxt in rows[i + 1:i + 10]:
            if re.match(r"^\d+\s+of:", nxt):
                break
            end = re.search(r"\$[\d,]+\.\d{2}\s*$", nxt)
            if end:  # the price cell: its own line, or the end of the item's last line
                price = money(end.group(0))
                break
        items.append({"title": m.group(2).strip(), "qty": int(m.group(1)), "price": price})
    return items


def order_date(text: str) -> str | None:
    for label in (r"Order Placed", r"Ordered on", r"Order placed", r"Order date"):
        m = re.search(rf"{label}:?\s*\n?\s*((?:{MONTHS}|{MON})\.? \d{{1,2}}, \d{{4}})", text or "", re.I)
        if m:
            return iso(to_date(m.group(1)))
    return None


def ship_to_name(text: str) -> str | None:
    """The recipient's name (never the address) from an invoice's "Shipping Address:" block."""
    value = after_label(text, r"Shipping Address")
    return value.strip() if value and not MONEY_RE.search(value) else None


# Records.

def item(raw: dict) -> dict:
    qty = re.search(r"\d+", str(raw.get("qty") or ""))
    return {"title": (raw.get("title") or raw.get("alt") or "").strip(), "asin": raw.get("asin"),
            "qty": int(qty.group(0)) if qty else 1, "price": money(raw.get("price"))}


def order_card(raw: dict) -> dict:
    """One order history card -> {order_id, date, total, ship_to, status, items, details_url}."""
    header = raw.get("header") or ""
    oid = ORDER_ID_RE.search(raw.get("order_id") or "") or ORDER_ID_RE.search(header)
    placed = after_label(header, r"Order placed") or after_label(header, r"Ordered on")
    total = after_label(header, r"Total")
    ship_to = raw.get("ship_to") or after_label(header, r"Ship to") or None
    return {"order_id": oid.group(1) if oid else None,
            "date": iso(to_date(placed)) or order_date(header),
            "total": money(total),
            "ship_to": ship_to,
            "status": raw.get("status") or None,
            "items": [item(i) for i in raw.get("items") or []],
            "details_url": raw.get("details_url") or None}


def order_detail(oid: str, page: dict, invoice: str = "") -> dict:
    """The details page reader's answer (+ the invoice's text) -> the full order record."""
    text = page.get("text") or ""
    sums = summary(invoice) if invoice else {}
    page_sums = summary(text)
    charges = {k: sums.get(k) if sums.get(k) is not None else page_sums.get(k) for k in page_sums}
    page_items = [item(i) for i in page.get("items") or []]
    items = invoice_items(invoice) if invoice else []
    for row in items:  # the invoice's item lacks a price the details page shows: take it, by title
        if row["price"] is None:
            same = next((p for p in page_items if p["title"] and p["title"].lower() == row["title"].lower()), None)
            row["price"] = same["price"] if same else None
    if not items:
        items = page_items
    payment = payment_methods(invoice) if invoice else []
    if not any(p.get("last4") for p in payment):
        payment = payment_methods(page.get("payment_text") or "", page.get("payment_alts") or []) or payment
    shipments = [shipment(s) for s in page.get("shipments") or []]
    return {"order_id": oid,
            "date": order_date(text) or order_date(invoice),
            "status": next((s["status"] for s in shipments if s.get("status")), None),
            "ship_to": page.get("ship_to") or ship_to_name(invoice),
            "items": items,
            **charges,
            "payment": payment,
            "shipments": shipments,
            "url": f"https://www.amazon.com/gp/your-account/order-details?orderID={oid}"}


def shipment(raw: dict) -> dict:
    return {"status": raw.get("status") or None, "items": [t for t in raw.get("items") or [] if t],
            "track_url": raw.get("track_url") or None}


def track(raw: dict, *, today: date | None = None) -> dict:
    """A tracking page reader's answer -> {status, delivered, carrier, tracking_number, delivery_date,
    delivery_text}."""
    text = raw.get("text") or ""
    primary = raw.get("primary") or (lines(text)[0] if lines(text) else "")
    carrier = raw.get("carrier") or ""
    m = re.search(r"(?:Shipped with|Delivered by|Carrier:?|Delivery by)\s+([A-Za-z][\w.&' -]{1,40}?)\s*(?:\n|$|Tracking)",
                  text, re.I)
    if m and not carrier:
        carrier = m.group(1).strip()
    carrier = re.sub(r"^(?:Shipped with|Delivered by|Delivery by)\s+", "", carrier, flags=re.I).strip()
    tracking = raw.get("tracking") or ""
    t = re.search(r"Tracking (?:ID|number|#):?\s*([A-Z0-9]{8,40})", tracking or text, re.I)
    when = to_date(primary, today=today) or to_date(raw.get("secondary") or "", today=today)
    return {"status": primary or None,
            "delivered": bool(re.match(r"^\s*delivered", primary or "", re.I)),
            "carrier": carrier or None,
            "tracking_number": t.group(1) if t else None,
            "delivery_date": iso(when),
            "delivery_text": " ".join(x for x in (primary, raw.get("secondary")) if x) or None}


def transaction(raw: dict) -> dict | None:
    """One transactions line item (its text, date header, section) -> {date, amount, type, card, order_ids,
    merchant, status}; None when it shows no amount."""
    rows = lines(raw.get("text") or "")
    amount_row = next((r for r in rows if re.fullmatch(r"[-+−]?\s*\$[\d,]+\.\d{2}", r)), None)
    if amount_row is None:
        amount_row = next((r for r in rows if MONEY_RE.search(r) and not ORDER_ID_RE.search(r)), None)
    if amount_row is None:
        return None
    signed = cents(amount_row)
    ids = order_ids(" ".join([raw.get("text") or ""] + list(raw.get("order_links") or [])))
    for link in raw.get("order_links") or []:
        try:
            q = parse_qs(urlsplit(link).query)
            for key in ("orderID", "orderId"):
                ids += [v for v in q.get(key, []) if ORDER_ID_RE.fullmatch(v)]
        except ValueError:
            continue
    ids = list(dict.fromkeys(ids))
    card_row = next((r for r in rows if LAST4_RE.search(r)), "")
    found = card(card_row) if card_row else None
    if found is None and card_row == "":
        gift = next((r for r in rows if "gift card" in r.lower()), None)
        found = {"brand": "Amazon gift card balance", "last4": None, "display": gift} if gift else None
    skip = {amount_row, card_row}
    merchant = next((r for r in rows if r not in skip and not ORDER_ID_RE.search(r) and not MONEY_RE.search(r)
                     and not re.match(r"^(order|refund)\b", r, re.I)), None)
    section = (raw.get("section") or "").strip()
    refund = (signed or 0) > 0 or any(re.match(r"^refund", r, re.I) for r in rows)
    return {"date": iso(to_date(raw.get("date"))),
            "amount": round(abs(signed) / 100, 2) if signed is not None else None,
            "type": "refund" if refund else "charge",
            "card": found,
            "order_ids": ids,
            "merchant": merchant,
            "status": "pending" if re.search(r"progress|pending", section, re.I) else "completed"}


def transactions_from_text(text: str) -> list[dict]:
    """The fallback reader: the page's text, split into line items at each amount, under the date lines."""
    out, current_date, section, block = [], "", "", []

    def flush() -> None:
        if block:
            row = transaction({"date": current_date, "section": section, "text": "\n".join(block)})
            if row:
                out.append(row)
            block.clear()

    for row in lines(text):
        if re.fullmatch(r"(?:In Progress|Completed|Pending)", row, re.I):
            flush()
            section = row
        elif FULL_DATE_RE.fullmatch(row):
            flush()
            current_date = row
        elif LAST4_RE.search(row) and any(re.fullmatch(r"[-+−]?\s*\$[\d,]+\.\d{2}", r) for r in block):
            flush()
            block.append(row)
        else:
            block.append(row)
    flush()
    return out


def match(target: int, transactions: list[dict], orders: list[dict], *, start: date, end: date) -> list[dict]:
    """Transactions (and orders with no matching transaction) whose amount is exactly `target` cents, dated in
    [start, end]. Each transaction match carries the orders it names (when found in `orders`)."""
    by_id = {o["order_id"]: o for o in orders if o.get("order_id")}

    def in_window(value: str | None) -> bool:
        d = to_date(value)
        return d is None or start <= d <= end

    out, covered = [], set()
    for t in transactions:
        if t.get("amount") is not None and cents(t["amount"]) == target and in_window(t.get("date")):
            linked = [summary_of(by_id[i]) if i in by_id else {"order_id": i} for i in t.get("order_ids") or []]
            covered.update(t.get("order_ids") or [])
            out.append({"source": "transaction", "date": t.get("date"), "amount": t.get("amount"),
                        "type": t.get("type"), "card": t.get("card"), "order_ids": t.get("order_ids") or [],
                        "orders": linked})
    for o in orders:
        if (o.get("order_id") not in covered and o.get("total") is not None and cents(o["total"]) == target
                and in_window(o.get("date"))):
            out.append({"source": "order", "date": o.get("date"), "amount": o.get("total"), "type": "order",
                        "card": None, "order_ids": [o["order_id"]], "orders": [summary_of(o)]})
    return out


def summary_of(order: dict) -> dict:
    return {"order_id": order.get("order_id"), "date": order.get("date"), "total": order.get("total"),
            "items": [{"title": i.get("title"), "qty": i.get("qty"), "price": i.get("price")}
                      for i in order.get("items") or []]}


def wishlist_item(raw: dict) -> dict:
    price = None
    attr = (raw.get("price_attr") or "").strip()
    try:
        value = float(attr)
        price = round(value, 2) if value > 0 and value != float("inf") else None
    except ValueError:
        price = None
    if price is None:
        price = money(raw.get("price_text"))
    added = re.sub(r"^Item added\s*", "", raw.get("added") or "", flags=re.I)
    return {"item_id": raw.get("item_id"), "asin": raw.get("asin"), "title": (raw.get("title") or "").strip(),
            "price": price, "byline": raw.get("byline") or None, "added": iso(to_date(added)) or (added or None),
            "available": bool(raw.get("available", True)), "url": raw.get("url") or None}
