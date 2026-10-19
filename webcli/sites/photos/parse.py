"""Google Photos ids, page labels, and download names. Pure: no browser code."""

from __future__ import annotations

import re

ALBUM_RE = re.compile(r"/album/([^/?#]+)")
PHOTO_RE = re.compile(r"/photo/([^/?#]+)")
MEDIA_KEY = "AF1Qip"  # every album and media key starts so
TITLE_JUNK = re.compile(r"\d+\s*items?\s*More options$", re.I)
FILENAME = re.compile(r'filename="?([^";]+)"?')
EXTENSIONS = (("mp4", "mp4"), ("video/", "mp4"), ("jpeg", "jpg"), ("jpg", "jpg"), ("png", "png"))


def _key(value: str, pattern: re.Pattern, what: str) -> str:
    value = value.strip()
    match = pattern.search(value)
    if match:
        return match.group(1)
    if value.startswith(MEDIA_KEY):
        return value
    raise ValueError(f"expected a{'n' if what[0] in 'aeiou' else ''} {what} id or URL")


def album_id(value: str) -> str:
    return _key(value, ALBUM_RE, "album")


def photo_id(value: str) -> str:
    return _key(value, PHOTO_RE, "photo")


def clean_title(text: str | None) -> str:
    """An album tile's label without its "N items More options" tail."""
    return re.sub(r"\s+", " ", TITLE_JUNK.sub("", (text or "").strip()).strip())


def described(label: str) -> dict:
    """{kind, taken_at} from a label like "Photo - Portrait - Nov 10, 2016, 11:22:34 AM"."""
    parts = [p.strip() for p in (label or "").split(" - ")]
    return {"kind": parts[0], "taken_at": " - ".join(parts[2:]) if len(parts) >= 3 else ""}


def photo(item: dict) -> dict:
    label = item.get("label") or ""
    return {"id": item["id"], **described(label), "label": label, "url": item["url"]}


def download_name(disposition: str | None, content_type: str | None, media_id: str) -> str:
    """The server's file name (Content-Disposition), else <id>.<extension for the content type>. Not yet safe to
    use as a path: google.safe_name makes it one."""
    match = FILENAME.search(disposition or "")
    if match and match.group(1).strip():
        return match.group(1).strip()
    kind = (content_type or "").lower()
    return f"{media_id}.{next((ext for marker, ext in EXTENSIONS if marker in kind), 'bin')}"
