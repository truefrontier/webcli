"""Pure helpers for webcli loom: video ids, the GraphQL answers, and the transcript's phrases. No browser."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from urllib.parse import urlsplit

from webcli.core.guard import host_matches
from webcli.sites.loom.endpoints import HOSTS, QUERIES, SHARE_URL, SIGNED_IN_PATHS, SIGNED_OUT_MARKS

ID_RE = re.compile(r"[0-9a-f]{32}")
NOT_A_VIDEO = {"GenericError", "UserNotAuthorizedError"}


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


def videos_page(body: dict) -> tuple[list[dict], str | None]:
    """GetLoomsForLibrary's answer: ([{id, title, visibility}], the next cursor or None)."""
    connection = (((body or {}).get("data") or {}).get("getLooms") or {}).get("videos") or {}
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
    found = ((body or {}).get("data") or {}).get("video") or {}
    if not found or found.get("__typename") in NOT_A_VIDEO:
        return None
    owner = found.get("owner") or {}
    name = owner.get("display_name") or " ".join(x for x in (owner.get("first_name"), owner.get("last_name")) if x)
    return {"id": found.get("id") or vid, "title": found.get("name") or "", "created_at": found.get("createdAt"),
            "duration_seconds": found.get("playable_duration"), "views": (found.get("views") or {}).get("total"),
            "owner": name, "complete": found.get("complete"), "archived": found.get("archived"),
            "url": SHARE_URL.format(id=vid)}


def phrases(data: dict) -> list[dict]:
    """The transcript file's phrases as [{ts, text}], empty ones dropped."""
    out = []
    for item in (data or {}).get("phrases") or []:
        text = ((item or {}).get("value") or "").strip()
        if text:
            out.append({"ts": item.get("ts"), "text": text})
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
    """A tab that shows the library, a video, or settings: sign-in is done."""
    return on_loom(url) and not signed_out_url(url) and urlsplit(url).path.startswith(SIGNED_IN_PATHS)
