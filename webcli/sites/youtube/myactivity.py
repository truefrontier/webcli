"""Best-effort parser for Google My Activity (YouTube) watch entries. Pure: no browser code.

My Activity's data format is undocumented and has not been sniffed live yet, so nothing
here relies on array positions. Instead it walks the decoded JSON and treats as one entry
the smallest nested list that contains all of:

  * a string starting with "Watched "            (English UI only)
  * an epoch timestamp in microseconds           (16 digits, int or numeric string)
  * a youtube.com watch / shorts / youtu.be URL

A candidate is skipped as ambiguous when it holds more than one video id or "Watched"
title, or when two of its child lists each carry a timestamp plus a title or URL (that is
a container of records, not one record). The channel is the non-URL string sitting
next to a youtube.com/channel/ or /@handle URL (the Takeout "subtitles" shape). Anything
that doesn't match yields no row, so callers see zero rows and fall back. Timestamps are
only ever read, never inferred.

Inputs are raw response bodies: batchexecute streams (")]}'" prefix, length-prefixed
chunks of ["wrb.fr", rpcid, "<json string>", ...] envelopes) or the page HTML, whose
first page is embedded as AF_initDataCallback({... data: [...] ...}).

parse_dom is the fallback for the rendered page (day headers, "Watched <title link>",
channel link, "10:41 AM"). verification_required spots Google's "verify it's you" gate,
which hides every entry until the person re-verifies.
"""

from __future__ import annotations

import json
import re
from collections import deque
from datetime import date, datetime, time, timezone, tzinfo
from html.parser import HTMLParser
from typing import Any, Iterable, Iterator

from webcli.sites.youtube.parse import parse_day_header

WATCHED = "Watched "
MICROS_MIN = 10**15  # 2001-09-09
MICROS_MAX = 10**16  # 2286-11-20
_MICROS_STR = re.compile(r"\d{16}")
_VIDEO = re.compile(
    r"https?://(?:(?:www|m)\.)?(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/)|youtu\.be/)([\w-]{11})"
)
_CHANNEL = re.compile(r"https?://(?:www\.)?youtube\.com/(?:channel/UC[\w-]{22}|@[\w.-]+)")
_TITLE, _TS, _URL = 1, 2, 4
_DECODER = json.JSONDecoder()
_INIT_DATA = re.compile(r"AF_initDataCallback\(")
_DATA_KEY = re.compile(r"\bdata:")
# Whole lines only, so a watched video titled "Verify it's you" ("Watched Verify it's you") isn't the gate.
_GATE = re.compile(r"^\s*(?:(?:to see your full history,\s*)?verify it['\u2019]s you\.?|verify)\s*$", re.I | re.M)
_MERIDIEM = re.compile(r"[ap]\.?\s*m\.?", re.I)
_CLOCK = re.compile(r"(\d{1,2}):(\d{2})(?:\s*([ap])\.?\s*m\b\.?)?(?![\d:])", re.I)


def verification_required(page_text: str) -> bool:
    """True when My Activity shows "To see your full history, verify it's you" / a Verify button."""
    return bool(_GATE.search(page_text or ""))


def iter_payloads(text: str) -> Iterator[Any]:
    """Yield decoded JSON payloads from a batchexecute body or a My Activity HTML page."""
    stripped = text.lstrip()
    if stripped.startswith(")]}'"):
        yield from _batch_payloads(stripped[4:])
    else:
        yield from _init_data(text)


def _batch_payloads(body: str) -> Iterator[Any]:
    idx = 0
    while idx < len(body):
        while idx < len(body) and body[idx].isspace():
            idx += 1
        if idx >= len(body):
            break
        try:
            value, idx = _DECODER.raw_decode(body, idx)
        except json.JSONDecodeError:
            break
        if isinstance(value, list):  # bare ints are chunk lengths
            yield from _envelopes(value)


def _envelopes(node: list) -> Iterator[Any]:
    if len(node) >= 3 and node[0] == "wrb.fr" and isinstance(node[2], str):
        try:
            yield json.loads(node[2])
        except json.JSONDecodeError:
            pass
        return
    for child in node:
        if isinstance(child, list):
            yield from _envelopes(child)


def _init_data(html: str) -> Iterator[Any]:
    for match in _INIT_DATA.finditer(html):
        data = _DATA_KEY.search(html, match.end(), match.end() + 400)
        if not data:
            continue
        start = html.find("[", data.end())
        if start < 0:
            continue
        try:
            value, _ = _DECODER.raw_decode(html, start)
        except json.JSONDecodeError:
            continue
        yield value


def _children(node: Any) -> Iterable[Any]:
    if isinstance(node, dict):
        return node.values()
    if isinstance(node, list):
        return node
    return ()


def _is_micros(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return MICROS_MIN <= value < MICROS_MAX
    return isinstance(value, str) and bool(_MICROS_STR.fullmatch(value))


def _flags(node: Any, out: list) -> int:
    """Bitmask of entry signals in node's subtree; appends minimal full matches to out."""
    if isinstance(node, str):
        flags = _TS if _is_micros(node) else 0
        if node.startswith(WATCHED):
            flags |= _TITLE
        if _VIDEO.search(node):
            flags |= _URL
        return flags
    if not isinstance(node, (list, dict)):
        return _TS if _is_micros(node) else 0
    flags, nested, records = 0, False, 0
    for child in _children(node):
        child_flags = _flags(child, out)
        nested |= child_flags == 7
        # a child holding a timestamp plus a title or URL looks like a whole record
        records += isinstance(child, (list, dict)) and child_flags & _TS and child_flags != _TS
        flags |= child_flags
    if flags == 7 and not nested and records < 2:  # two records would be a container, not one entry
        out.append(node)
    return flags


def _bfs(node: Any) -> Iterator[Any]:
    queue = deque([node])
    while queue:
        current = queue.popleft()
        yield current
        queue.extend(c for c in _children(current) if isinstance(c, (list, dict)))


def _scalars(node: Any) -> Iterator[Any]:
    for container in _bfs(node):
        for child in _children(container):
            if not isinstance(child, (list, dict)):
                yield child


def _channel(node: Any) -> tuple[str | None, str | None]:
    for container in _bfs(node):
        strings = [c for c in _children(container) if isinstance(c, str)]
        url = next((s for s in strings if _CHANNEL.fullmatch(s)), None)
        if url:
            name = next((s for s in strings if s and not s.startswith(("http", WATCHED)) and not _is_micros(s)), None)
            return name, url
    return None, None


def _entry(node: Any, tz: tzinfo | None) -> dict | None:
    scalars = list(_scalars(node))
    strings = [s for s in scalars if isinstance(s, str)]
    ids = {m.group(1) for s in strings for m in [_VIDEO.search(s)] if m}
    if len(ids) != 1:
        return None
    video_id = ids.pop()
    micros = next((int(s) for s in scalars if isinstance(s, int) and _is_micros(s)), None)
    if micros is None:
        micros = next(int(s) for s in scalars if _is_micros(s))
    titles = {s for s in strings if s.startswith(WATCHED)}
    if len(titles) != 1:
        return None
    title = titles.pop()[len(WATCHED):].strip()
    if _VIDEO.search(title):
        title = None  # "Watched https://..." means the video title is unavailable
    video_url = next(s for s in strings if _VIDEO.search(s))
    is_short = "/shorts/" in video_url
    channel, channel_url = _channel(node)
    watched = datetime.fromtimestamp(micros / 1_000_000, tz=timezone.utc).astimezone(tz)
    return {
        "title": title,
        "channel": channel,
        "channelUrl": channel_url,
        "videoId": video_id,
        "url": f"https://www.youtube.com/{'shorts/' if is_short else 'watch?v='}{video_id}",
        "watchedAt": watched.isoformat(timespec="seconds"),
        "watchedAtPrecision": "exact",
        "durationSec": None,
        "isShort": is_short,
        "source": "myactivity",
        "_micros": micros,
    }


def entries_from_payload(payload: Any, tz: tzinfo | None = None) -> list[dict]:
    """Watch rows found anywhere in one decoded payload (each row keeps its `_micros` key)."""
    candidates: list = []
    _flags(payload, candidates)
    return [row for row in (_entry(c, tz) for c in candidates) if row]


def parse_bodies(bodies: Iterable[str], tz: tzinfo | None = None) -> list[dict]:
    """Deduplicated watch rows from raw bodies, newest first. Drops the internal `_micros` key."""
    seen: dict[tuple[str, int], dict] = {}
    for body in bodies:
        for payload in iter_payloads(body):
            for row in entries_from_payload(payload, tz):
                seen.setdefault((row["videoId"], row["_micros"]), row)
    rows = sorted(seen.values(), key=lambda r: r["_micros"], reverse=True)
    return [{k: v for k, v in row.items() if k != "_micros"} for row in rows]


class _Tokens(HTMLParser):
    """Rendered HTML -> ordered ("text", str) and ("a", href, text) tokens; scripts and styles skipped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[tuple] = []
        self._href: str | None = None
        self._link: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "a":
            self._href, self._link = dict(attrs).get("href") or "", []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._href is not None:
            self.tokens.append(("a", self._href, " ".join("".join(self._link).split())))
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._href is not None:
            self._link.append(data)
        elif data.strip():
            text = " ".join(data.split())
            if _MERIDIEM.fullmatch(text) and self.tokens and self.tokens[-1][0] == "text":
                self.tokens[-1] = ("text", f"{self.tokens[-1][1]} {text}")  # <span>10:41</span><span>PM</span>
            else:
                self.tokens.append(("text", text))


def _clock(text: str) -> time | None:
    m = _CLOCK.match(text)
    if not m:
        return None
    hour, minute, half = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if half:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if half == "p" else 0)
    return time(hour, minute) if hour < 24 and minute < 60 else None


def parse_dom(html: str, today: date, tz: tzinfo | None = None) -> list[dict]:
    """Watch rows from the rendered My Activity page, in page order (newest first).

    A day header ("Today", "Oct 12") sets the day. "Watched" followed by a youtube.com video
    link opens an entry; the next channel link and the next "10:41 AM" text fill it. The entry
    closes at its time, the next entry, or the next day header, so fields never cross entries.
    watchedAt is exact (to the minute) only when both the day and the time are present.
    """
    parser = _Tokens()
    parser.feed(html or "")
    rows: list[dict] = []
    day: date | None = None
    entry: dict | None = None
    previous = ""

    def close() -> None:
        nonlocal entry
        if entry:
            at = None
            if entry["day"]:
                naive = datetime.combine(entry["day"], entry["clock"] or time())
                at = naive.astimezone() if tz is None else naive.replace(tzinfo=tz)
            is_short = "/shorts/" in entry["href"]
            rows.append({
                "title": entry["title"] or None,
                "channel": entry["channel"],
                "channelUrl": entry["channelUrl"],
                "videoId": entry["videoId"],
                "url": f"https://www.youtube.com/{'shorts/' if is_short else 'watch?v='}{entry['videoId']}",
                "watchedAt": at.isoformat(timespec="seconds") if at else None,
                "watchedAtPrecision": ("exact" if entry["clock"] else "day") if at else None,
                "durationSec": None,
                "isShort": is_short,
                "source": "myactivity",
            })
        entry = None

    for token in parser.tokens:
        if token[0] == "text":
            text = token[1]
            if header := parse_day_header(text, today):
                close()
                day = header
            elif entry and (clock := _clock(text)):
                entry["clock"] = clock
                close()
            previous = text
            continue
        _, href, text = token
        video = _VIDEO.search(href)
        if video and previous == "Watched":
            close()
            entry = {"href": href, "videoId": video.group(1), "title": text, "channel": None,
                     "channelUrl": None, "clock": None, "day": day}
        elif entry and entry["channelUrl"] is None and (channel := _CHANNEL.match(href)):
            entry["channel"], entry["channelUrl"] = text or None, channel.group(0)
        previous = ""
    close()
    return rows
