"""Perplexity Discover JSON -> webcli rows. Pure: no browser, no network."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

from webcli.sites.perplexity.endpoints import ORIGIN, TOPIC_ALIASES


def topic_slug(topic: str) -> str:
    """The Discover slug a --topic reads (science shares tech's "Tech & Science")."""
    return TOPIC_ALIASES.get(topic, topic)


def topic_id(topics: dict, slug: str) -> str | None:
    """The feed's topic parameter: "top" as is; any other slug -> its uuid from /rest/discover/topics (None when
    Perplexity lists no such topic)."""
    if slug == "top":
        return "top"
    for entry in (topics or {}).get("all_topics") or []:
        if isinstance(entry, dict) and entry.get("slug") == slug and entry.get("id"):
            return str(entry["id"])
    return None


def _iso_utc(value) -> str | None:
    """published_timestamp has no zone ("2026-10-26T17:32:52.200321"); Perplexity's are UTC. ISO 8601 with +00:00."""
    if not isinstance(value, str) or not value:
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc).isoformat(timespec="seconds")


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _first(items) -> dict:
    return items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}


def row(item: dict, slug: str) -> dict | None:
    """One Discover card as a row; None when it has no id or title."""
    if not isinstance(item, dict) or not item.get("uuid") or not (item.get("title") or item.get("short_title")):
        return None
    sources = [s for s in item.get("bullet_summary_web_results_preload") or [] if isinstance(s, dict) and s.get("url")]
    image = _first(item.get("featured_images"))
    social = item.get("social_info") if isinstance(item.get("social_info"), dict) else {}
    preview = item.get("web_results_preview") if isinstance(item.get("web_results_preview"), dict) else {}
    page_slug = item.get("slug")
    return {
        "source": "perplexity",
        "id": str(item["uuid"]),
        "url": (sources[0]["url"] if sources else None) or image.get("url") or item.get("url") or None,
        "title": item.get("title") or item.get("short_title"),
        "author": item.get("author_username") or None,
        "publishedAt": _iso_utc(item.get("published_timestamp")),
        "snippet": item.get("summary") or item.get("description") or None,
        "thumbnail": image.get("thumbnail") or image.get("image") or None,
        "metrics": {"views": _int(social.get("view_count")), "likes": _int(social.get("like_count")),
                    "forks": _int(social.get("fork_count")),
                    "sources": _int(preview.get("total_count")) if preview else len(sources) or None},
        "perplexityUrl": f"{ORIGIN}/discover/{quote(slug)}/{quote(str(page_slug))}" if page_slug else None,
        "topic": slug,
    }


def feed_rows(feed: dict, slug: str) -> list[dict]:
    return [r for r in (row(i, slug) for i in (feed or {}).get("items") or []) if r]
