"""Google News rendered stories (what fetch.STORIES_JS reads off the page) -> webcli rows. Pure."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from webcli.sites.gnews.endpoints import ORIGIN

UI_LINES = frozenset({"More", "See more headlines & perspectives", "Full Coverage", "Share"})


def _iso(value) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def article_id(href: str) -> str | None:
    """The story's token in ./read/<token> or ./articles/<token> (no query)."""
    path = urlsplit(urljoin(ORIGIN + "/", href or "")).path
    for prefix in ("/read/", "/articles/"):
        if path.startswith(prefix) and len(path) > len(prefix):
            return path[len(prefix):].split("/")[0]
    return None


def _author(lines: list[str]) -> str | None:
    for line in lines:
        if line.startswith("By ") and len(line) > 3:
            return line[3:].strip()
    return None


def row(raw: dict, section: str) -> dict | None:
    """One story: {href, title, lines (the card's text lines), time, img}. None without a story link or title."""
    if not isinstance(raw, dict):
        return None
    token, title = article_id(raw.get("href") or ""), (raw.get("title") or "").strip()
    if not token or not title:
        return None
    lines = [str(x).strip() for x in raw.get("lines") or [] if str(x).strip()]
    publisher = next((x for x in lines if x not in UI_LINES and x != title), None)
    img = raw.get("img")
    thumb = urljoin(ORIGIN + "/", img) if isinstance(img, str) and img and not img.startswith("data:") else None
    url = f"{ORIGIN}/read/{token}"
    return {"source": "google_news", "id": token, "url": url, "title": title, "author": _author(lines),
            "publisher": publisher, "publishedAt": _iso(raw.get("time")), "snippet": None, "thumbnail": thumb,
            "metrics": {}, "newsUrl": url, "section": section}


def rows(raws: list, section: str, limit: int) -> list[dict]:
    out, seen = [], set()
    for raw in raws or []:
        r = row(raw, section)
        if r and r["id"] not in seen:
            seen.add(r["id"])
            out.append(r)
        if len(out) >= limit:
            break
    return out


def section_href(navs: list, label: str) -> str | None:
    """The nav link labelled `label` ([[text, href], ...] read off the page), as an absolute news.google.com URL."""
    for text, href in navs or []:
        if (text or "").strip() == label and href:
            url = urljoin(ORIGIN + "/", href)
            return url if urlsplit(url).hostname == urlsplit(ORIGIN).hostname else None
    return None
