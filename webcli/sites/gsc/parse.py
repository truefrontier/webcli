"""Pure functions over Search Console's batchexecute answers (shapes in endpoints.py): no browser, no network.
Tested on synthetic fixtures in tests/test_gsc.py."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from webcli.sites.gemini.parse import AUTH_CODES, NOT_FOUND_CODES, RpcError, at, envelopes, freq_entries
from webcli.sites.gsc.endpoints import REPORT_TZ, STALE_DAYS

__all__ = ["AUTH_CODES", "NOT_FOUND_CODES", "RpcError", "freq_entries", "rpc_payload"]

HELP_PREFIX = "https://support.google.com/webmasters/answer/"
SEVERITIES = {"INFORMATIONAL", "ERROR", "WARNING", "OK"}
KINDS = {5: "domain", 1: "url-prefix"}
# The site's users list (it5yrc) shows code 1 as Owner and 2 as Full; 3 restricted is Search Console's next level.
PERMISSIONS = {1: "owner", 2: "full", 3: "restricted"}

# Page indexing reasons: the report's enum -> (the label Search Console shows, who it says fixes it). Seen live
# 2026-10-30; any other enum falls back to its help anchor or the enum itself, humanized.
REASONS = {
    "NO_INDEX_TAG": ("Excluded by 'noindex' tag", "Website"),
    "ALT_REDIRECT": ("Page with redirect", "Website"),
    "ALTERNATE_WITH_CANONICAL": ("Alternate page with proper canonical tag", "Website"),
    "DISCOVERED": ("Discovered - currently not indexed", "Google systems"),
    "CRAWLED": ("Crawled - currently not indexed", "Google systems"),
    "DUP_USER_CANONICAL": ("Duplicate, Google chose different canonical than user", "Google systems"),
}
# URL Inspection's coverage state: the state code -> label; the help anchor is the fallback.
COVERAGE = {
    1: "Submitted and indexed",
    5: "Discovered - currently not indexed",
    6: "Crawled - currently not indexed",
    19: "Alternate page with proper canonical tag",
    39: "URL is unknown to Google",
}
ANCHORS = {
    "submitted_and_indexed": "Submitted and indexed",
    "indexed_not_submitted_in_sitemap": "Indexed, not submitted in sitemap",
    "discovered__unclear_status": "Discovered - currently not indexed",
    "crawled": "Crawled - currently not indexed",
    "duplicate_page_with_proper_canonical_tag": "Alternate page with proper canonical tag",
    "url_unknown": "URL is unknown to Google",
    "blocked_by_noindex_tag": "Excluded by 'noindex' tag",
    "page_with_redirect": "Page with redirect",
}
CRAWLED_AS = {1: "Googlebot desktop", 2: "Googlebot smartphone"}
SITEMAP_STATUS = {1: "Success"}
SITEMAP_TYPES = {1: "Sitemap"}
SITEMAP_COUNTS = {1: "pages", 2: "videos"}
METRICS = {5: "clicks", 6: "impressions", 7: "ctr", 8: "position"}
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def rpc_payload(body: str, rpcid: str) -> Any:
    """The decoded payload of rpcid's answer (None for an empty one). RpcError for an error code or no answer."""
    for env in envelopes(body):
        if env[0] == "er":
            raise RpcError(rpcid, at(env, 5), f"Search Console rejected {rpcid}.")
        if env[0] != "wrb.fr" or at(env, 1) != rpcid:
            continue
        payload = at(env, 2)
        if isinstance(payload, str):
            return json.loads(payload)
        status = at(env, 5)
        code = status[0] if isinstance(status, list) and status and isinstance(status[0], int) else None
        if code is None:
            return None
        raise RpcError(rpcid, code, f"Search Console answered {rpcid} with error code {code}.")
    raise RpcError(rpcid, None, f"Search Console's answer had no {rpcid} result.")


def freq(rpcid: str, payload: Any) -> str:
    return json.dumps([[[rpcid, json.dumps(payload, separators=(",", ":")), None, "generic"]]], separators=(",", ":"))


def iso_ms(ms: Any) -> str | None:
    """Epoch milliseconds -> ISO 8601 UTC ("...Z"); 0 / None -> None."""
    if not isinstance(ms, (int, float)) or isinstance(ms, bool) or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def report_day(ms: Any) -> str | None:
    """A Pages-report day (epoch ms, midnight UTC) -> its date in Search Console's reporting time zone, as the
    report's own "Last update" shows it."""
    if not isinstance(ms, (int, float)) or isinstance(ms, bool) or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=ZoneInfo(REPORT_TZ)).date().isoformat()


def report_today() -> date:
    return datetime.now(ZoneInfo(REPORT_TZ)).date()


def humanize(text: str) -> str:
    words = re.sub(r"_+", " ", text or "").strip().lower()
    return words[:1].upper() + words[1:]


def walk(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, list):
        for v in value:
            yield from walk(v)


# Properties

def normalize_property(value: str) -> str:
    """A property as Search Console names it: sc-domain:example.com (a bare domain is a Domain property), or a
    URL-prefix property https://www.example.com/ (always with its trailing slash)."""
    text = (value or "").strip()
    if not text:
        raise ValueError("The property is empty.")
    if text.lower().startswith("sc-domain:"):
        host = text.split(":", 1)[1].strip().lower().rstrip("/")
        if not host or "/" in host:
            raise ValueError(f"Not a domain property: {value!r}.")
        return f"sc-domain:{host}"
    if "://" in text:
        parts = urlsplit(text)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"Not a property: {value!r}.")
        return text if text.endswith("/") else text + "/"
    if "/" in text or " " in text or "." not in text:
        raise ValueError(f"Not a property: {value!r} (use sc-domain:example.com or https://www.example.com/).")
    return f"sc-domain:{text.lower()}"


def properties(payload: Any) -> list[dict]:
    """BWF8he -> [{property, type, permission, permissionCode, displayName}]."""
    rows = at(payload, 0) or []
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not (isinstance(row, list) and row and isinstance(row[0], str)):
            continue
        kind, perm = at(row, 1), at(row, 2)
        out.append({"property": row[0], "type": KINDS.get(kind, f"kind {kind}"),
                    "permission": PERMISSIONS.get(perm, f"code {perm}"), "permissionCode": perm,
                    "displayName": at(row, 4) or row[0]})
    return out


def url_in_property(url: str, prop: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        return False
    if prop.startswith("sc-domain:"):
        domain = prop.split(":", 1)[1]
        return host == domain or host.endswith("." + domain)
    return url.startswith(prop)


def property_for(url: str, props: list[str]) -> str | None:
    """The property a URL belongs to: the longest URL-prefix property that prefixes it, else the most specific
    Domain property covering its host."""
    prefixes = sorted((p for p in props if not p.startswith("sc-domain:") and url_in_property(url, p)), key=len)
    if prefixes:
        return prefixes[-1]
    domains = sorted((p for p in props if p.startswith("sc-domain:") and url_in_property(url, p)), key=len)
    return domains[-1] if domains else None


# Page indexing

def index_counts(payload: Any) -> dict:
    """OLiH4d (view 30) -> {indexed, notIndexed, asOf}: the last day with data. Columns are named in the answer
    (INFORMATIONAL = not indexed, OK = indexed); the defaults are their order as seen."""
    names: list[str] = []
    for v in walk(payload):
        if (isinstance(v, list) and v and all(isinstance(c, list) and c and isinstance(c[0], str) for c in v)
                and {c[0] for c in v} >= {"INFORMATIONAL", "OK"}):
            names = [c[0] for c in v]
            break
    col = {n: i for i, n in enumerate(names)} or {"INFORMATIONAL": 0, "OK": 1}
    points = at(payload, 1, 0) or []
    for point in reversed(points if isinstance(points, list) else []):
        values = at(point, 1)
        if not isinstance(values, list):
            continue
        ni, ok = at(values, col["INFORMATIONAL"]), at(values, col["OK"])
        if isinstance(ni, (int, float)) and isinstance(ok, (int, float)):
            day = report_day(at(point, 0))
            return {"indexed": int(ok), "notIndexed": int(ni), "lastUpdated": day, "asOf": day}
    return {"indexed": None, "notIndexed": None, "lastUpdated": None, "asOf": None}


def staleness(last_updated: str | None, crawls: list[str | None], today: date) -> tuple[bool, list[str]]:
    """(stale, why) for the Pages report: its last update is more than STALE_DAYS days old, or an example URL was
    crawled after it (so the report predates what Google already knows about that URL)."""
    if not last_updated:
        return True, ["Search Console's Pages report has no last-update date."]
    updated = date.fromisoformat(last_updated)
    why = []
    age = (today - updated).days
    if age > STALE_DAYS:
        why.append(f"last updated {last_updated}, {age} days ago")
    days = [report_day(datetime.strptime(c, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() * 1000)
            for c in crawls if c]
    newer = sorted(d for d in days if d and d > last_updated)
    if newer:
        why.append(f"{len(newer)} example URL(s) crawled after it, up to {newer[-1]}")
    return bool(why), why


def stale_warning(prop: str, why: list[str]) -> str:
    return (f"{prop}: the Pages report is stale ({'; '.join(why)}). Its counts lag URL Inspection by days; check "
            "single URLs with `webcli gsc inspect URL`.")


def _reason_enum(row: Any) -> tuple[str | None, str | None]:
    """(enum, help anchor) inside a reason row."""
    enum = anchor = None
    for v in walk(row):
        if anchor is None and isinstance(v, str) and v.startswith(HELP_PREFIX) and "#" in v:
            anchor = v.split("#", 1)[1]
        if (enum is None and isinstance(v, list) and len(v) > 2 and v[0] is None and v[1] in SEVERITIES
                and isinstance(v[2], str) and v[2].isupper()):
            enum = v[2]
    return enum, anchor


def reasons(payload: Any) -> list[dict]:
    """nDAfwb view 35 -> [{reason, source, enum, key, pages}], in Search Console's order."""
    out = []
    for block in at(payload, 1, 0) or []:
        row = at(block, 0)
        if not isinstance(row, list) or not row:
            continue
        key = at(row, 1, 0)
        count = at(row[-1], 1) if isinstance(row[-1], list) else None
        enum, anchor = _reason_enum(row)
        label, source = REASONS.get(enum or "", (None, None))
        if label is None:
            label = ANCHORS.get(anchor or "") or humanize(anchor or enum or str(key))
        out.append({"reason": label, "source": source, "enum": enum, "key": key,
                    "pages": count if isinstance(count, int) else 0, "severity": at(row, 0, 0)})
    return out


def reason_urls(payload: Any) -> list[dict]:
    """nDAfwb view 33 -> [{url, lastCrawled}]."""
    out = []
    for item in at(payload, 1, 0) or []:
        url = at(item, 0, 0, 0)
        if isinstance(url, str):
            sec = at(item, 0, 1, 1)
            out.append({"url": url, "lastCrawled": iso_ms(sec * 1000) if isinstance(sec, (int, float)) else None})
    return out


# Sitemaps

def sitemaps(payload: Any) -> list[dict]:
    """vyvePb -> [{sitemap, type, submitted, lastRead, status, discoveredPages, discoveredVideos, errors,
    warnings}]."""
    out = []
    for row in at(payload, 0) or []:
        if not (isinstance(row, list) and row and isinstance(row[0], str)):
            continue
        counts = {SITEMAP_COUNTS.get(at(c, 0), f"kind{at(c, 0)}"): at(c, 1) for c in (at(row, 4) or [])
                  if isinstance(c, list)}
        status = at(row, 8)
        out.append({"sitemap": row[0], "type": SITEMAP_TYPES.get(at(row, 1), f"type {at(row, 1)}"),
                    "submitted": iso_ms(at(row, 2)), "lastRead": iso_ms(at(row, 3)),
                    "status": SITEMAP_STATUS.get(status, "Couldn't fetch" if status in (2, 3) else f"code {status}"),
                    "statusCode": status, "discoveredPages": counts.get("pages", 0),
                    "discoveredVideos": counts.get("videos", 0), "errors": at(row, 5) or 0,
                    "warnings": at(row, 6) or 0})
    return out


def check_sitemap_url(sitemap: str, prop: str) -> str:
    text = (sitemap or "").strip()
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname or "@" in parts.netloc:
        raise ValueError(f"Not a sitemap URL: {sitemap!r} (use https://...).")
    if not url_in_property(text, prop):
        raise ValueError(f"{text} isn't inside {prop}; Search Console only takes sitemaps in the property.")
    return text


# URL Inspection

def job_id(payload: Any) -> str:
    job = at(payload, 0)
    if not isinstance(job, str) or not job:
        raise RpcError("RVtklb", None, "Search Console started no inspection.")
    return job


def job_done(payload: Any) -> bool:
    return at(payload, 1) == 2


def inspection(payload: Any, url: str, prop: str) -> dict:
    """HZPzjb -> the Page indexing block of URL Inspection."""
    verdict = at(payload, 0, 0)
    state = at(payload, 9)
    help_url = at(payload, 14)
    anchor = help_url.split("#", 1)[1] if isinstance(help_url, str) and "#" in help_url else None
    coverage = COVERAGE.get(state) or ANCHORS.get(anchor or "") or (humanize(anchor) if anchor else None)
    user_kind, google = at(payload, 12), at(payload, 15)
    user_canonical = url if user_kind == 1 else (at(payload, 8, 0) if user_kind == 2 else None)
    google_flag = at(google, 1)
    google_canonical = (at(google, 0) or url) if google_flag == 1 else (at(google, 0) if google_flag == 2 else None)
    sitemaps_ = [s for s in (at(payload, 2) or []) if isinstance(s, str)]
    referring = [s for s in (at(payload, 3) or []) if isinstance(s, str)]
    allowed, fetched = at(payload, 6), at(payload, 7)
    return {
        "url": url, "property": prop,
        "onGoogle": verdict == 1, "verdict": "URL is on Google" if verdict == 1 else "URL is not on Google",
        "coverageState": coverage, "coverageCode": state,
        "lastCrawl": iso_ms(at(payload, 1)), "crawledAs": CRAWLED_AS.get(at(payload, 5, 2)),
        "crawlAllowed": True if allowed == 1 else None, "pageFetch": "Successful" if fetched == 1 else None,
        "userCanonical": user_canonical or None, "googleCanonical": google_canonical or None,
        "inSitemap": bool(sitemaps_), "sitemaps": sitemaps_, "referringUrls": referring,
        "helpUrl": help_url if isinstance(help_url, str) and help_url.startswith(HELP_PREFIX) else None,
    }


PASSED_THROUGH = "This is Google's stored inspection data, passed through as is; it can lag or disagree with the " \
                 "live sitemap."


def is_sitemap_url(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return "sitemap" in path or path.endswith((".xml", ".xml.gz"))


def inspection_notes(info: dict) -> list[str]:
    """Plain notes where URL Inspection's sitemap fields look contradictory (they are Google's, unchanged)."""
    notes = []
    indexed = info.get("coverageCode") == 1 or info.get("onGoogle")
    if indexed and (not info.get("inSitemap") or not info.get("sitemaps")):
        notes.append(f"inSitemap is false and sitemaps is empty, though coverage is '{info.get('coverageState')}'. "
                     f"{PASSED_THROUGH}" + ("" if "inLiveSitemap" in info else " Check with --check-live-sitemap."))
    sitemap_refs = [u for u in info.get("referringUrls") or [] if is_sitemap_url(u)]
    if sitemap_refs:
        notes.append(f"referringUrls includes {', '.join(sitemap_refs)}: Google lists a sitemap it found the URL "
                     f"in as a referring page. {PASSED_THROUGH}")
    live = info.get("inLiveSitemap")
    if live is True and not info.get("inSitemap"):
        notes.append("The live submitted sitemap lists this URL now; Google's stored data (inSitemap false) hasn't "
                     "caught up.")
    elif live is False and info.get("inSitemap"):
        notes.append("Google's stored data puts this URL in a sitemap, but the live submitted sitemaps don't list "
                     "it now.")
    return notes


# Performance

def check_date(text: str, name: str) -> date:
    if not _DATE.fullmatch(text or ""):
        raise ValueError(f"{name} must be YYYY-MM-DD, not {text!r}.")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{name} isn't a date: {text!r}.") from None


def performance_request(prop: str, dim: int, start: date, end: date) -> list:
    """nDAfwb's view-32 payload: one dimension, the date range, web search, as the Performance page asks."""
    return [prop, 32, None, None, None,
            [[dim], None, [[start.strftime("%Y%m%d"), end.strftime("%Y%m%d")]], [[6, ["WEB"]]],
             None, None, None, None, None, None, 1, None, None, 2, None, 1]]


def performance_series_request(prop: str, start: date, end: date) -> list:
    """OLiH4d's view-27 payload: the Performance chart (by day) for the range, web search; its answer carries the
    range's totals and the data's last update."""
    return [prop, 27, None, None, None,
            [[1], None, [[start.strftime("%Y%m%d"), end.strftime("%Y%m%d")]], [[6, ["WEB"]]],
             None, None, None, None, None, None, 1, None, None, 2, None, 1], None, None, [None, 2]]


def performance_totals(payload: Any) -> dict:
    """OLiH4d view 27 -> {totals: {clicks, impressions, ctr, position}, lastUpdated}: Search Console's own totals for
    the whole range (anonymized queries included), as the Performance page's cards show them. Columns are named in
    the answer (CLICKS, IMPRESSIONS, CTR, POSITION); the defaults are their order as seen."""
    names = [at(c, 0) for c in (at(payload, 6, 2) or []) if isinstance(c, list)]
    col = {n: i for i, n in enumerate(names) if isinstance(n, str)} or \
        {"CLICKS": 0, "IMPRESSIONS": 1, "CTR": 2, "POSITION": 3}
    values = at(payload, 1, 1, 1)
    if not isinstance(values, list):
        raise ValueError("no totals in the Performance answer")

    def num(name):
        v = at(values, col[name]) if name in col else None
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    clicks, impressions, position = int(num("CLICKS") or 0), int(num("IMPRESSIONS") or 0), num("POSITION")
    return {"totals": {"clicks": clicks, "impressions": impressions,
                       "ctr": round(clicks / impressions, 4) if impressions else 0.0,
                       "position": round(position, 2) if impressions and position is not None else None},
            "lastUpdated": iso_ms(at(payload, 1, 3))}


def _key(key: Any, dimension: str) -> Any:
    if dimension == "date":
        ms = at(key, 6)
        day = iso_ms(ms)
        return day[:10] if day else None
    if dimension == "page":
        value = at(key, 40)
        if isinstance(value, str):
            return value
    value = at(key, 0)
    if isinstance(value, str):
        return value
    return next((v for v in (key if isinstance(key, list) else []) if isinstance(v, str)), None)


def performance(payload: Any, dimension: str) -> list[dict]:
    """nDAfwb view 32 -> [{<dimension>, clicks, impressions, ctr, position}]. CTR is clicks / impressions (a
    fraction, 0..1), computed here; position is Search Console's average position, 2 decimals (null with no
    impressions)."""
    out = []
    for item in at(payload, 1, 0) or []:
        row = at(item, 0)
        if not isinstance(row, list) or not row:
            continue
        metrics: dict[str, Any] = {}
        for entry in row[1:]:
            if not isinstance(entry, list) or len(entry) < 9:
                continue
            name = METRICS.get(entry[8])
            if name in ("clicks", "impressions"):
                metrics[name] = entry[1] if isinstance(entry[1], (int, float)) else 0
            elif name:  # the value sits past the metric id (index 43 / 44); a day with no data has none
                values = [v for v in entry[9:] if isinstance(v, (int, float)) and not isinstance(v, bool)]
                metrics[name] = values[-1] if values else None
        clicks, impressions = int(metrics.get("clicks") or 0), int(metrics.get("impressions") or 0)
        position = metrics.get("position") if impressions else None
        out.append({dimension: _key(row[0], dimension), "clicks": clicks, "impressions": impressions,
                    "ctr": round(clicks / impressions, 4) if impressions else 0.0,
                    "position": round(position, 2) if isinstance(position, (int, float)) else None})
    if dimension == "date":
        out.sort(key=lambda r: r["date"] or "")
    else:
        out.sort(key=lambda r: (-r["clicks"], -r["impressions"]))
    return out
