"""Site rows -> newsfeed.items/1 items. Pure.

An item: {source, native_id, url, title, text, author{name, handle, profile_url}, published_at (UTC, Z), metrics,
links, media, meta{via, network, pick_slot, why_it_fits, auth, ...}}.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

SCHEMA = "newsfeed.items/1"


def utc_z(value: Any) -> str | None:
    """Any ISO 8601 time -> UTC with Z (no zone: taken as UTC). None for anything else."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        at = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _media(url: Any) -> list[dict]:
    return [{"type": "image", "url": url}] if isinstance(url, str) and url.startswith("http") else []


def _first_line(text: Any, limit: int = 140) -> str | None:
    line = next((x.strip() for x in str(text or "").splitlines() if x.strip()), "")
    return (line[: limit - 1] + "…" if len(line) > limit else line) or None


def item(site: str, read: str, row: dict) -> dict | None:
    """One row of `webcli <site> <read>` as an item; None when it has no id or url."""
    if not isinstance(row, dict):
        return None
    via = f"webcli {site} {read}"
    auth = row.get("auth") or "signed_in"
    if site == "linkedin":
        native, url = row.get("urn"), row.get("url")
        out = {"source": "linkedin", "native_id": native, "url": url, "title": _first_line(row.get("text")),
               "text": row.get("text"),
               "author": {"name": row.get("author_name"), "handle": row.get("author_handle"),
                          "profile_url": (f"https://www.linkedin.com/in/{row['author_handle']}/"
                                          if row.get("author_handle") else None)},
               "published_at": utc_z(row.get("posted_at")),
               "metrics": {k: row.get(k) for k in ("reactions", "comments", "reposts")},
               "links": [u for u in (row.get("original_url"),) if u], "media": [],
               "meta": {"kind": row.get("kind"), "headline": row.get("author_headline")}}
    else:
        native, url = row.get("id"), row.get("url") or row.get("perplexityUrl") or row.get("newsUrl")
        source = row.get("source") or site
        author = {"name": row.get("author"), "handle": None, "profile_url": row.get("channelUrl")}
        if source == "google_news":
            author = {"name": row.get("author") or row.get("publisher"), "handle": None, "profile_url": None}
        out = {"source": source, "native_id": native, "url": url, "title": row.get("title"),
               "text": row.get("snippet"), "author": author, "published_at": utc_z(row.get("publishedAt")),
               "metrics": row.get("metrics") or {},
               "links": [u for u in (row.get("perplexityUrl"), row.get("newsUrl")) if u and u != url],
               "media": _media(row.get("thumbnail")),
               "meta": {k: row[k] for k in ("publisher", "topic", "section", "publishedText") if row.get(k)}}
    if not native or not url:
        return None
    out["meta"] = {"via": via, "network": out["source"], "pick_slot": None, "why_it_fits": None, "auth": auth,
                   "reads": [read], **out["meta"]}
    return out


def merge(items: list[dict]) -> list[dict]:
    """Drop repeats of the same (source, native_id): the first stays and lists every read that showed it."""
    out: dict[tuple, dict] = {}
    for it in items:
        key = (it["source"], it["native_id"])
        if key in out:
            reads = out[key]["meta"].setdefault("reads", [])
            reads += [r for r in it["meta"].get("reads", []) if r not in reads]
        else:
            out[key] = it
    return list(out.values())


def imported(doc: Any) -> list[dict]:
    """Items from another newsfeed.items/1 file (e.g. an X export): those with a source, native_id, and url."""
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise ValueError(f"not a {SCHEMA} file")
    out = []
    for it in doc.get("items") or []:
        if isinstance(it, dict) and it.get("source") and it.get("native_id") and it.get("url"):
            it = dict(it)
            it["published_at"] = utc_z(it.get("published_at")) or it.get("published_at")
            it["meta"] = {"auth": "signed_in", **(it.get("meta") or {})}
            out.append(it)
    return out
