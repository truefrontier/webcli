"""Pure parsers for webcli youtube's feed reads: subs (browse FEchannels), uploads (FEsubscriptions), and home
(FEwhat_to_watch). Rows share the newsfeed row shape: source, id, url, title, author, publishedAt, snippet,
thumbnail, metrics.

Signed in only (auth "signed_in"): subscriptions and their uploads have no public form, and webcli doesn't read
YouTube's signed-out home, so a signed-out profile exits 4 (`webcli feed` records needs_login).

Shorts are dropped unless asked for: a shelf of them (reelShelfRenderer, a rich shelf titled "Shorts" or holding
Shorts lockups) is skipped whole, and so is any Short item or /shorts/ URL that is left.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from webcli.sites.youtube.parse import YT, _ROOTS, _dig, _find, text_of, video_row

_SHORT_KEYS = ("shortsLockupViewModel", "reelItemRenderer")
_ITEM_KEYS = ("videoRenderer", "lockupViewModel", *_SHORT_KEYS)
_SKIP = {"secondaryContents", "header", "frameworkUpdates", "responseContext", "menu", "topbar"}
_AGE = re.compile(r"(\d+)\s*(s|sec|second|m|min|minute|h|hr|hour|d|day|w|wk|week|mo|month|y|yr|year)s?\b", re.I)
_UNITS = {"s": 1, "sec": 1, "second": 1, "m": 60, "min": 60, "minute": 60, "h": 3600, "hr": 3600, "hour": 3600,
          "d": 86400, "day": 86400, "w": 604800, "wk": 604800, "week": 604800, "mo": 2592000, "month": 2592000,
          "y": 31536000, "yr": 31536000, "year": 31536000}
_COUNT = re.compile(r"^([\d.,]+)\s*([KMB])?(?:\s+(?:views?|watching|subscribers?))?$", re.I)
_SINCE = re.compile(r"^(\d+)\s*([hdw])$", re.I)


def count(text: str | None) -> int | None:
    """'9.3M' / '1,234 views' / '642K subscribers' -> int. None when it isn't a count."""
    m = _COUNT.match((text or "").strip())
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return int(round(value * {"K": 1e3, "M": 1e6, "B": 1e9}.get((m.group(2) or "").upper(), 1)))


def age_seconds(text: str | None) -> int | None:
    """'2d ago' / '3 hours ago' / 'Streamed 1 week ago' -> seconds. None for anything else ('LIVE', a date)."""
    text = (text or "").strip()
    if "ago" not in text.lower():
        return None
    m = _AGE.search(text)
    return int(m.group(1)) * _UNITS[m.group(2).lower()] if m else None


def parse_since(value: str | None, now: datetime | None = None) -> datetime | None:
    """--since: 48h / 3d / 1w, or an ISO date/time (UTC when no offset). None for None; ValueError when bad."""
    if not value:
        return None
    now = now or datetime.now(timezone.utc)
    m = _SINCE.match(value.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        return now - timedelta(hours=n if unit == "h" else 0, days=n if unit == "d" else 7 * n if unit == "w" else 0)
    try:
        at = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"--since {value!r}: use 48h, 3d, 1w, or an ISO date.") from None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _is_shorts_shelf(key: str, node: Any) -> bool:
    if key == "reelShelfRenderer":
        return True
    if key in ("richShelfRenderer", "shelfRenderer", "gridShelfViewModel") and isinstance(node, dict):
        title = (text_of(node.get("title")) or "").strip().lower()
        return title == "shorts" or bool(node.get("isShorts")) or any(
            next(_find(node.get("contents"), k), None) is not None for k in _SHORT_KEYS)
    return False


def _titled(key: str, node: Any, titles: tuple[str, ...]) -> bool:
    return bool(titles) and key in ("richShelfRenderer", "shelfRenderer") and isinstance(node, dict) and (
        (text_of(node.get("title")) or "").strip().lower() in titles)


def items(resp: dict, *, include_shorts: bool = False,
          skip_shelves: tuple[str, ...] = ()) -> Iterator[tuple[str, dict]]:
    """(renderer key, renderer) for each video item, in page order, Shorts shelves skipped unless include_shorts,
    and shelves titled one of skip_shelves (lowercase) skipped always."""
    def walk(node: Any) -> Iterator[tuple[str, dict]]:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _SKIP:
                    continue
                if key in _ITEM_KEYS and isinstance(value, dict):
                    if include_shorts or key not in _SHORT_KEYS:
                        yield key, value
                    continue
                if (not include_shorts and _is_shorts_shelf(key, value)) or _titled(key, value, skip_shelves):
                    continue
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    yield from walk([resp.get(k) for k in _ROOTS])


def _lockup_parts(renderer: dict) -> list[str]:
    meta = _dig(renderer, "metadata", "lockupMetadataViewModel") or {}
    rows = _dig(meta, "metadata", "contentMetadataViewModel", "metadataRows") or []
    return [t for row in rows for p in (row.get("metadataParts") or []) if (t := text_of(p.get("text")))]


def _thumb(renderer: dict) -> str | None:
    sources = next(_find(renderer.get("contentImage") or renderer.get("thumbnail"), "sources"), None) \
        or _dig(renderer, "thumbnail", "thumbnails")
    if isinstance(sources, list) and sources and isinstance(sources[-1], dict) and sources[-1].get("url"):
        url = str(sources[-1]["url"])
        return "https:" + url if url.startswith("//") else url
    return None


def video(key: str, renderer: dict, now: datetime, *, include_shorts: bool = False) -> dict | None:
    """One video item as a row; None for a non-video, or a Short when Shorts are excluded."""
    base = video_row(key, renderer)
    if not base or (not include_shorts and (base["isShort"] or "/shorts/" in base["url"])):
        return None
    if key == "videoRenderer":
        age_text = text_of(renderer.get("publishedTimeText"))
        views = count(text_of(renderer.get("viewCountText")) or text_of(renderer.get("shortViewCountText")))
        snippet = text_of(renderer.get("descriptionSnippet")) or text_of(
            _dig(renderer, "detailedMetadataSnippets", "0", "snippetText"))
    else:
        parts = _lockup_parts(renderer) if key == "lockupViewModel" else []
        age_text = next((p for p in parts if age_seconds(p) is not None), None)
        views = next((c for p in parts if p != base["channel"] and (c := count(p)) is not None), None)
        snippet = None
    age = age_seconds(age_text)
    published = (now - timedelta(seconds=age)).isoformat(timespec="seconds").replace("+00:00", "Z") \
        if age is not None else None
    return {"source": "youtube", "id": base["videoId"], "url": base["url"], "title": base["title"],
            "author": base["channel"], "channelUrl": base["channelUrl"], "publishedAt": published,
            "publishedText": age_text, "snippet": snippet, "thumbnail": _thumb(renderer),
            "metrics": {"views": views, "durationSec": base["durationSec"]}, "isShort": base["isShort"],
            "auth": "signed_in"}


# FEsubscriptions opens with a "Most relevant" shelf ahead of its newest-first grid; uploads skips it (its videos are
# in the grid too) so the rows stay newest first and --since can stop at the first older one.
UPLOADS_SKIP = ("most relevant",)


def videos(resp: dict, now: datetime, *, include_shorts: bool = False,
           skip_shelves: tuple[str, ...] = ()) -> list[dict]:
    return [r for key, value in items(resp, include_shorts=include_shorts, skip_shelves=skip_shelves)
            if (r := video(key, value, now, include_shorts=include_shorts))]


def channel(renderer: dict) -> dict | None:
    """One subscription (channelRenderer). YouTube puts the @handle in subscriberCountText and the subscriber count in
    videoCountText on this page, so both are recognised by what they hold."""
    cid = renderer.get("channelId")
    if not cid:
        return None
    texts = [text_of(renderer.get(k)) for k in ("subscriberCountText", "videoCountText")]
    handle = next((t for t in texts if t and t.startswith("@")), None)
    subs = next((c for t in texts if t and "subscriber" in t.lower() and (c := count(t)) is not None), None)
    base = _dig(renderer, "navigationEndpoint", "browseEndpoint", "canonicalBaseUrl")
    return {"source": "youtube", "id": cid, "url": YT + base if base else f"{YT}/channel/{cid}",
            "title": text_of(renderer.get("title")), "author": handle, "publishedAt": None,
            "snippet": text_of(renderer.get("descriptionSnippet")), "thumbnail": _thumb(renderer),
            "metrics": {"subscribers": subs}, "auth": "signed_in"}


def channels(resp: dict) -> list[dict]:
    out, seen = [], set()
    for renderer in _find([resp.get(k) for k in _ROOTS], "channelRenderer"):
        row = channel(renderer) if isinstance(renderer, dict) else None
        if row and row["id"] not in seen:
            seen.add(row["id"])
            out.append(row)
    return out
