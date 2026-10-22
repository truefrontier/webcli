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
    """What the app shows: "unpaired" (the QR pairing page), "use_here" (another session holds Messages), "paired"
    (the conversation list, its rows in or its spinner gone), or "loading"."""
    if (snap.get("path") or "").startswith(AUTH_PATH) or snap.get("qr"):
        return "unpaired"
    if snap.get("useHere"):
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


def resolve(rows: list[dict], query: str) -> dict | None:
    """The conversation `query` names: an exact id; else the one whose name is the query (ignoring case); else the one
    whose name contains it; else, for 7 to 15 digits, the one whose name is that number (formatting and a country
    code aside, gmsg's match). None when nothing matches; UsageError (exit 2) when several do."""
    query = query.strip()
    for row in rows:
        if row["id"] and row["id"] == query:
            return row
    low = query.casefold()
    found = [row for row in rows if row["name"].casefold() == low]
    found = found or [row for row in rows if low and low in row["name"].casefold()]
    wanted = digits(query)
    if not found and 7 <= len(wanted) <= 15:
        found = [row for row in rows
                 if len(have := digits(row["name"])) >= 7 and (wanted in have or have in wanted)]
    if len(found) > 1:
        names = ", ".join(f"{row['name']} ({row['id']})" for row in found[:5])
        raise UsageError(f"{len(found)} conversations match {query!r}: {names}. Use its id.",
                         hint="See `webcli messages list --compact`.")
    return found[0] if found else None


def search(rows: list[dict], query: str) -> list[dict]:
    """The rows matching `query` in the name, the number (3+ digits), or the latest message's preview, each with
    matched: the fields that matched."""
    low, wanted = query.strip().casefold(), digits(query)
    out = []
    for row in rows:
        matched = [field for field, hit in (("name", low in row["name"].casefold()),
                                            ("number", len(wanted) >= 3 and wanted in digits(row["name"])),
                                            ("preview", low in row["preview"].casefold())) if hit]
        if matched:
            out.append({**row, "matched": matched})
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
