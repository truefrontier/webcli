"""Pure parsers for youtubei responses (FEhistory browse, account_menu, a playlist's VL browse), --since, and video
ids, from ytcli. No browser code."""

from __future__ import annotations

import base64
import re
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, Iterator
from urllib.parse import parse_qs, unquote, urlsplit

YT = "https://www.youtube.com"
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_WEEKDAYS = {d: i for i, d in enumerate(("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"))}
_MONTH_DAY = re.compile(r"([a-z]{3,})\.?\s+(\d{1,2})(?:,?\s+(\d{4}))?")  # Oct 3 / October 3, 2025
_DAY_MONTH = re.compile(r"(\d{1,2})\s+([a-z]{3,})\.?(?:,?\s+(\d{4}))?")  # 3 Oct 2025
_CLOCK = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})")
_SKIP = {"secondaryContents"}  # sidebar: search box and the clear/pause buttons we never touch
_ROOTS = ("contents", "onResponseReceivedActions", "onResponseReceivedEndpoints", "onResponseReceivedCommands", "continuationContents")
_VIDEO_KEYS = ("videoRenderer", "lockupViewModel", "shortsLockupViewModel", "reelItemRenderer")
# ponytail: matches the bytes of protobuf field 23 (tag ba 01, length 24) instead of decoding the
# message; checked on 271/271 real Shorts. Swap for a wire-format reader if YouTube reorders it.
_REEL_CHANNEL = re.compile(rb"\xba\x01\x18(UC[\w-]{22})")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_VIDEO_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}


def text_of(node: Any) -> str | None:
    """Text from a str, {simpleText}, {runs:[{text}]}, or {content} node."""
    if isinstance(node, str):
        return node.strip() or None
    if not isinstance(node, dict):
        return None
    if "simpleText" in node:
        return text_of(node["simpleText"])
    if "runs" in node:
        return "".join(r.get("text", "") for r in node["runs"] if isinstance(r, dict)).strip() or None
    if "content" in node:
        return text_of(node["content"])
    return None


def _find(node: Any, key: str) -> Iterator[Any]:
    """Depth-first values for `key`, in document order."""
    if isinstance(node, list):
        for child in node:
            yield from _find(child, key)
    elif isinstance(node, dict):
        for k, v in node.items():
            if k in _SKIP:
                continue
            if k == key:
                yield v
            else:
                yield from _find(v, key)


def _dig(node: Any, *path: str) -> Any:
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node


def parse_duration(text: str | None) -> int | None:
    """"12:34" -> 754, "1:02:03" -> 3723; anything else (LIVE, SHORTS, None) -> None."""
    m = _CLOCK.fullmatch((text or "").strip())
    if not m:
        return None
    hours, minutes, seconds = m.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)


def parse_day_header(text: str | None, today: date) -> date | None:
    """History section header -> calendar date relative to `today`; None when unrecognized."""
    t = " ".join((text or "").lower().split())
    if t == "today":
        return today
    if t == "yesterday":
        return today - timedelta(days=1)
    if t in _WEEKDAYS:
        return today - timedelta(days=(today.weekday() - _WEEKDAYS[t]) % 7 or 7)
    m = _MONTH_DAY.fullmatch(t)
    if m:
        month_name, day, year = m.groups()
    else:
        m = _DAY_MONTH.fullmatch(t)
        if not m:
            return None
        day, month_name, year = m.groups()
    month = _MONTHS.get(month_name[:3])
    if not month:
        return None
    try:
        if year:
            return date(int(year), month, int(day))
        d = date(today.year, month, int(day))
        return d if d <= today else date(today.year - 1, month, int(day))
    except ValueError:
        return None


def _channel(node: Any) -> tuple[str | None, str | None]:
    """(UC channel id, channel URL) from the first channel browseEndpoint under node."""
    for endpoint in _find(node, "browseEndpoint"):
        if not isinstance(endpoint, dict):
            continue
        browse_id = str(endpoint.get("browseId", ""))
        if endpoint.get("canonicalBaseUrl"):
            return (browse_id if browse_id.startswith("UC") else None), YT + endpoint["canonicalBaseUrl"]
        if browse_id.startswith("UC"):
            return browse_id, f"{YT}/channel/{browse_id}"
    return None, None


def _reel_channel(endpoint: Any) -> str | None:
    """Shorts show no channel name; reelWatchEndpoint.params (base64 protobuf) carries the channel id."""
    raw = endpoint.get("params") if isinstance(endpoint, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        match = _REEL_CHANNEL.search(base64.urlsafe_b64decode(unquote(raw) + "=="))
    except ValueError:
        return None
    return match.group(1).decode() if match else None


def _has(node: Any, key: str) -> bool:
    return next(_find(node, key), None) is not None


def _video_renderer(r: dict) -> dict:
    byline = r.get("ownerText") or r.get("longBylineText") or r.get("shortBylineText")
    overlays = [o.get("thumbnailOverlayTimeStatusRenderer") or {} for o in r.get("thumbnailOverlays") or []]
    duration = parse_duration(text_of(r.get("lengthText")))
    if duration is None:
        duration = next((d for o in overlays if (d := parse_duration(text_of(o.get("text")))) is not None), None)
    channel_id, channel_url = _channel(byline)
    return {
        "videoId": r.get("videoId"),
        "title": text_of(r.get("title")),
        "channel": text_of(byline),
        "channelId": channel_id,
        "channelUrl": channel_url,
        "durationSec": duration,
        "isShort": any(o.get("style") == "SHORTS" for o in overlays) or _has(r.get("navigationEndpoint"), "reelWatchEndpoint"),
    }


def _lockup(r: dict) -> dict | None:
    if r.get("contentType", "LOCKUP_CONTENT_TYPE_VIDEO") != "LOCKUP_CONTENT_TYPE_VIDEO":
        return None  # playlists, mixes
    meta = _dig(r, "metadata", "lockupMetadataViewModel") or {}
    rows = _dig(meta, "metadata", "contentMetadataViewModel", "metadataRows") or []
    parts = [p for row in rows for p in (row.get("metadataParts") or [])]
    linked = next((p for p in parts if _has(p, "browseEndpoint")), None)
    channel_part = linked or (parts[0] if parts else None)
    badges = [text_of(b.get("text")) for b in _find(r.get("contentImage"), "thumbnailBadgeViewModel") if isinstance(b, dict)]
    channel_id, channel_url = _channel([meta.get("metadata"), meta.get("image")])  # image: avatar, or collab list
    return {
        "videoId": r.get("contentId"),
        "title": text_of(meta.get("title")),
        "channel": text_of((channel_part or {}).get("text")),
        "channelId": channel_id,
        "channelUrl": channel_url,
        "durationSec": next((d for b in badges if (d := parse_duration(b)) is not None), None),
        "isShort": _has(r.get("rendererContext"), "reelWatchEndpoint") or any((b or "").upper() == "SHORTS" for b in badges),
    }


def _short(key: str, r: dict) -> dict:
    if key == "reelItemRenderer":
        endpoint = _dig(r, "navigationEndpoint", "reelWatchEndpoint")
        video_id, title = r.get("videoId"), text_of(r.get("headline"))
    else:
        endpoint = _dig(r, "onTap", "innertubeCommand", "reelWatchEndpoint")
        video_id = _dig(endpoint, "videoId")
        entity = str(r.get("entityId", ""))
        if not video_id and "shorts-shelf-item-" in entity:  # "history-shorts-shelf-item-<id>"
            video_id = entity.rpartition("shorts-shelf-item-")[2]
        title = text_of(_dig(r, "overlayMetadata", "primaryText"))
    channel_id = _reel_channel(endpoint)
    return {
        "videoId": video_id,
        "title": title,
        "channel": None,  # filled by name_channels when the same channel is named elsewhere
        "channelId": channel_id,
        "channelUrl": f"{YT}/channel/{channel_id}" if channel_id else None,
        "durationSec": None,
        "isShort": True,
    }


def video_row(key: str, renderer: dict) -> dict | None:
    """One history item -> row fields (plus internal channelId) except watchedAt/precision/source.

    None if not a video.
    """
    if key == "videoRenderer":
        row = _video_renderer(renderer)
    elif key == "lockupViewModel":
        row = _lockup(renderer)
    else:
        row = _short(key, renderer)
    if not row or not row.get("videoId"):
        return None
    vid = row["videoId"]
    row["url"] = f"{YT}/shorts/{vid}" if row["isShort"] else f"{YT}/watch?v={vid}"
    return row


def channel_names(resp: dict) -> dict[str, tuple[str, str | None]]:
    """UC channel id -> (name, URL) for every named channel in one response."""
    names: dict[str, tuple[str, str | None]] = {}
    roots = [resp.get(k) for k in _ROOTS]
    for key in _VIDEO_KEYS:
        for renderer in _find(roots, key):
            row = video_row(key, renderer) if isinstance(renderer, dict) else None
            if row and row["channel"] and row["channelId"]:
                names.setdefault(row["channelId"], (row["channel"], row["channelUrl"]))
    return names


def name_channels(rows: list[dict], names: dict[str, tuple[str, str | None]]) -> list[dict]:
    """Give unnamed rows (Shorts) the name and URL of the same channel id seen on a named row."""
    prefix = f"{YT}/channel/"
    for row in rows:
        url = row.get("channelUrl") or ""
        if row.get("channel") is None and url.startswith(prefix) and url[len(prefix):] in names:
            row["channel"], row["channelUrl"] = names[url[len(prefix):]]
    return rows


def continuation_token(resp: dict) -> str | None:
    """Next-page token from the primary list's continuationItemRenderer."""
    for item in _find([resp.get(k) for k in _ROOTS], "continuationItemRenderer"):
        command = next(_find(item, "continuationCommand"), None)
        if isinstance(command, dict) and command.get("token"):
            return command["token"]
    return None


def local_midnight(day: date, tz: tzinfo | None = None) -> datetime:
    naive = datetime.combine(day, time())
    return naive.astimezone() if tz is None else naive.replace(tzinfo=tz)


def history_page(
    resp: dict, *, today: date, day: date | None = None, tz: tzinfo | None = None
) -> tuple[list[dict], str | None, date | None]:
    """Rows from one FEhistory response (first page or continuation).

    `day` carries the last section's date from the previous page, for sections that
    continue without a header. Returns (rows, continuation token, last day).
    """
    rows: list[dict] = []
    state = {"day": day}

    def emit(key: str, renderer: Any) -> None:
        row = video_row(key, renderer) if isinstance(renderer, dict) else None
        if not row:
            return
        at = local_midnight(state["day"], tz) if state["day"] else None
        rows.append({
            "title": row["title"],
            "channel": row["channel"],
            "channelUrl": row["channelUrl"],
            "videoId": row["videoId"],
            "url": row["url"],
            "watchedAt": at.isoformat() if at else None,
            "watchedAtPrecision": "day" if at else None,
            "durationSec": row["durationSec"],
            "isShort": row["isShort"],
            "source": "youtube-history",
        })

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key in _SKIP:
                continue
            if key == "itemSectionRenderer" and isinstance(value, dict):
                # header follows contents in the JSON, so read it first. Empty sections are
                # skipped: real pages can end with an empty, out-of-order one ("Aug 23" after
                # "Sep 15") that must not carry into the next page.
                header = text_of(_dig(value, "header", "itemSectionHeaderRenderer", "title"))
                if header and value.get("contents"):
                    state["day"] = parse_day_header(header, today)
                walk(value.get("contents"))
            elif key == "itemSectionHeaderRenderer" and isinstance(value, dict):
                state["day"] = parse_day_header(text_of(value.get("title")), today)
            elif key in _VIDEO_KEYS:
                emit(key, value)
            else:
                walk(value)

    walk([resp.get(k) for k in _ROOTS])
    return rows, continuation_token(resp), state["day"]


def account_from_menu(resp: dict) -> dict:
    """account_menu or accounts_list response -> {accountName, email, handle} (None when absent)."""
    header = next(_find(resp, "activeAccountHeaderRenderer"), None) or {}
    google = next(_find(resp, "googleAccountHeaderRenderer"), None) or {}
    return {
        "accountName": text_of(header.get("accountName")) or text_of(google.get("name")),
        "email": text_of(header.get("email")) or text_of(google.get("email")),
        "handle": text_of(header.get("channelHandle")),
    }


def parse_since(value: str | None, tz: tzinfo | None = None) -> datetime | None:
    """YYYY-MM-DD or ISO-8601 -> aware datetime in local time (or tz). No offset means local wall time."""
    if not value:
        return None
    raw = value.strip()
    if raw[-1:] in ("Z", "z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError("Use YYYY-MM-DD or ISO-8601, e.g. 2026-10-01 or 2026-10-01T08:00:00-05:00.") from None
    if dt.tzinfo is None:
        return dt.astimezone() if tz is None else dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def is_older(row: dict, cutoff: datetime) -> bool:
    """Day-precision rows compare by date; rows with no time are never older."""
    if not row.get("watchedAt"):
        return False
    at = datetime.fromisoformat(row["watchedAt"])
    if row.get("watchedAtPrecision") == "day":
        return at.date() < cutoff.astimezone(at.tzinfo).date()
    return at < cutoff


def take_newer(rows: list[dict], cutoff: datetime | None) -> tuple[list[dict], bool]:
    """Leading rows (newest first) not older than cutoff, and whether an older row was hit."""
    if cutoff is None:
        return list(rows), False
    for i, row in enumerate(rows):
        if is_older(row, cutoff):
            return rows[:i], True
    return list(rows), False


def video_id(text: str) -> str:
    """The 11-character video id in an id, a youtu.be/ID link, or a youtube.com watch?v=ID, /shorts/ID, /embed/ID,
    or /live/ID URL. ValueError for anything else."""
    text = text.strip()
    if VIDEO_ID.fullmatch(text):
        return text
    parts = urlsplit(text if "//" in text else f"https://{text}")
    host = (parts.hostname or "").lower()
    found = None
    if host == "youtu.be":
        found = parts.path.strip("/")
    elif host in _VIDEO_HOSTS:
        if parts.path == "/watch":
            found = (parse_qs(parts.query).get("v") or [""])[0]
        elif parts.path.split("/")[1:2] in (["shorts"], ["embed"], ["live"]):
            found = parts.path.split("/")[2] if len(parts.path.split("/")) > 2 else ""
    if found and VIDEO_ID.fullmatch(found):
        return found
    raise ValueError(f"Not a YouTube video id or link: {text!r}")


def _playlist_items(resp: dict) -> list:
    """The playlist's own list items: playlistVideoListRenderer's on a VL browse page, a continuation's appended
    items otherwise."""
    for found in _find(resp.get("contents"), "playlistVideoListRenderer"):
        return found.get("contents") or [] if isinstance(found, dict) else []
    items: list = []
    for action in _find(resp.get("onResponseReceivedActions"), "appendContinuationItemsAction"):
        items += (action.get("continuationItems") or []) if isinstance(action, dict) else []
    return items


def playlist_page(resp: dict) -> tuple[list[str], str | None]:
    """(video ids in order, the next page's token) for one VL<playlistId> browse page or continuation. Only the
    playlist's own rows: the recommendations YouTube appends below it (their own section and continuation) are left
    out."""
    ids: list[str] = []
    token = None
    for item in _playlist_items(resp):
        if not isinstance(item, dict):
            continue
        row = item.get("playlistVideoRenderer")
        if isinstance(row, dict) and isinstance(row.get("videoId"), str):
            ids.append(row["videoId"])
        command = next(_find(item.get("continuationItemRenderer"), "continuationCommand"), None)
        if isinstance(command, dict) and command.get("token"):
            token = command["token"]
    return ids, token
