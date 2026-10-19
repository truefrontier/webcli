"""Export writers for `webcli contacts export`: JSON, CSV, and vCard 3.0 (RFC 2426) from parse.contact() dicts, as
gcontacts wrote them. No browser code; the CLI writes the file (output.write_atomic: mode 0600, never through a
symlink)."""

from __future__ import annotations

import csv
import io
import json
import re

FORMATS = ("json", "csv", "vcf")
CSV_FIELDS = ("id", "name", "givenName", "familyName", "emails", "phones", "organization", "title", "addresses",
              "birthday", "labels", "urls", "notes", "source")
JOIN = "; "  # several values in one CSV cell
_PHONE = re.compile(r"\+?[\d\s().\-/]+")

# Google's type names -> vCard 3.0 TYPE parameters.
TEL_TYPES = {"mobile": "CELL", "home": "HOME", "work": "WORK", "homeFax": "HOME,FAX", "workFax": "WORK,FAX",
             "otherFax": "FAX", "pager": "PAGER", "workMobile": "WORK,CELL", "workPager": "WORK,PAGER"}
EMAIL_TYPES = {"home": "INTERNET,HOME", "work": "INTERNET,WORK"}
ADR_TYPES = {"home": "HOME", "work": "WORK"}


def to_json(contacts: list[dict]) -> str:
    return json.dumps(contacts, indent=2, ensure_ascii=False) + "\n"


def _cell(value: str | None) -> str:
    """A CSV cell that a spreadsheet won't run as a formula. Phone numbers (+1 555-0100) stay as they are."""
    text = value or ""
    phones = all(_PHONE.fullmatch(part) for part in text.split(JOIN))  # one number, or several joined
    if text[:1] in ("=", "@", "\t", "\r") or (text[:1] in ("+", "-") and not phones):
        return "'" + text
    return text


def csv_row(c: dict) -> dict:
    name = c["names"][0] if c["names"] else {}
    org = c["organizations"][0] if c["organizations"] else {}
    row = {
        "id": c["id"], "name": c["name"], "givenName": name.get("givenName"), "familyName": name.get("familyName"),
        "emails": JOIN.join(e["value"] for e in c["emails"]),
        "phones": JOIN.join(p["value"] for p in c["phones"]),
        "organization": org.get("company"), "title": org.get("title"),
        "addresses": JOIN.join(a.get("formatted") or "" for a in c["addresses"] if a.get("formatted")),
        "birthday": c["birthdays"][0]["date"] if c["birthdays"] else None,
        "labels": JOIN.join(c["labels"]), "urls": JOIN.join(u["value"] for u in c["urls"]),
        "notes": c["biography"], "source": c["source"],
    }
    return {k: _cell(v) for k, v in row.items()}


def to_csv(contacts: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(csv_row(c) for c in contacts)
    return out.getvalue()


def escape(value: str) -> str:
    """vCard 3.0 TEXT escaping: backslash, comma, semicolon, and newlines."""
    return (value.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;")
            .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n"))


def fold(line: str, limit: int = 75) -> str:
    """Fold a content line at `limit` octets of UTF-8 (CRLF + space), never inside a character."""
    parts, current, size = [], "", 0
    for ch in line:
        width = len(ch.encode("utf-8"))
        room = limit if not parts else limit - 1  # continuation lines start with a space
        if size + width > room:
            parts.append(current)
            current, size = "", 0
        current += ch
        size += width
    parts.append(current)
    return "\r\n ".join(parts)


def _prop(name: str, value: str, types: str | None = None) -> str:
    return fold(f"{name}{';TYPE=' + types if types else ''}:{value}")


def vcard(c: dict) -> str:
    n = c["names"][0] if c["names"] else {}
    lines = ["BEGIN:VCARD", "VERSION:3.0", _prop("FN", escape(c["name"] or "")),
             _prop("N", ";".join(escape(n.get(k) or "") for k in
                                 ("familyName", "givenName", "middleName", "honorificPrefix", "honorificSuffix")))]
    if c["nicknames"]:
        lines.append(_prop("NICKNAME", ",".join(escape(x) for x in c["nicknames"])))
    if c["organizations"]:
        org = c["organizations"][0]
        if org.get("company"):
            lines.append(_prop("ORG", escape(org["company"])))
        if org.get("title"):
            lines.append(_prop("TITLE", escape(org["title"])))
    for e in c["emails"]:
        lines.append(_prop("EMAIL", escape(e["value"]), EMAIL_TYPES.get(e.get("type", ""), "INTERNET")))
    for p in c["phones"]:
        lines.append(_prop("TEL", escape(p["value"]), TEL_TYPES.get(p.get("type", ""), "VOICE")))
    for a in c["addresses"]:
        parts = ["", "", a.get("street"), a.get("city"), a.get("region"), a.get("postalCode"), a.get("country")]
        if not any(parts) and a.get("formatted"):
            parts[2] = a["formatted"]  # unstructured: the whole address as the street
        lines.append(_prop("ADR", ";".join(escape(x or "") for x in parts), ADR_TYPES.get(a.get("type", ""))))
    if c["birthdays"]:
        lines.append(_prop("BDAY", c["birthdays"][0]["date"]))
    for u in c["urls"]:
        lines.append(_prop("URL", escape(u["value"])))
    if c["biography"]:
        lines.append(_prop("NOTE", escape(c["biography"])))
    if c["labels"]:
        lines.append(_prop("CATEGORIES", ",".join(escape(x) for x in c["labels"])))
    if c["resourceName"]:
        lines.append(_prop("UID", escape(c["resourceName"])))
    lines.append("END:VCARD")
    return "\r\n".join(lines) + "\r\n"


def to_vcf(contacts: list[dict]) -> str:
    return "".join(vcard(c) for c in contacts)


def render(contacts: list[dict], fmt: str) -> str:
    return {"json": to_json, "csv": to_csv, "vcf": to_vcf}[fmt](contacts)

