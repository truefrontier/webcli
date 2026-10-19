"""Pure parsers for contacts.google.com's wire format and webcli contacts' arguments. No browser code.

batchexecute framing (freq_entries, envelopes, at) is gemini's: the same Google boq format.

A contact ("row") is a Person message in JSPB form: a list where index i holds field i + 1, and a trailing dict holds
high field numbers ({"104": ...}). Each field is a list of items, and each item is [field_metadata, value, ...];
field_metadata[0] is true on the primary item. Field numbers and item positions were mapped from live answers by
gcontacts on 2026-10-18.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from webcli.sites.contacts.endpoints import HIDDEN_GROUPS, SYSTEM_GROUP_NAMES
from webcli.sites.gemini.parse import at, envelopes

# gRPC status codes Google puts in an envelope's [5][0].
AUTH_CODES = {7, 16}  # PERMISSION_DENIED, UNAUTHENTICATED
RATE_CODES = {8}  # RESOURCE_EXHAUSTED

# Person field numbers.
F_ID, F_META, F_NAMES, F_PHOTOS = 1, 2, 3, 4
F_BIO, F_URLS, F_BIRTHDAYS, F_EMAILS, F_NICKNAMES, F_PHONES, F_ORGS = 6, 7, 8, 10, 11, 12, 13
F_ADDRESSES, F_RELATIONS, F_EVENTS = 15, 17, 19
F_SORT, F_ETAG, F_MEMBERSHIPS = 25, 26, 104
# Fields every row carries, real or not. A row with nothing else is an empty shell (what an unknown id answers).
BOOKKEEPING = {F_ID, F_META, F_PHOTOS, F_SORT, F_ETAG, F_MEMBERSHIPS, 48, 139, 140}

_CONTACT_ID = re.compile(r"(?:people/)?(c\d+)")
_CONTACT_URL = re.compile(r"^https?://contacts\.google\.com/(?:u/\d+/)?person/(c\d+)")
_PHONE = re.compile(r"\+?[\d\s().\-/]{5,}")


class RpcError(RuntimeError):
    def __init__(self, rpcid: str, code: int | None, message: str) -> None:
        self.rpcid, self.code = rpcid, code
        super().__init__(message)


def rpc_payload(body: str, rpcid: str) -> Any:
    """The decoded payload of rpcid's answer in a batchexecute body. RpcError when Google answered with an error code
    or didn't answer rpcid at all (a sign-in page or an empty body included)."""
    for env in envelopes(body):
        if env[0] == "er":
            raise RpcError(rpcid, None, f"Google rejected the {rpcid} request (er).")
        if env[0] != "wrb.fr" or at(env, 1) != rpcid:
            continue
        payload = at(env, 2)
        if isinstance(payload, str):
            return json.loads(payload)
        code = at(env, 5, 0)
        code = code if isinstance(code, int) else None
        raise RpcError(rpcid, code, f"Google answered {rpcid} with error code {code}.")
    raise RpcError(rpcid, None, f"Google's answer had no {rpcid} result.")


# Pages.

def _page(payload: Any, token_at: int) -> tuple[list[list], str | None]:
    if not isinstance(payload, list):
        raise ValueError("not a list page")
    rows = payload[0] if payload and isinstance(payload[0], list) else []
    token = at(payload, token_at)
    return [r for r in rows if isinstance(r, list) and r], token if isinstance(token, str) and token else None


def contacts_page(payload: Any) -> tuple[list[list], str | None]:
    """YQqBqc: (rows, next page token or None)."""
    return _page(payload, 2)


def other_page(payload: Any) -> tuple[list[list], str | None]:
    """tAERjb: (rows, next page token or None)."""
    return _page(payload, 1)


def search_results(payload: Any) -> tuple[list[list], list[list]]:
    """fcwrWc: (My Contacts rows, Other contacts rows)."""
    if not isinstance(payload, list):
        raise ValueError("not a search answer")
    return tuple([r for r in (at(payload, i) or []) if isinstance(r, list) and r] for i in (0, 1))


def groups(payload: Any) -> list[dict]:
    """GKlgB: [{id, name, count, system}] in Google's order (your labels first, then system groups)."""
    out = []
    for g in at(payload, 0) or []:
        if isinstance(g, list) and isinstance(at(g, 0), str):
            count = at(g, 2)
            out.append({"id": g[0], "name": at(g, 1) or g[0], "count": count if isinstance(count, int) else None,
                        "system": bool(at(g, 3))})
    return out


def group_names(group_list: list[dict]) -> dict[str, str]:
    return {**SYSTEM_GROUP_NAMES, **{g["id"]: g["name"] for g in group_list}}


# A contact.

def fields(row: list) -> dict[int, Any]:
    """{field number: value} for a JSPB row: index i is field i + 1; a trailing dict holds the rest."""
    out: dict[int, Any] = {}
    for i, value in enumerate(row):
        if isinstance(value, dict) and i == len(row) - 1:
            out.update({int(k): v for k, v in value.items() if str(k).isdigit() and v is not None})
        elif value is not None:
            out[i + 1] = value
    return out


def _items(f: dict, num: int) -> list[list]:
    """A repeated field's items, primary first (metadata [0] is true on the primary one)."""
    items = [x for x in f.get(num) or [] if isinstance(x, list) and len(x) > 1]
    return sorted(items, key=lambda x: at(x, 0, 0) is not True)


def _text(item: list, i: int) -> str | None:
    value = at(item, i)
    return value.strip() or None if isinstance(value, str) else None


def _compact(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [])}


def _dedupe(items: list[dict], key) -> list[dict]:
    seen, out = set(), []
    for item in items:
        k = key(item)
        if k and k not in seen:
            seen.add(k)
            out.append(item)
    return out


def _json_key(d: dict) -> str | None:
    return json.dumps(d, sort_keys=True) if d else None


def _date(parts: Any) -> str | None:
    """[year|null, month, day] -> YYYY-MM-DD, or --MM-DD without a year (vCard's form)."""
    if not isinstance(parts, list) or len(parts) < 3:
        return None
    year, month, day = parts[:3]
    if not (isinstance(month, int) and isinstance(day, int)):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}" if isinstance(year, int) and year > 0 else f"--{month:02d}-{day:02d}"


def digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def contact(row: list, names: dict[str, str] | None = None, *, source: str | None = None) -> dict:
    """One Person row as a plain dict. `names` maps label ids to names (group_names)."""
    names = names or {}
    f = fields(row)
    cid = f.get(F_ID) if isinstance(f.get(F_ID), str) else None
    meta = f.get(F_META) if isinstance(f.get(F_META), list) else []

    person_names = _dedupe([_compact({
        "displayName": _text(n, 1), "givenName": _text(n, 3), "familyName": _text(n, 4), "middleName": _text(n, 5),
        "honorificPrefix": _text(n, 6), "honorificSuffix": _text(n, 7),  # read as prefix / suffix by their shape
    }) for n in _items(f, F_NAMES)], _json_key)
    emails = _dedupe([_compact({"value": _text(e, 1), "type": _text(e, 2) or _text(e, 3)})
                      for e in _items(f, F_EMAILS)], lambda e: (e.get("value") or "").lower())
    phones = _dedupe([_compact({"value": _text(p, 1), "type": _text(p, 2) or _text(p, 4), "canonical": _text(p, 3)})
                      for p in _items(f, F_PHONES)], lambda p: p.get("canonical") or digits(p.get("value")))
    orgs = _dedupe([_compact({"company": _text(o, 1), "title": _text(o, 3),
                              "type": None if _text(o, 15) == "unknown" else _text(o, 15)})
                    for o in _items(f, F_ORGS)], _json_key)
    addresses = _dedupe([_compact({
        "formatted": _text(a, 2), "type": _text(a, 1) or _text(a, 10), "street": _text(a, 4), "city": _text(a, 5),
        "region": _text(a, 6), "postalCode": _text(a, 7), "country": _text(a, 8), "countryCode": _text(a, 9),
    }) for a in _items(f, F_ADDRESSES)], _json_key)
    birthdays = _dedupe([_compact({"date": _date(at(b, 4))}) for b in _items(f, F_BIRTHDAYS)],
                        lambda b: b.get("date"))
    events = _dedupe([_compact({"type": _text(e, 2) or _text(e, 3), "date": _date(at(e, 4))})
                      for e in _items(f, F_EVENTS)], lambda e: _json_key(e) if e.get("date") else None)
    relations = _dedupe([_compact({"person": _text(r, 2), "type": _text(r, 1) or _text(r, 3)})
                         for r in _items(f, F_RELATIONS)], _json_key)
    urls = _dedupe([_compact({"value": _text(u, 1), "type": _text(u, 2) or _text(u, 3)})
                    for u in _items(f, F_URLS)], lambda u: u.get("value"))
    nicknames = list(dict.fromkeys(filter(None, (_text(n, 1) for n in _items(f, F_NICKNAMES)))))
    bios = [b for b in (_text(x, 1) for x in _items(f, F_BIO)) if b]

    member_ids = [at(m, 1) for m in f.get(F_MEMBERSHIPS) or [] if isinstance(at(m, 1), str)]
    member_ids += [g for g in at(meta, 8) or [] if isinstance(g, str)]
    member_ids = list(dict.fromkeys(member_ids))
    label_ids = [g for g in member_ids if g not in HIDDEN_GROUPS]

    display = (next((n["displayName"] for n in person_names if n.get("displayName")), None)
               or (at(meta, 23, 0) if isinstance(at(meta, 23, 0), str) else None)  # the server's display name
               or next((o["company"] for o in orgs if o.get("company")), None))
    return {
        "id": cid,
        "resourceName": f"people/{cid}" if cid else None,
        "source": source or ("contacts" if "myContacts" in member_ids else "other"),
        "name": display or (emails[0]["value"] if emails else None) or (phones[0]["value"] if phones else None),
        "names": [n for n in person_names if n],
        "nicknames": nicknames,
        "emails": [e for e in emails if e.get("value")],
        "phones": [p for p in phones if p.get("value")],
        "organizations": [o for o in orgs if o.get("company") or o.get("title")],
        "addresses": [a for a in addresses if a],
        "birthdays": [b for b in birthdays if b],
        "events": [e for e in events if e.get("date")],
        "relations": [r for r in relations if r.get("person")],
        "urls": [u for u in urls if u.get("value")],
        "biography": "\n\n".join(bios) or None,
        "labels": [names.get(g, g) for g in label_ids],
        "labelIds": label_ids,
        "updated": _iso_ms(at(meta, 15)),
    }


def _iso_ms(value: Any) -> str | None:
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ms > 0 else None


def is_empty(row: list) -> bool:
    """True for a row that has only bookkeeping fields: what Google answers for an id it has no data for."""
    return not (set(fields(row)) - BOOKKEEPING)


def row(c: dict) -> dict:
    """The one-line summary `list`, `search`, and `other` print."""
    org = c["organizations"][0] if c["organizations"] else {}
    return {"id": c["id"], "name": c["name"], "email": c["emails"][0]["value"] if c["emails"] else None,
            "phone": c["phones"][0]["value"] if c["phones"] else None, "org": org.get("company") or org.get("title"),
            "labels": c["labels"], "source": c["source"]}


# Arguments.

def lookup_key(value: str) -> tuple[str, str]:
    """('id', 'c123') | ('email', 'a@example.com') | ('phone', '+1 555-0100'). ValueError otherwise.

    Ids are what `list` prints (c<digits>), people/c<digits>, or a contacts.google.com/person/ URL."""
    text = value.strip()
    if match := _CONTACT_URL.match(text):
        return "id", match.group(1)
    if match := _CONTACT_ID.fullmatch(text):
        return "id", match.group(1)
    if "@" in text and " " not in text:
        return "email", text
    if _PHONE.fullmatch(text) and len(digits(text)) >= 5:
        return "phone", text
    raise ValueError(f"{value!r} is not a contact id (c123...), an email address, or a phone number.")


def same_phone(a: str | None, b: str | None) -> bool:
    """Same number, ignoring punctuation and a leading US/Canada country code."""
    da, db = digits(a), digits(b)
    strip = lambda d: d[1:] if len(d) == 11 and d.startswith("1") else d  # noqa: E731
    return bool(da) and strip(da) == strip(db)


def matches(c: dict, kind: str, value: str) -> bool:
    if kind == "email":
        return any(e["value"].lower() == value.strip().lower() for e in c["emails"])
    if kind == "phone":
        return any(same_phone(value, p.get("canonical")) or same_phone(value, p["value"]) for p in c["phones"])
    return c["id"] == value


# Label writes.

def label_key(name: str) -> str:
    """A label name for matching: whitespace trimmed and collapsed, case folded ("Business  Pro " -> "business pro")."""
    return " ".join(name.split()).casefold()


def clean_label_name(name: str) -> str:
    """A label name as `label add --create-missing` creates it: whitespace trimmed and collapsed, case kept."""
    return " ".join(name.split())


def contact_key(contact_id: str) -> str:
    """The key ls96Jc takes for a contact: its id's number in lowercase hex (c123 -> "7b")."""
    match = _CONTACT_ID.fullmatch(contact_id)
    if not match:
        raise ValueError(f"{contact_id!r} is not a contact id")
    return format(int(match.group(1)[1:]), "x")


def membership_payload(contact_ids: list[str], label_id: str, action: str) -> list:
    """ls96Jc's payload: add every contact to `label_id`, or remove every one from it."""
    if action not in ("add", "remove") or not contact_ids:
        raise ValueError("membership_payload needs add|remove and at least one contact")
    group = [label_id]
    return [[contact_key(c) for c in contact_ids], None, group if action == "add" else None,
            group if action == "remove" else None, None, None, 1, 0]


def create_label_payload(name: str) -> list:
    """ouU0xd's payload: a new, empty label."""
    return [name, None, None, None, 1, 0]
