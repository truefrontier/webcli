"""Pure helpers for webcli loom: video ids, the GraphQL answers, and the transcript's phrases. No browser."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from urllib.parse import urlsplit

from webcli.core.guard import host_matches
from webcli.sites.loom.endpoints import HOSTS, QUERIES, SHARE_URL, SIGNED_IN_PATHS, SIGNED_OUT_MARKS

ID_RE = re.compile(r"[0-9a-f]{32}")
# The query types the bundled documents select fields on. Any other __typename (GenericError, UserNotAuthorizedError,
# ...) carries none of them.
LIBRARY_TYPE, VIDEO_TYPE = "GetLoomsPayload", "RegularUserVideo"


def video_id(value: str) -> str:
    """The 32-hex video id in an id or a share URL. ValueError when there is none."""
    match = ID_RE.search((value or "").strip().lower())
    if not match:
        raise ValueError("expected a Loom video id or share URL")
    return match.group(0)


@lru_cache(maxsize=None)
def query(name: str) -> dict:
    """The bundled GraphQL document `name` ({operationName, query, variables})."""
    return json.loads((QUERIES / f"{name}.json").read_text())


def fmt_duration(seconds) -> str:
    if seconds is None:
        return ""
    mins, secs = divmod(int(round(float(seconds))), 60)
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"


def _data(body) -> dict:
    data = body.get("data") if isinstance(body, dict) else None
    return data if isinstance(data, dict) else {}


def looms(body) -> dict | None:
    """GetLoomsForLibrary's getLooms when it is a library (an empty one counts); None when it is null or an error
    type. A getLooms without __typename counts as a library."""
    found = _data(body).get("getLooms")
    return found if isinstance(found, dict) and found.get("__typename", LIBRARY_TYPE) == LIBRARY_TYPE else None


def videos_page(body: dict) -> tuple[list[dict], str | None]:
    """GetLoomsForLibrary's answer: ([{id, title, visibility}], the next cursor or None). fetch.library has already
    checked that it holds a library."""
    connection = (looms(body) or {}).get("videos") or {}
    rows = []
    for edge in connection.get("edges") or []:
        node = (edge or {}).get("node") or {}
        if node.get("id"):
            rows.append({"id": node["id"], "title": node.get("name") or "", "visibility": node.get("visibility") or ""})
    info = connection.get("pageInfo") or {}
    return rows, info.get("endCursor") if info.get("hasNextPage") else None


def video(body: dict, vid: str) -> dict | None:
    """GetVideoCardDetails' answer as {id, title, created_at, duration_seconds, views, owner, complete, archived,
    url}; None when Loom says there is no such video (or not one you may see)."""
    found = _data(body).get("video")
    if not isinstance(found, dict) or found.get("__typename") != VIDEO_TYPE:
        return None
    owner = found.get("owner") or {}
    name = owner.get("display_name") or " ".join(x for x in (owner.get("first_name"), owner.get("last_name")) if x)
    return {"id": found.get("id") or vid, "title": found.get("name") or "", "created_at": found.get("createdAt"),
            "duration_seconds": found.get("playable_duration"), "views": (found.get("views") or {}).get("total"),
            "owner": name, "complete": found.get("complete"), "archived": found.get("archived"),
            "url": SHARE_URL.format(id=vid)}


def phrases(data) -> list[dict]:
    """The transcript file's phrases as [{ts, text}]; empty phrases and anything not shaped like one dropped."""
    out = []
    for item in (data.get("phrases") if isinstance(data, dict) else None) or []:
        text = item.get("value") if isinstance(item, dict) else None
        if isinstance(text, str) and text.strip():
            out.append({"ts": item.get("ts"), "text": text.strip()})
    return out


def transcript_text(rows: list[dict]) -> str:
    """One line per phrase: "[1m 5s] text" (no stamp when the phrase has none)."""
    return "\n".join((f"[{fmt_duration(r['ts'])}] " if r.get("ts") is not None else "") + r["text"] for r in rows)


def on_loom(url: str) -> bool:
    return any(host_matches(pattern, urlsplit(url).hostname or "") for pattern in HOSTS)


def signed_out_url(url: str) -> bool:
    """Loom sent the page to its sign-in or sign-up page."""
    return on_loom(url) and any(mark in urlsplit(url).path.lower() for mark in SIGNED_OUT_MARKS)


def signed_in_url(url: str) -> bool:
    """A tab that shows the library or settings: past Loom's sign-in pages (fetch.login also checks the API)."""
    return on_loom(url) and not signed_out_url(url) and urlsplit(url).path.startswith(SIGNED_IN_PATHS)
