"""Pure helpers for the read-only post commands (`feed`, `posts search|from|read`): Voyager feed updates (the dash
GraphQL `Update` and the REST `UpdateV2`) and the rendered SDUI post cards, as one row shape. No browser, no network.

A row: url, urn, author_name, author_handle, author_headline, posted_at, text, reactions, comments, reposts, kind
(post | repost | article), original_url (a repost's original post, else null).

Numbers are never invented (limsg's null rules): a count is an int only when LinkedIn's response (or the rendered
card) shows it, else null. A count LinkedIn hides is null, not 0. posted_at comes from the post urn: LinkedIn's
activity, share, and ugcPost ids carry their creation time (the id's top 41 bits are Unix milliseconds). A rendered
card without a urn falls back to its relative label ("3h", "2d"), which is only as precise as the label."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from webcli.sites.linkedin.endpoints import FEED_UPDATE, ORIGIN
from webcli.sites.linkedin.parse import linkedin_time_to_iso

FIELDS = ["url", "urn", "author_name", "author_handle", "author_headline", "posted_at", "text", "reactions",
          "comments", "reposts", "kind", "original_url"]
COMPACT = ("posted_at", "author_name", "kind", "reactions", "comments", "reposts", "url")
COLUMNS = ["posted_at", "author_name", "kind", "reactions", "comments", "reposts", "text"]

POST_URN = re.compile(r"urn:li:(activity|share|ugcPost):(\d{6,25})")
_SLUG_ID = re.compile(r"[-_](activity|share|ugcPost)[-_](\d{6,25})")
_HANDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,99}")
_ID_EPOCH_SHIFT = 22  # LinkedIn's post ids: Unix ms << 22
_EARLIEST = datetime(2006, 1, 1, tzinfo=timezone.utc)  # older "times" mean the id isn't time-based


# --- urns, urls, times ---------------------------------------------------------------------------------------------

def post_urn(text: str | None) -> str | None:
    """The first urn:li:activity|share|ugcPost:N in `text` (URL-decoded), else None."""
    found = POST_URN.search(unquote(text or ""))
    return found.group(0) if found else None


def parse_post_target(value: str) -> str:
    """URL_OR_URN for `posts read`: urn:li:activity|share|ugcPost:N, a /feed/update/<urn>/ URL, or a /posts/<slug>-
    activity-N-xxxx URL. ValueError otherwise."""
    raw = (value or "").strip()
    urn = post_urn(raw)
    if urn:
        return urn
    parts = urlsplit(raw if "://" in raw else f"https://{raw}")
    if (parts.hostname or "").endswith("linkedin.com") and (slug := _SLUG_ID.search(parts.path)):
        return f"urn:li:{slug.group(1)}:{slug.group(2)}"
    raise ValueError("Expected a post urn (urn:li:activity:N, urn:li:share:N, urn:li:ugcPost:N) or a LinkedIn post "
                     "URL (/feed/update/urn:li:activity:N/ or /posts/...-activity-N-...).")


def post_url(urn: str | None) -> str | None:
    return FEED_UPDATE.format(urn=urn) if urn else None


def urn_time(urn: str | None) -> str | None:
    """posted_at from a post urn's id (ISO 8601 UTC, seconds), or None when it isn't a plausible time."""
    found = POST_URN.search(urn or "")
    if not found:
        return None
    try:
        when = datetime.fromtimestamp((int(found.group(2)) >> _ID_EPOCH_SHIFT) / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    if when < _EARLIEST or when > datetime.now(timezone.utc) + timedelta(days=1):
        return None
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def author_handle(url: str | None) -> str | None:
    """A member's vanity name from /in/<handle>/, else the canonical page URL (/company/x/, /showcase/x/,
    /school/x/, /groups/N/), else None."""
    if not url:
        return None
    parts = urlsplit(url if "://" in url else ORIGIN + url)
    segments = [unquote(s) for s in parts.path.split("/") if s]
    if not (parts.hostname or "").endswith("linkedin.com") or len(segments) < 2:
        return None
    if segments[0] == "in":
        return segments[1]
    if segments[0] in ("company", "showcase", "school", "groups"):
        return f"{ORIGIN}/{segments[0]}/{quote(segments[1], safe='')}/"
    return None


def parse_author_target(value: str) -> tuple[str, str]:
    """HANDLE_OR_URL for `posts from`: ("member", handle) or ("company", universal name). ValueError otherwise.
    Takes kevnk, @kevnk, in/kevnk, linkedin.com/in/kevnk, https://www.linkedin.com/in/kevnk/, and company/x (or a
    /company/, /showcase/, /school/ URL)."""
    raw = (value or "").strip()
    bare = raw.lstrip("@").strip("/")
    if bare and _HANDLE.fullmatch(bare):
        return "member", bare
    if "://" in raw:
        url = raw
    elif raw.lstrip("/").startswith(("linkedin.com/", "www.linkedin.com/")):
        url = "https://" + raw.lstrip("/")
    else:
        url = f"{ORIGIN}/{raw.lstrip('/')}"
    parts = urlsplit(url)
    segments = [unquote(s) for s in parts.path.split("/") if s]
    host = parts.hostname or ""
    if (host == "linkedin.com" or host.endswith(".linkedin.com")) and len(segments) >= 2 \
            and _HANDLE.fullmatch(segments[1]):
        if segments[0] == "in":
            return "member", segments[1]
        if segments[0] in ("company", "showcase", "school"):
            return "company", segments[1]
    raise ValueError(f"Not a LinkedIn handle or a profile or company URL: {value!r}")


def apply_since(rows: list[dict], since: datetime | None) -> list[dict]:
    """Rows posted at or after `since`. A row with no posted_at can't be shown to be recent, so it's dropped."""
    if since is None:
        return rows
    kept = []
    for row in rows:
        stamp = row.get("posted_at")
        if not stamp:
            continue
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if when >= since:
            kept.append(row)
    return kept


def newest_first(rows: list[dict]) -> list[dict]:
    """Stable newest-first by posted_at; rows without one keep their order, last."""
    dated = [r for r in rows if r.get("posted_at")]
    return sorted(dated, key=lambda r: r["posted_at"], reverse=True) + [r for r in rows if not r.get("posted_at")]


# --- Voyager updates -----------------------------------------------------------------------------------------------

def _get(data: Any, *keys: str) -> Any:
    for key in keys:
        data = data.get(key) if isinstance(data, dict) else None
    return data


def _text(view: Any) -> str | None:
    """A TextViewModel's text ({text: "..."}), or a plain string."""
    if isinstance(view, str):
        return view
    value = _get(view, "text")
    if isinstance(value, dict):
        value = value.get("text")
    return value if isinstance(value, str) else None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _ref(obj: dict, key: str, index: dict[str, dict]) -> dict | None:
    """obj[key] inline, or obj["*key"] resolved through `included`."""
    inline = obj.get(key)
    if isinstance(inline, dict):
        return inline
    pointer = obj.get(f"*{key}")
    return index.get(pointer) if isinstance(pointer, str) else None


def _index(data: Any) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for obj in (data.get("included") or []) if isinstance(data, dict) else []:
        if isinstance(obj, dict):
            for key in ("entityUrn", "dashEntityUrn"):
                if isinstance(obj.get(key), str):
                    out.setdefault(obj[key], obj)
    return out


def _update_type(obj: Any) -> bool:
    kind = str(_get(obj, "$type") or "")
    return kind.endswith(".feed.Update") or kind.endswith(".UpdateV2")


def element_urns(data: Any) -> list[str]:
    """The response's ordered update pointers: data.data.<query>.*elements (GraphQL), data.*elements, or
    *elements at the top (REST)."""
    if not isinstance(data, dict):
        return []
    holders = [data, data.get("data")]
    inner = _get(data, "data", "data")
    if isinstance(inner, dict):
        holders += [v for v in inner.values() if isinstance(v, dict)]
    for holder in holders:
        found = _get(holder, "*elements")
        if isinstance(found, list):
            return [u for u in found if isinstance(u, str)]
    return []


def pagination_token(data: Any) -> str | None:
    inner = _get(data, "data", "data")
    for holder in [*(inner.values() if isinstance(inner, dict) else []), _get(data, "data"), data]:
        token = _get(holder, "metadata", "paginationToken")
        if isinstance(token, str) and token:
            return token
    return None


def _skip(entity: str) -> bool:
    """Ads, promos, and carousels of other updates: not posts."""
    return any(tag in entity for tag in ("sponsoredContent", "inAppPromotion", "urn:li:aggregate:", "sponsored"))


def _activity(update: dict) -> str | None:
    meta = _get(update, "metadata") or _get(update, "updateMetadata") or {}
    for value in (meta.get("backendUrn"), meta.get("urn"), update.get("entityUrn"), update.get("urn")):
        if isinstance(value, str) and (urn := post_urn(value)):
            return urn
    return None


def _counts(update: dict, index: dict[str, dict], by_urn: dict[str, dict]) -> dict[str, int | None]:
    detail = _ref(update, "socialDetail", index) or {}
    counts = _ref(detail, "totalSocialActivityCounts", index)
    if counts is None:
        for urn in (detail.get("threadUrn"), _get(update, "metadata", "shareUrn"), _activity(update)):
            if isinstance(urn, str) and urn in by_urn:
                counts = by_urn[urn]
                break
    counts = counts or {}
    social = update.get("socialContent") or {}
    hidden = bool(social.get("hideSocialActivityCounts"))
    return {"reactions": None if hidden or social.get("hideReactionsCount") else _count(counts.get("numLikes")),
            "comments": None if hidden or social.get("hideCommentsCount") else _count(counts.get("numComments")),
            "reposts": None if hidden or social.get("hideRepostsCount") else _count(counts.get("numShares"))}


def _handle(actor: dict, index: dict[str, dict]) -> str | None:
    """A member's vanity from the profile the actor's name points at, else from the actor's link."""
    for attr in _get(actor, "name", "attributesV2") or _get(actor, "name", "attributes") or []:
        detail = _get(attr, "detailData") or {}
        pointer = detail.get("*profileFullName") or detail.get("*profile") or _get(attr, "*miniProfile")
        profile = index.get(pointer) if isinstance(pointer, str) else None
        if profile and isinstance(profile.get("publicIdentifier"), str):
            return profile["publicIdentifier"]
    target = _get(actor, "navigationContext", "actionTarget") or _get(actor, "navigationContext", "url")
    return author_handle(target) if isinstance(target, str) else None


def _is_article(update: dict) -> bool:
    content = update.get("content") or {}
    for key, value in content.items() if isinstance(content, dict) else []:
        if "article" not in key.lower() or not isinstance(value, dict):
            continue
        target = (_get(value, "navigationContext", "actionTarget") or _get(value, "navigationContext", "url")
                  or value.get("url") or "")
        if "linkedin.com/pulse/" in str(target):
            return True
    return False


def _header(update: dict) -> str:
    return _text(_get(update, "header", "text")) or ""


def update_row(update: dict, index: dict[str, dict], by_urn: dict[str, dict]) -> dict[str, Any] | None:
    """One Update / UpdateV2 as a row, or None for what isn't a post (an ad, a promo, a carousel, no actor)."""
    entity = str(update.get("entityUrn") or "")
    actor = update.get("actor")
    urn = _activity(update)
    if _skip(entity) or not isinstance(actor, dict) or urn is None:
        return None
    reshared = _ref(update, "resharedUpdate", index)
    original = _activity(reshared) if reshared else None
    header = _header(update).lower()
    kind = "repost" if reshared is not None or header.endswith("reposted this") else (
        "article" if _is_article(update) else "post")
    target = _get(actor, "navigationContext", "actionTarget") or _get(actor, "navigationContext", "url")
    return {
        "url": post_url(urn),
        "urn": urn,
        "author_name": _text(actor.get("name")),
        "author_handle": _handle(actor, index) or (author_handle(target) if isinstance(target, str) else None),
        "author_headline": _text(actor.get("description")),
        "posted_at": urn_time(urn),
        "text": _text(_get(update, "commentary", "text")) or "",
        **_counts(update, index, by_urn),
        "kind": kind,
        "original_url": post_url(original) if original and original != urn else None,
    }


def updates(data: Any) -> list[dict[str, Any]]:
    """Rows for a feed / profile-updates / single-update response, in the response's order, one per post urn."""
    index = _index(data)
    by_urn = {o["urn"]: o for o in index.values() if isinstance(o.get("urn"), str)
              and str(o.get("$type", "")).endswith("SocialActivityCounts")}
    pointers = element_urns(data)
    found = [index[p] for p in pointers if p in index] if pointers else [o for o in index.values() if _update_type(o)]
    rows, seen = [], set()
    for update in found:
        if not _update_type(update):
            continue
        row = update_row(update, index, by_urn)
        if row and row["urn"] not in seen:
            seen.add(row["urn"])
            rows.append(row)
    return rows


def profile_urn(data: Any, handle: str) -> str | None:
    """The fsd_profile urn of `handle` from /identity/dash/profiles?q=memberIdentity."""
    for obj in _index(data).values():
        if str(obj.get("publicIdentifier") or "").lower() == handle.lower():
            urn = str(obj.get("entityUrn") or "")
            if urn.startswith("urn:li:fsd_profile:"):
                return urn
    return None


# --- Rendered SDUI post cards (search results, company pages) ------------------------------------------------------

_TIME = re.compile(r"^(\d+)\s*(s|m|h|d|w|mo|yr)\b", re.I)
_DEGREE = re.compile(r"^(•\s*)?(1st|2nd|3rd\+?|following|you|verified|premium|influencer|author)$", re.I)
_SHARE_ID = re.compile(r"(shareId|ugcPostId|activityId)=(\d{15,25})")
_ACTIONS = {"follow", "following", "connect", "message", "subscribe", "view my newsletter", "visit my website",
            "view my portfolio", "book an appointment", "promoted"}
_COUNT = re.compile(r"^\d{1,3}(?:,\d{3})*$|^\d+$")


def card_count(text: str | None) -> int | None:
    """A rendered count ("29", "1,234"). Blank, zero-width, or abbreviated ("1.2K") is null: never a guess."""
    value = (text or "").replace("\u200b", "").strip()
    return int(value.replace(",", "")) if _COUNT.fullmatch(value) else None


def _card_time(lines: list[str], now: datetime) -> str | None:
    for line in lines:
        token = line.replace("•", " ").split("Edited")[0].strip()
        found = _TIME.match(token)
        if found:
            unit = found.group(2).lower()
            if unit in ("mo", "yr"):  # months and years: too coarse to compute; still a known-old post
                days = int(found.group(1)) * (30 if unit == "mo" else 365)
                return linkedin_time_to_iso(f"{days}d", now=now)
            return linkedin_time_to_iso(f"{found.group(1)}{unit}", now=now)
    return None


def card_urn(card: dict) -> str | None:
    """A post urn the card's markup carries (a /feed/update/ link, a urn, or a shareId=/ugcPostId= key), else None."""
    for href in card.get("links") or []:
        if "/feed/update/" in str(href) and (urn := post_urn(href)):
            return urn
    urn = post_urn(card.get("ids") or "")
    if urn:
        return urn
    found = _SHARE_ID.search(card.get("ids") or "")
    if found:
        kind = {"shareId": "share", "ugcPostId": "ugcPost", "activityId": "activity"}[found.group(1)]
        return f"urn:li:{kind}:{found.group(2)}"
    return None


def card_row(card: dict, *, now: datetime | None = None) -> dict[str, Any] | None:
    """A rendered post card (see fetch.POST_CARD_JS) as a row, or None without an author."""
    now = now or datetime.now(timezone.utc)
    name = (card.get("actorName") or "").strip()
    if not name:
        return None
    meta = [line.strip() for line in card.get("actorLines") or [] if line and line.strip()]
    meta = meta[meta.index(name) + 1:] if name in meta else meta
    rest = [line for line in meta if not _DEGREE.fullmatch(line) and line.lower() not in _ACTIONS
            and not (line.startswith("•") and not _TIME.match(line.replace("•", " ").strip()))]
    when = _card_time(rest, now)
    headline = next((line for line in rest if not _TIME.match(line.replace("•", " ").strip())), None)
    urn = card_urn(card)
    header = " ".join(card.get("header") or []).lower()
    article = any("linkedin.com/pulse/" in unquote(str(h)) for h in card.get("articleLinks") or [])
    text = (card.get("text") or "").strip()
    text = re.sub(r"\s*(…|\.\.\.)\s*more$", "", text).strip()
    return {
        "url": post_url(urn),
        "urn": urn,
        "author_name": name,
        "author_handle": author_handle(card.get("actorHref")),
        "author_headline": headline,
        "posted_at": urn_time(urn) or when,
        "text": text,
        "reactions": card_count(card.get("reactions")),
        "comments": card_count(card.get("comments")),
        "reposts": card_count(card.get("reposts")),
        "kind": "repost" if "reposted this" in header else ("article" if article else "post"),
        "original_url": None,
    }


def cards(raw: list[Any], *, now: datetime | None = None) -> list[dict[str, Any]]:
    rows, seen = [], set()
    for card in raw:
        row = card_row(card, now=now) if isinstance(card, dict) else None
        if row is None:
            continue
        key = row["urn"] or (row["author_name"], row["text"][:200])
        if key not in seen:
            seen.add(key)
            rows.append(row)
    return rows


SEARCH_SORTS = {"relevance": "relevance", "top": "relevance", "latest": "date_posted"}  # --sort -> LinkedIn's sortBy
DEFAULT_SORT = "relevance"


def search_url(query: str, since: datetime | None, *, sort: str = DEFAULT_SORT, now: datetime | None = None) -> str:
    """The content search page in LinkedIn's own order: relevance ("Top", the default) or latest (date posted).
    Narrowed to LinkedIn's past-24h / past-week / past-month when --since fits inside one (the --since cut itself is
    applied client-side too)."""
    if sort not in SEARCH_SORTS:
        raise ValueError(f"Unknown sort {sort!r}: use {', '.join(SEARCH_SORTS)}.")
    now = now or datetime.now(timezone.utc)
    url = (f"{ORIGIN}/search/results/content/?keywords={quote(query, safe='')}&origin=FACETED_SEARCH"
           f"&sortBy=%22{SEARCH_SORTS[sort]}%22")
    if since is not None:
        age = now - since
        for limit_days, token in ((1, "past-24h"), (7, "past-week"), (31, "past-month")):
            if age.total_seconds() <= limit_days * 86400:
                return f"{url}&datePosted=%22{token}%22"
    return url


def search_order(rows: list[dict], sort: str) -> list[dict]:
    """Latest: newest first. Relevance/top: LinkedIn's own order, untouched."""
    return newest_first(rows) if SEARCH_SORTS.get(sort) == "date_posted" else rows


def empty_search_reason(query: str, *, cards: int, kept: int, since: datetime | None, page: dict | None) -> str:
    """Why a search that answered gave no rows, for stderr: LinkedIn said no results, every result read was older than
    --since, or no result cards rendered at all."""
    page = page or {}
    if cards and since is not None and not kept:
        return (f"LinkedIn returned {cards} result(s) for {query!r}, but none posted at or after "
                f"{since.strftime('%Y-%m-%dT%H:%M:%SZ')} (--since). Widen --since, or try --sort latest.")
    if page.get("noResults"):
        return f"LinkedIn found no posts for {query!r}" + (" in that --since window." if since else ".")
    return (f"LinkedIn's search page showed no result cards for {query!r} (nothing rendered; the page may have "
            "changed or loaded slowly). Retry once; if it persists, the search page has moved.")


# --- Comments (posts read --comments) ------------------------------------------------------------------------------

COMMENT_FIELDS = ["author_name", "author_handle", "author_headline", "posted_at", "text", "reactions", "replies",
                  "pinned", "url"]


def _ms_time(value: Any) -> str | None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    try:
        when = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _commenter(comment: dict, index: dict[str, dict]) -> tuple[str | None, str | None, str | None]:
    """(name, handle, headline) of a comment's author: a member's miniProfile, or a company's miniCompany."""
    who = comment.get("commenter") if isinstance(comment.get("commenter"), dict) else {}
    profile = _ref(who, "miniProfile", index)
    if profile:
        name = " ".join(p for p in (profile.get("firstName"), profile.get("lastName")) if isinstance(p, str) and p)
        handle = profile.get("publicIdentifier") if isinstance(profile.get("publicIdentifier"), str) else None
        return name or None, handle, profile.get("occupation") if isinstance(profile.get("occupation"), str) else None
    company = _ref(who, "miniCompany", index) or _ref(_get(who, "company") or {}, "miniCompany", index)
    if company:
        universal = company.get("universalName")
        handle = f"{ORIGIN}/company/{quote(universal, safe='')}/" if isinstance(universal, str) and universal else None
        return company.get("name") if isinstance(company.get("name"), str) else None, handle, None
    return None, None, None


def comment_row(comment: dict, index: dict[str, dict]) -> dict[str, Any]:
    name, handle, headline = _commenter(comment, index)
    detail = _ref(comment, "socialDetail", index) or {}
    counts = _ref(detail, "totalSocialActivityCounts", index) or {}
    text = _text(comment.get("commentV2"))
    if text is None:
        values = _get(comment, "comment", "values")
        text = "".join(v.get("value", "") for v in values if isinstance(v, dict)) if isinstance(values, list) else ""
    permalink = comment.get("permalink")
    return {
        "author_name": name,
        "author_handle": handle,
        "author_headline": headline,
        "posted_at": _ms_time(comment.get("createdTime")),
        "text": text or "",
        "reactions": _count(counts.get("numLikes")),
        "replies": _count(counts.get("numComments")),
        "pinned": comment.get("pinned") is True,
        "url": permalink if isinstance(permalink, str) and permalink.startswith(ORIGIN + "/") else None,
    }


def _is_comment(obj: Any) -> bool:
    return str(_get(obj, "$type") or "").endswith(".feed.Comment")


def top_comments(data: Any, urn: str, limit: int) -> list[dict[str, Any]]:
    """The post's top-level comments in LinkedIn's own order (its "Most relevant" sort), at most `limit`. Read from
    the single-update response (updatesV2 ...&commentsCount=N): the post's socialDetail lists them; replies are left
    out."""
    if limit <= 0:
        return []
    index = _index(data)
    post = next((o for o in index.values() if _update_type(o) and _activity(o) == urn), None)
    if post is None:
        post = next((o for o in index.values() if _update_type(o)), None)
    detail = _ref(post, "socialDetail", index) if post else None
    pointers = _get(detail, "comments", "*elements") if detail else None
    if isinstance(pointers, list):
        found = [index[p] for p in pointers if isinstance(p, str) and p in index]
    else:
        found = [o for o in index.values() if _is_comment(o)]
    rows = [comment_row(c, index) for c in found if _is_comment(c) and not c.get("parentCommentUrn")]
    return rows[:limit]


# --- The page's own RSC payload (search / company cards carry no urn in the DOM) ------------------------------------

_FLIGHT_PAIR = re.compile(
    r'updateUrnLegacy\\*"\s*:\s*\\*"urn:li:fsd_update:\(urn:li:activity:(\d{15,25})[^"\\]*\\*"\s*,\s*'
    r'\\*"url\\*"\s*:\s*\\*"(https://www\.linkedin\.com/posts/[^"\\?\s]+)')
_POSTS_VANITY = re.compile(r"/posts/([A-Za-z0-9-]{2,100})_")


def flight_posts(text: str | None) -> list[tuple[str, str]]:
    """(activity urn, permalink) per post, in page order, from the server-rendered payload LinkedIn ships with a
    search or company page (and its read-only pagination responses)."""
    out: dict[str, str] = {}
    for activity, url in _FLIGHT_PAIR.findall(text or ""):
        out.setdefault(f"urn:li:activity:{activity}", url)
    return list(out.items())


def _vanity(url: str | None) -> str | None:
    match = _POSTS_VANITY.search(url or "")
    return match.group(1).lower() if match else None


def attach_flight(rows: list[dict], pairs: list[tuple[str, str]]) -> list[dict]:
    """Give card rows their urn/url (and exact posted_at) from the payload pairs. Cards and pairs come in the same
    order; that is trusted only when the counts agree and every author handle that can be checked matches the
    permalink's vanity. Otherwise only unambiguous handle matches are used; the rest keep what the card showed."""

    def fill(row: dict, urn: str) -> None:
        row["urn"], row["url"] = urn, post_url(urn)
        row["posted_at"] = urn_time(urn) or row["posted_at"]

    def agrees(row: dict, url: str) -> bool:
        vanity, handle = _vanity(url), row.get("author_handle")
        return vanity is None or not handle or "/" in handle or handle.lower() == vanity

    if pairs and len(pairs) == len(rows) and all(agrees(r, u) for r, (_, u) in zip(rows, pairs)):
        for row, (urn, _) in zip(rows, pairs):
            fill(row, urn)
        return rows
    by_vanity: dict[str, list[str]] = {}
    for urn, url in pairs:
        if (vanity := _vanity(url)):
            by_vanity.setdefault(vanity, []).append(urn)
    handles = [r.get("author_handle") for r in rows]
    for row in rows:
        handle = (row.get("author_handle") or "").lower()
        urns = by_vanity.get(handle) if handle and "/" not in handle else None
        if urns and len(urns) == 1 and sum(1 for h in handles if (h or "").lower() == handle) == 1:
            fill(row, urns[0])
    return rows
