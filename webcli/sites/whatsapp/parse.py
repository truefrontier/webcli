"""Pure parsers for webcli whatsapp: the app's state, the chat list's rows, one conversation's messages, search
results, which chat a name means, and the times WhatsApp shows. No browser: input is a pane's outerHTML (dom.py).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from webcli.core.errors import UsageError
from webcli.sites.whatsapp import dom
from webcli.sites.whatsapp.endpoints import MAX_TEXT, MEDIA, SECTIONS, SELECTORS, STATUS_ICONS, STATUS_LABELS

S = SELECTORS
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
TIME = re.compile(r"^(\d{1,2})[:.](\d{2})(?:[:.]\d{2})?\s*(?:([AaPp])\.?\s*[Mm]\.?)?$")
TIME_LABEL = re.compile(
    r"^(?:\d{1,2}[:.]\d{2}(?:\s*[AaPp]\.?\s*[Mm]\.?)?|today|yesterday|"
    r"(?:mon|tues|wednes|thurs|fri|satur|sun)day|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{4}-\d{2}-\d{2})$", re.I)
# A message's data-pre-plain-text: "[3:45 PM, 10/22/2026] Alice Smith: "
PRE = re.compile(r"^\[(?P<time>[^,\]]+),\s*(?P<date>[^\]]+)\]\s*(?P<sender>.*?):\s*$", re.S)
RELATIVE = re.compile(r"^(\d+)\s*([mhdw])$", re.I)
UNREAD_COUNT = re.compile(r"(\d+)\s+unread", re.I)


def names_equal(a: str | None, b: str | None) -> bool:
    """Exactly the same name: case counts; only whitespace runs and bidi marks are ignored."""
    return a is not None and b is not None and dom.clean(a) == dom.clean(b)


# --- State --------------------------------------------------------------------------------------------------------

def state(snap: dict) -> str:
    """What the app shows: "blocked" (a block or challenge page), "unlinked" (the QR code, or logged out),
    "use_here" (another tab of this profile holds WhatsApp), "linked" (the chat list, loaded), or "loading"."""
    if snap.get("block"):
        return "blocked"
    if snap.get("qr") or snap.get("loggedOut"):
        return "unlinked"
    if snap.get("useHere"):
        return "use_here"
    if snap.get("list") and not snap.get("loading"):
        return "linked"
    return "loading"


# --- Times --------------------------------------------------------------------------------------------------------

def parse_time(value: str | None) -> tuple[int, int] | None:
    m = TIME.match(dom.clean(value))
    if not m:
        return None
    hour, minute = int(m[1]), int(m[2])
    if m[3]:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if m[3].lower() == "p" else 0)
    return (hour, minute) if hour < 24 and minute < 60 else None


def parse_date(value: str | None, today: date, *, dayfirst: bool = False) -> date | None:
    """A date as WhatsApp shows it: TODAY, YESTERDAY, a weekday (within the past week), 10/22/2026 (day first only
    when the first number can't be a month, or `dayfirst`), 2026-10-22, 22.10.2026, or "October 22, 2026"."""
    label = dom.clean(value).strip(" ,").lower()
    if not label:
        return None
    if label == "today":
        return today
    if label == "yesterday":
        return today - timedelta(days=1)
    for i, day in enumerate(WEEKDAYS):
        if label in (day, day[:3]):
            back = (today.weekday() - i) % 7 or 7
            return today - timedelta(days=back)
    if m := re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", label):
        return _date(int(m[1]), int(m[2]), int(m[3]))
    if m := re.match(r"^(\d{1,2})([/.-])(\d{1,2})\2(\d{2,4})$", label):
        a, b, year = int(m[1]), int(m[3]), int(m[4])
        year += 2000 if year < 100 else 0
        day_first = a > 12 or (b <= 12 and (dayfirst or m[2] == "."))
        return _date(year, b, a) if day_first else _date(year, a, b)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%B %d %Y"):
        try:
            return datetime.strptime(label, fmt).date()
        except ValueError:
            continue
    for fmt in ("%B %d", "%b %d", "%d %B", "%d %b"):
        try:
            parsed = datetime.strptime(f"{label} {today.year}", f"{fmt} %Y").date()
        except ValueError:
            continue
        return parsed.replace(year=today.year - 1) if parsed > today else parsed
    return None


def _date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_since(value: str, now: datetime) -> datetime:
    """--since: YYYY-MM-DD (from midnight), an ISO 8601 date-time, or relative (30m, 48h, 2d, 1w). Local time.
    ValueError otherwise."""
    value = (value or "").strip()
    if m := RELATIVE.match(value):
        unit = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[m[2].lower()]
        return now - timedelta(**{unit: int(m[1])})
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{value!r} isn't a date: use YYYY-MM-DD, an ISO 8601 time, or 48h / 2d / 1w.") from None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


# --- The chat list ------------------------------------------------------------------------------------------------

def _title(node: dom.Node | None) -> str:
    if node is None:
        return ""
    return dom.clean(node.attrs.get("title")) or dom.text(node)


def _leaf_texts(node: dom.Node, skip: set[int] | None = None) -> list[str]:
    out = []
    for n in node.iter():
        if skip and id(n) in skip:
            continue
        if not any(isinstance(c, dom.Node) for c in n.children):
            value = dom.text(n)
            if value:
                out.append(value)
    return out


def _unread(row: dom.Node) -> int:
    for node in dom.select(row, S["unread"]):
        label = node.attrs.get("aria-label", "")
        if m := UNREAD_COUNT.search(label):
            return int(m[1])
        digits = re.sub(r"\D", "", dom.text(node))
        return int(digits) if digits else 1
    return 0


def _row(row: dom.Node) -> dict | None:
    name_node = dom.first(row, S["rowName"])
    name = _title(name_node)
    if not name:
        return None
    time_node = dom.first(row, S["rowTime"])
    when = dom.text(time_node) if time_node is not None else next(
        (t for t in _leaf_texts(row) if TIME_LABEL.match(t)), "")
    preview_node = dom.first(row, S["rowPreview"])
    if preview_node is not None:
        preview = _title(preview_node) if preview_node.attrs.get("title") else dom.text(preview_node)
    else:
        titled = [n for n in dom.select(row, "span[title]") if n is not name_node]
        preview = ""
        if titled:  # a group's preview: "Mom: Dinner at 7?", the sender in a sibling span
            around = titled[0].parent
            whole = dom.text(around) if around is not None and not around.contains(name_node) else ""
            preview = whole if whole.endswith(_title(titled[0])) else _title(titled[0])
    return {"name": name, "preview": preview, "time": when, "unread": _unread(row),
            "pinned": bool(dom.select(row, S["pinned"])), "muted": bool(dom.select(row, S["muted"]))}


def _rows(root: dom.Node) -> list[dom.Node]:
    return dom.outermost(dom.select(root, S["row"]))


def chats(html: str | None) -> list[dict]:
    """The chat list's rows as rendered, top first: {name, preview, time, unread (count), pinned, muted}."""
    return [row for node in _rows(dom.build(html)) if (row := _row(node))]


def merge(seen: list[dict], more: list[dict]) -> int:
    """Append rows of `more` not already in `seen` (same name, time, and preview), in order. How many were new."""
    keys = {(r["name"], r["time"], r["preview"]) for r in seen}
    added = 0
    for row in more:
        key = (row["name"], row["time"], row["preview"])
        if key not in keys:
            keys.add(key)
            seen.append(row)
            added += 1
    return added


# --- Search -------------------------------------------------------------------------------------------------------

def search(html: str | None) -> list[dict]:
    """WhatsApp's search results, top first: {type: chat|contact|message, name, preview, time, unread}. A row's type
    is its section's (the "Chats", "Contacts", "Messages" headings); rows before any heading are chats."""
    out, section = [], "chat"
    for node in _rows(dom.build(html)):
        label = dom.text(node).lower()
        if label in SECTIONS:
            section = SECTIONS[label]
            continue
        if section is None:
            continue
        row = _row(node)
        if row:
            out.append({"type": section, "name": row["name"], "preview": row["preview"], "time": row["time"],
                        "unread": row["unread"]})
    return out


# --- Which chat a name means --------------------------------------------------------------------------------------

def resolve(rows: list[dict], query: str) -> dict | None:
    """The chat `query` names: the one whose name is exactly the query (case counts); else the one whose name is
    the query ignoring case; else the one whose name contains it (ignoring case). None when nothing matches.
    UsageError (exit 2) listing the candidates when several do (two chats with the very same name included)."""
    want = dom.clean(query)
    if not want:
        return None
    unique: list[dict] = []
    for row in rows:  # the same chat seen twice (list and search) counts once
        if not any(names_equal(row["name"], u["name"]) and row.get("time") == u.get("time") for u in unique):
            unique.append(row)
    tiers = ([r for r in unique if dom.clean(r["name"]) == want],
             [r for r in unique if dom.clean(r["name"]).casefold() == want.casefold()],
             [r for r in unique if want.casefold() in dom.clean(r["name"]).casefold()])
    for found in tiers:
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            names = "; ".join(r["name"] for r in found[:10])
            raise UsageError(f"{len(found)} chats match {query!r}: {names}. Nothing was opened.",
                             hint="Give the exact chat name (see `webcli whatsapp chats` or `webcli whatsapp search`).")
    return None


# --- One conversation ---------------------------------------------------------------------------------------------

def header(html: str | None) -> str | None:
    """The open chat's name, from its header."""
    node = dom.first(dom.build(html), S["header"])
    return _title(node) or None


def _status(msg: dom.Node) -> str | None:
    """Your message's tick: its aria-label (" Read ", " Delivered "...: a blue double tick has the same icon as a grey
    one), else its data-icon."""
    for node in msg.iter():
        label = node.attrs.get("aria-label", "").strip().lower()
        if label in STATUS_LABELS:
            return STATUS_LABELS[label]
    for node in msg.iter():
        icon = node.attrs.get("data-icon", "")
        if icon in STATUS_ICONS:
            return STATUS_ICONS[icon]
    return None


def _media(msg: dom.Node, skip: set[int]) -> str | None:
    for selector, word in MEDIA:
        for node in dom.select(msg, selector):
            if id(node) in skip or (node.tag == "img" and "emoji" in node.attrs.get("class", "")):
                continue
            return word
    return None


def _quoted(msg: dom.Node) -> tuple[dict | None, set[int]]:
    blocks = dom.outermost(dom.select(msg, S["quoted"]))
    if not blocks:
        return None, set()
    block = blocks[0]
    while block.parent is not None and block.parent is not msg and "quoted-mention" in block.attrs.get("class", ""):
        block = block.parent  # a bare .quoted-mention: its quote is the element around it
    body = dom.first(block, S["quotedText"])
    quote_text = dom.text(body, lines=True) if body is not None else dom.text(block, lines=True)
    sender = None
    for node in dom.select(block, 'span[dir="auto"], [data-testid="quoted-author"]'):
        if body is not None and (node is body or body.contains(node) or node.contains(body)):
            continue
        value = dom.text(node)
        if value and value != dom.clean(quote_text):
            sender = value
            break
    return {"sender": sender, "text": quote_text}, dom.subtree_ids(blocks)


def _dayfirst(root: dom.Node) -> bool:
    """WhatsApp writes dates in the phone's locale: a 12-hour clock goes with month-first dates."""
    pres = [n.attrs.get("data-pre-plain-text", "") for n in dom.select(root, S["prePlain"])]
    if not pres:
        return False
    return not any(re.search(r"\d\s*[AaPp]\.?\s*[Mm]", p) for p in pres)


def _message(msg: dom.Node, chat: str | None, day: date | None, today: date, dayfirst: bool) -> dict | None:
    quoted, skip = _quoted(msg)
    out = msg.attrs.get("data-id", "").startswith("true_") or bool(dom.select(msg, S["messageOut"]))
    pre = next((n.attrs["data-pre-plain-text"] for n in dom.select(msg, S["prePlain"]) if id(n) not in skip), "")
    m = PRE.match(pre)
    texts = [dom.text(n, lines=True, skip=skip) for n in dom.outermost(dom.select(msg, S["text"])) if id(n) not in skip]
    body = "\n".join(t for t in texts if t)
    media = _media(msg, skip)
    if not body and not media:
        whole = dom.text(msg).lower()
        if "message was deleted" in whole or "deleted this message" in whole:
            body = "[deleted]"
        else:
            return None
    if media == "document" and not body:  # its file name, from the download button's title
        for node in dom.select(msg, '[title^="Download"]'):
            if id(node) not in skip and (m_name := re.search(r'"([^"]+)"', node.attrs["title"])):
                body = dom.clean(m_name[1])
                break
    text = f"[{media}]" + (f" {body}" if body else "") if media else body
    if out:
        sender = "You"
    elif m and m["sender"].strip():
        sender = dom.clean(m["sender"])
    else:
        author = dom.first(msg, S["author"])
        sender = dom.text(author).rstrip(":") if author is not None and id(author) not in skip else (chat or "")
    when = m["time"].strip() if m else ""
    if not when:
        node = dom.first(msg, S["msgTime"])
        when = dom.text(node) if node is not None and id(node) not in skip else ""
    if not when:
        when = next((t for t in reversed(_leaf_texts(msg, skip)) if TIME.match(t)), "")
    on = (parse_date(m["date"], today, dayfirst=dayfirst) if m else None) or day
    clock = parse_time(when)
    stamp = datetime(on.year, on.month, on.day, *clock).isoformat(timespec="minutes") if on and clock else None
    return {"sender": sender, "direction": "sent" if out else "received", "time": when, "datetime": stamp,
            "text": text, "media": media, "quoted": quoted, "status": _status(msg) if out else None}


def thread(html: str | None, chat: str | None = None, *, today: date | None = None) -> dict:
    """{header, messages}: the open chat's name and its rendered messages, oldest first. A message:
    {sender ("You" for yours), direction (sent|received), time (as shown), datetime (local ISO, null when the date
    isn't known), text (media as a placeholder: "[image] caption", "[voice note]"), media, quoted ({sender, text}
    of the message it replies to, when shown), status (yours: pending|sent|delivered|read)}."""
    today = today or date.today()
    root = dom.build(html)
    name = header(html) or chat
    dayfirst = _dayfirst(root)
    candidates = dom.outermost(dom.select(root, f'[role="row"], {S["message"]}'))
    messages, day = [], None
    for node in candidates:
        msg = node if dom.matches(node, S["message"]) else dom.first(node, S["message"])
        if msg is None:
            label = dom.text(node)
            if len(label) <= 40 and (found := parse_date(label, today, dayfirst=dayfirst)):
                day = found
            continue
        row = _message(msg, name, day, today, dayfirst)
        if row:
            messages.append(row)
    return {"header": name, "messages": messages}


def since_filter(rows: list[dict], since: datetime | None) -> list[dict]:
    """Rows at or after `since` (a row with no known datetime is left out)."""
    if since is None:
        return rows
    cut = since.isoformat(timespec="minutes")
    return [r for r in rows if r["datetime"] and r["datetime"] >= cut]


def sent_matches(rows: list[dict], text: str) -> list[dict]:
    """Your messages whose text is exactly `text`."""
    return [r for r in rows if r["direction"] == "sent" and r["media"] is None and _same_text(r["text"], text)]


def _same_text(a: str, b: str) -> bool:
    return [dom.clean(x) for x in a.splitlines() if dom.clean(x)] == [dom.clean(x) for x in b.splitlines()
                                                                         if dom.clean(x)]


def compose_matches(shown: str | None, text: str) -> bool:
    """The compose box reads back exactly `text` (line breaks and whitespace runs aside)."""
    return _same_text(shown or "", text)


def message_text(text: str | None) -> str:
    """A message's text: trimmed, not empty, at most MAX_TEXT characters, line breaks as \\n."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ValueError("Message text is empty.")
    if len(text) > MAX_TEXT:
        raise ValueError(f"Message text is {len(text)} characters; at most {MAX_TEXT}.")
    return text
