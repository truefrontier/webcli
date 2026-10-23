"""Pure helpers for webcli messages: the app's state, its conversation and message rows (what gmsg read), which
conversation a query names, and search. No browser."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from webcli.core.errors import UsageError
from webcli.sites.messages.endpoints import AUTH_PATH

ARIA_TIME = re.compile(r"(?:Sent|Received) on (.+?)(?:\.\s|$)", re.I)  # gmsg: a bubble's aria-label carries its time
THREAD = re.compile(r"(?:^|/)conversations/([^/?#]+)$")


def _clean(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def digits(text: str | None) -> str:
    return re.sub(r"\D", "", text or "")


def thread_id(url_or_path: str | None) -> str | None:
    """The conversation id in a .../conversations/<id> URL, path, or relative link (not "new"), else None."""
    match = THREAD.search(urlsplit(url_or_path or "").path)
    return match[1] if match and match[1] != "new" else None


def state(snap: dict) -> str:
    """What the app shows: "unpaired" (the QR pairing page), "use_here" (Messages is open in another window or tab:
    it offers "Use here", or a banner says so), "paired" (the conversation list, its rows in or its spinner gone), or
    "loading"."""
    if (snap.get("path") or "").startswith(AUTH_PATH) or snap.get("qr"):
        return "unpaired"
    if snap.get("useHere") or snap.get("elsewhere"):
        return "use_here"
    if snap.get("list") and (snap.get("items") or not snap.get("loading")):
        return "paired"
    return "loading"


def conversations(raw) -> list[dict]:
    """The list's rows {id, name, preview, timestamp, unread}, as the page shows them (newest first). id is the
    conversation's, from its link; None when a row has none."""
    return [{"id": thread_id(item.get("href")), "name": _clean(item.get("name")) or "Unknown",
             "preview": _clean(item.get("preview")), "timestamp": _clean(item.get("timestamp")),
             "unread": bool(item.get("unread"))}
            for item in raw or [] if isinstance(item, dict)]


def _bubbles(raw) -> list[tuple[int, dict]]:
    """(index in `raw`, row) for each bubble that makes a row: see messages()."""
    out = []
    for index, item in enumerate(raw or []):
        if not isinstance(item, dict):
            continue
        text = _clean(item.get("text")) or ("[image]" if item.get("image") else "")
        if not text:
            continue
        aria = ARIA_TIME.search(item.get("aria") or "")
        out.append((index, {"direction": "sent" if item.get("sent") else "received", "text": text,
                            "timestamp": _clean(item.get("time")) or (aria[1].strip() if aria else "")}))
    return out


def messages(raw, saved: dict[int, list[str]] | None = None) -> list[dict]:
    """One conversation's bubbles {direction: sent|received, text, timestamp}, oldest first. An image with no text is
    "[image]"; a bubble with neither is left out (gmsg). With `saved` ({index in raw: paths}, `read --image-out`),
    every row also has images: the paths its bubble's images were saved to ([] for none)."""
    return [row if saved is None else {**row, "images": saved.get(index, [])} for index, row in _bubbles(raw)]


def image_parts(raw, limit: int | None = None) -> list[tuple[int, int, int]]:
    """(bubble index in raw, part index in the bubble, n) for each image in the bubbles of the last `limit` rows, in
    order. n numbers the image among every image the conversation shows (1-based), so a file name stays the same
    whatever --limit is. A bubble's `images` is its image count (1 when only `image` is given)."""
    counts = [int(item.get("images") or bool(item.get("image"))) if isinstance(item, dict) else 0 for item in raw or []]
    rows = [index for index, _row in _bubbles(raw)]
    keep = set(rows[-limit:] if limit else rows)
    out, n = [], 0
    for index, count in enumerate(counts):
        for part in range(count):
            n += 1
            if index in keep:
                out.append((index, part, n))
    return out


def image_ext(data: bytes) -> str:
    """A saved image's file extension, from its first bytes: .jpg, .png, .gif, .webp, else .bin."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    magic = ((b"\xff\xd8\xff", ".jpg"), (b"\x89PNG\r\n\x1a\n", ".png"), (b"GIF8", ".gif"))
    return next((ext for head, ext in magic if data.startswith(head)), ".bin")


# --- Phone numbers ---------------------------------------------------------------------------------------------

NUMBERISH = re.compile(r"^(?:tel:)?\+?[\d\s().\-/]+$", re.I)
DEFAULT_COUNTRY = "1"  # a 10-digit number with no country code is a US/NANP one


def normalize_number(text: str | None, country: str = DEFAULT_COUNTRY) -> str | None:
    """`text` as an E.164 number ("+12025550100"), punctuation aside: "+" and 8-15 digits; "011" then the number
    (dialled from the US); 10 digits (the default country, +1); or 11 digits starting with the country code. None
    for anything else: letters, a short code, a local 7-digit number."""
    text = (text or "").strip()
    if not text or not NUMBERISH.match(text):
        return None
    text = re.sub(r"^tel:", "", text, flags=re.I)
    number = digits(text)
    if text.startswith("+"):
        return f"+{number}" if 8 <= len(number) <= 15 else None
    if number.startswith("011") and 11 <= len(number) <= 18:
        return f"+{number[3:]}"
    if len(number) == 10:
        return f"+{country}{number}"
    if len(number) == 10 + len(country) and number.startswith(country):
        return f"+{number}"
    return None


def is_number(text: str | None) -> bool:
    return normalize_number(text) is not None


def same_number(a: str | None, b: str | None) -> bool:
    """Two numbers are one: equal as E.164 when both normalize, else the same 7+ digits."""
    na, nb = normalize_number(a), normalize_number(b)
    if na and nb:
        return na == nb
    da, db = digits(a), digits(b)
    return len(da) >= 7 and da == db


def national(number: str) -> str:
    """What to type into the contact picker for an E.164 number: the 10 national digits for +1 (what the picker
    matches, LIVE), else the E.164 number itself."""
    return number[2:] if number.startswith("+1") and len(number) == 12 else number


def typed_forms(number: str) -> list[str]:
    """What to type into the contact picker for an E.164 number, best first. LIVE (2026-10-23): the picker matches a
    contact's number as it was saved, from its start: one saved as 2025550100 only by "2025550100", one saved as
    +12025550100 only by "+12025550100" (not by "2025550100", "12025550100", or "(202) 555-0100"). So for +1: the
    national digits, then the E.164 number, then the (202) 555-0100 form; else the E.164 number alone."""
    nat = national(number)
    if nat == number:
        return [number]
    return [nat, number, f"({nat[:3]}) {nat[3:6]}-{nat[6:]}"]


def has_number(people: list[dict], number: str) -> bool:
    """A picker contact has this number."""
    return any(same_number(n["number"], number) for c in people for n in c["numbers"])


def same_text(a: str | None, b: str | None) -> bool:
    """A bubble shows exactly this text, runs of whitespace (a line break shown as a space, or none) aside."""
    return " ".join((a or "").split()) == " ".join((b or "").split())


def _fold(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


# --- The contact picker (Start chat) ---------------------------------------------------------------------------

NUMBER_IN = re.compile(r"\+?\d[\d\s().\-]{5,}\d")


def _shown_number(text: str) -> str:
    """The phone number in a picker row's number cell, which may carry a label ("Mobile")."""
    if normalize_number(text):
        return text
    found = NUMBER_IN.search(text or "")
    return found.group(0).strip() if found else text


def contacts(raw) -> list[dict]:
    """The picker's contact rows as contacts {name, numbers: [{number (E.164, else as shown), shown}]}, in order. A
    row with no avatar (`continued`) is another number of the contact above it (LIVE); a contact with no name has
    name ""."""
    out: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        shown = _shown_number(_clean(item.get("number")))
        entry = {"number": normalize_number(shown) or shown, "shown": shown} if shown else None
        if item.get("continued") and out:
            if entry:
                out[-1]["numbers"].append(entry)
            continue
        out.append({"name": _clean(item.get("name")), "numbers": [entry] if entry else []})
    return out


def groups(raw) -> list[dict]:
    """The picker's group conversation results (Messages' own search by member) as conversation rows."""
    return [{"id": thread_id(item.get("href")), "name": _clean(item.get("name")) or "Unknown",
             "preview": _clean(item.get("preview")), "timestamp": "", "unread": False}
            for item in raw or [] if isinstance(item, dict) and thread_id(item.get("href"))]


def picker(snap: dict | None) -> dict:
    """The picker as {contacts, groups, sendTo (E.164, or the shown text, of its "Send to" number, else None),
    loading}."""
    snap = snap or {}
    send_to = _clean(snap.get("sendTo"))
    return {"contacts": contacts(snap.get("contacts")), "groups": groups(snap.get("groups")),
            "sendTo": (normalize_number(send_to) or send_to) if send_to else None, "loading": bool(snap.get("loading"))}


# --- Which conversation a query names ----------------------------------------------------------------------------

def _ambiguous(query: str, found: list[dict]) -> UsageError:
    names = ", ".join(f"{row['name']} ({row['id']})" for row in found[:8])
    return UsageError(f"{len(found)} conversations match {query!r}: {names}. Use its id.",
                      hint="See `webcli messages search QUERY` for the candidates.")


def _unique(rows: list[dict]) -> list[dict]:
    seen, out = set(), []
    for row in rows:
        key = row["id"] or id(row)
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def resolve(rows: list[dict], query: str) -> dict | None:
    """The conversation `query` names: an exact id; else the one whose name is the query (ignoring case); else the one
    whose name contains it; else, for a phone number, the one shown by that number (punctuation and a +1 aside).
    None when nothing matches; UsageError (exit 2) when several do."""
    query = query.strip()
    for row in rows:
        if row["id"] and row["id"] == query:
            return row
    low = query.casefold()
    found = [row for row in rows if row["name"].casefold() == low]
    found = found or [row for row in rows if low and low in row["name"].casefold()]
    if not found and is_number(query) or not found and 7 <= len(digits(query)) <= 15 and NUMBERISH.match(query):
        found = [row for row in rows if len(digits(row["name"])) >= 7 and same_number(row["name"], query)]
    found = _unique(found)
    if len(found) > 1:
        raise _ambiguous(query, found)
    return found[0] if found else None


def _contact_hits(people: list[dict], query: str) -> list[dict]:
    """The picker's contacts that `query` names: by number when it is one, else by name (containing it)."""
    if is_number(query):
        return [c for c in people if any(same_number(n["number"], query) for n in c["numbers"])]
    low = _fold(query)
    return [c for c in people if low and low in _fold(c["name"])]


contact_hits = _contact_hits


def _rows_for(rows: list[dict], person: dict) -> list[dict]:
    """The 1:1 conversations of a picker contact: named by its name, or shown by one of its numbers."""
    name = _fold(person["name"])
    return [row for row in rows if (name and _fold(row["name"]) == name)
            or any(same_number(row["name"], n["number"]) for n in person["numbers"] if len(digits(row["name"])) >= 7)]


def strong(rows: list[dict], query: str, people: list[dict] = ()) -> list[dict]:
    """The conversations `query` names for sure (ids, exact names, the number, or a picker contact's 1:1), best tier
    first: [] while none. What a deeper lookup waits for before it stops loading."""
    query = query.strip()
    by_id = [row for row in rows if row["id"] and row["id"] == query]
    if by_id:
        return by_id[:1]
    low = _fold(query)
    exact = _unique([row for row in rows if _fold(row["name"]) == low])
    if exact:
        return exact
    if is_number(query):
        shown = _unique([row for row in rows if len(digits(row["name"])) >= 7 and same_number(row["name"], query)])
        named = _unique([row for c in _contact_hits(people, query) for row in _rows_for(rows, c)])
        return _unique(shown + named)
    return _unique([row for c in _contact_hits(people, query) for row in _rows_for(rows, c)])


def covered(rows: list[dict], query: str, people: list[dict] = ()) -> bool:
    """A deeper lookup can stop loading: the query is a listed id or exact name, or every contact it names (by name,
    or by number) has its 1:1 listed, or (a number no contact has) a conversation shown by it is listed. A query
    naming no contact is never covered: only the whole list (or the page cap) answers it."""
    query = query.strip()
    low = _fold(query)
    if any(row["id"] == query or _fold(row["name"]) == low for row in rows):
        return True
    hits = _contact_hits(people, query)
    if hits:
        return all(_rows_for(rows, person) for person in hits)
    return is_number(query) and any(len(digits(row["name"])) >= 7 and same_number(row["name"], query) for row in rows)


def resolve_deep(rows: list[dict], query: str, people: list[dict] = (), group_rows: list[dict] = (), *,
                 strict: bool = False) -> dict | None:
    """The conversation `query` names among the deeper lookup's rows (the list after "Load more", and the picker's
    group results), with the picker's contacts: an id; else an exact name; else, for a number, the conversation shown
    by it or the 1:1 of the contact who has it; else the 1:1 of the contact whose name contains the query; else a
    conversation whose name contains it. Several in the first tier that has any: UsageError (exit 2), listing them.
    strict (send): also UsageError when the query names several different contacts, unless it is an id or a
    conversation's exact name, so a send never goes to one of two people a name fits."""
    query = query.strip()
    every = _unique([*rows, *group_rows])
    found = strong(every, query, people)
    exact = bool(found) and (found[0]["id"] == query or _fold(found[0]["name"]) == _fold(query))
    if strict and not exact and not is_number(query):
        people_hit = [c for c in _contact_hits(people, query) if c["name"]]
        names = sorted({c["name"] for c in people_hit}, key=str.casefold)
        if len(names) > 1:
            raise UsageError(f"{len(names)} contacts match {query!r}: {', '.join(names[:8])}. Use the full name, "
                             "the number, or the conversation id.",
                             hint="See `webcli messages search QUERY` for the candidates.")
    if not found:
        low = query.casefold()
        found = _unique([row for row in every if low and low in row["name"].casefold()])
    if len(found) > 1:
        raise _ambiguous(query, found)
    return found[0] if found else None


def search(rows: list[dict], query: str) -> list[dict]:
    """The rows matching `query` in the name, the number (3+ digits, or the whole number punctuation and +1 aside),
    or the latest message's preview, each with matched: the fields that matched."""
    low, wanted = query.strip().casefold(), digits(query)
    out = []
    for row in rows:
        number = (len(wanted) >= 3 and wanted in digits(row["name"])) or (
            is_number(query) and len(digits(row["name"])) >= 7 and same_number(row["name"], query))
        matched = [field for field, hit in (("name", low in row["name"].casefold()), ("number", number),
                                            ("preview", low in row["preview"].casefold())) if hit]
        if matched:
            out.append({**row, "matched": matched})
    return out


def search_deep(rows: list[dict], query: str, people: list[dict] = (), group_rows: list[dict] = ()) -> list[dict]:
    """search() over the deeper lookup: conversation rows {kind: "conversation", id, name, number, preview,
    timestamp, unread, matched} (matched may add "group": Messages' own group search found it, and "contact": the
    1:1 of a contact the query names), then one row per number of each contact the query names {kind: "contact",
    id (its 1:1 conversation when one is loaded, else null), name, number, matched: ["contact"]}."""
    every = _unique([*rows, *group_rows])
    hits: dict[str, dict] = {}
    order: list[str] = []

    def add(row: dict, field: str) -> None:
        key = row["id"] or f"#{len(order)}"
        if key not in hits:
            hits[key] = {"kind": "conversation", **row, "number": normalize_number(row["name"]) or "", "matched": []}
            order.append(key)
        if field not in hits[key]["matched"]:
            hits[key]["matched"].append(field)

    for row in search(every, query):
        for field in row["matched"]:
            add(row, field)
    group_ids = {row["id"] for row in group_rows}
    for row in every:
        if row["id"] in group_ids:
            add(row, "group")
    people_hit = _contact_hits(people, query)
    for person in people_hit:
        for row in _rows_for(every, person):
            add(row, "contact")
    out = [hits[key] for key in order]
    for person in people_hit:
        one = _rows_for(every, person)
        for number in person["numbers"] or [{"number": "", "shown": ""}]:
            mine = [row for row in one if _fold(row["name"]) == _fold(person["name"]) and person["name"]
                    or same_number(row["name"], number["number"])]
            out.append({"kind": "contact", "id": mine[0]["id"] if len(mine) == 1 else None,
                        "name": person["name"] or number["shown"], "number": number["number"], "preview": "",
                        "timestamp": "", "unread": False, "matched": ["contact"]})
    return out


# --- New conversations: recipients ------------------------------------------------------------------------------

def recipient(query: str, people: list[dict]) -> dict:
    """The one person `query` names for a new conversation: {query, name (the contact's, else ""), number (E.164)}.
    A number is itself (named when exactly one picker contact has it). A name must be exactly one picker contact
    (the one whose name is the query, ignoring case, else the only one containing it) with exactly one number.
    NotFound when none; UsageError (exit 2) when several contacts, or several numbers, fit."""
    from webcli.core.errors import NotFound

    query = query.strip()
    number = normalize_number(query)
    if number:
        named = {c["name"] for c in people if c["name"] and any(same_number(n["number"], number)
                                                                for n in c["numbers"])}
        return {"query": query, "name": next(iter(named)) if len(named) == 1 else "", "number": number}
    low = _fold(query)
    hits = [c for c in people if c["name"] and _fold(c["name"]) == low] or \
           [c for c in people if c["name"] and low and low in _fold(c["name"])]
    names = sorted({c["name"] for c in hits}, key=str.casefold)
    if not hits:
        raise NotFound(f"No contact matches {query!r} in Messages' Start chat picker.",
                       hint="Pass the phone number instead (10 digits for the US, or +country code).")
    if len(names) > 1:
        raise UsageError(f"{len(names)} contacts match {query!r}: {', '.join(names[:8])}. Use the full name or "
                         "the number.", hint="See `webcli messages search QUERY` for the candidates.")
    numbers = list(dict.fromkeys(n["number"] for c in hits for n in c["numbers"] if normalize_number(n["number"])))
    if not numbers:
        raise NotFound(f"The contact {names[0]!r} has no phone number Messages can text.",
                       hint="Pass the phone number instead.")
    if len(numbers) > 1:
        raise UsageError(f"The contact {names[0]!r} has {len(numbers)} numbers: {', '.join(numbers)}. Pass the "
                         "one to text.", hint="--to with the number, e.g. --to +12025550100.")
    return {"query": query, "name": names[0], "number": numbers[0]}


def pick(snap: dict, person: dict) -> tuple[str, int] | None:
    """What to click in the Start chat picker (its raw snapshot) for exactly `person`: ("row", i), a contact row
    with its number (the row of its contact by name first), else ("sendTo", 0), the "Send to" button when it shows
    its number; None when the picker offers neither."""
    rows, name = [], ""
    for item in snap.get("contacts") or []:
        if not item.get("continued") or not rows:
            name = _clean(item.get("name"))
        rows.append((name, normalize_number(_shown_number(_clean(item.get("number")))) or ""))
    found = [i for i, (_, number) in enumerate(rows) if number and number == person["number"]]
    if person.get("name"):
        found = [i for i in found if _fold(rows[i][0]) == _fold(person["name"])] or found
    if found:
        return ("row", found[0])
    send_to = _clean(snap.get("sendTo"))
    if send_to and normalize_number(_shown_number(send_to)) == person["number"]:
        return ("sendTo", 0)
    return None


def label(person: dict) -> str:
    """How the page shows a recipient: the contact's name, else the number."""
    return person.get("name") or person["number"]


def names_one(shown: str, person: dict) -> bool:
    """`shown` (a chip, a header part) is exactly this recipient: its name (case and spacing aside) or its number."""
    if person.get("name") and _fold(shown) == _fold(person["name"]):
        return True
    return bool(normalize_number(shown)) and same_number(shown, person["number"])


def same_set(shown: list[str], people: list[dict]) -> bool:
    """`shown` names exactly `people`, each once: as many entries, and a one-to-one match (names_one)."""
    if len(shown) != len(people):
        return False
    left = list(people)
    for item in shown:
        match = next((p for p in left if names_one(item, p)), None)
        if match is None:
            return False
        left.remove(match)
    return not left


def header_parts(header: str | None) -> list[str]:
    """A conversation title's members: a group's are joined by ", " (LIVE); a 1:1's title is one name or number."""
    return [part.strip() for part in (header or "").split(",") if part.strip()]


def expect_matches(expect: list[str], people: list[dict]) -> bool:
    """--expect-recipient values name exactly `people`: each value a recipient's name (case and spacing aside) or
    number (punctuation and +1 aside), one value per recipient."""
    return same_set(list(expect), people)


def conversation_people(row: dict) -> list[dict]:
    """An existing conversation's members as recipients, from its name (a group's names joined by ", ")."""
    return [{"name": "" if normalize_number(part) else part, "number": normalize_number(part) or ""}
            for part in header_parts(row["name"])]


def participants(raw) -> list[dict]:
    """The people the open conversation's Details dialog lists, as recipients {name, number (E.164, else as shown)}.
    A person who isn't a contact shows only the number, in the name's place (LIVE): name "" then."""
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        name, shown = _clean(item.get("name")), _shown_number(_clean(item.get("number")))
        if not shown and normalize_number(name):
            name, shown = "", name
        out.append({"name": name, "number": normalize_number(shown) or shown})
    return out


def numbers_match(members: list[dict], people: list[dict]) -> bool:
    """The conversation's people (participants()) are exactly `people` by number: as many, each number once. A
    member without a number Messages can text never matches."""
    if len(members) != len(people) or not all(normalize_number(m["number"]) for m in members):
        return False
    left = list(people)
    for member in members:
        match = next((p for p in left if p.get("number") and same_number(member["number"], p["number"])), None)
        if match is None:
            return False
        left.remove(match)
    return not left


def uses_number(members: list[dict], number: str) -> bool:
    """A 1:1 conversation's one person has this number."""
    return len(members) == 1 and bool(normalize_number(members[0]["number"])) and same_number(members[0]["number"],
                                                                                            number)


def with_numbers(results: list[dict], numbers: dict[str, list[str]]) -> list[dict]:
    """search_deep's rows with the numbers each conversation uses ({conversation id: its people's numbers}, from
    their Details): a conversation row gains numbers (null when unread), and number when it is a 1:1; a contact
    row's id becomes the 1:1 that uses its number, when one was read."""
    one = {cid: nums[0] for cid, nums in numbers.items() if len(nums) == 1}
    out = []
    for row in results:
        row = dict(row)
        if row["kind"] == "conversation":
            row["numbers"] = numbers.get(row["id"]) if row["id"] else None
            if row["id"] in one:
                row["number"] = one[row["id"]]
        elif row["number"]:
            mine = [cid for cid, number in one.items() if same_number(number, row["number"])]
            if len(mine) == 1:
                row["id"] = mine[0]
            elif row["id"] in one and not same_number(one[row["id"]], row["number"]):
                row["id"] = None  # the 1:1 by its name uses another number
        out.append(row)
    return out


def message_text(text: str | None) -> str:
    """A message's text: trimmed, and not empty."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Message text is empty.")
    return text


def names_match(header: str, name: str) -> bool:
    """The open conversation's title names `name` (whitespace and case aside)."""
    return " ".join(name.split()).casefold() in " ".join(header.split()).casefold()
