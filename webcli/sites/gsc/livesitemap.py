"""`inspect --check-live-sitemap`: read the property's submitted sitemaps as they are live on the site, to set
Google's stored inSitemap beside what the sitemaps list now.

A plain anonymous HTTPS GET from this machine (no browser, no Google session, no cookies), and only for sitemap
URLs inside the property: Search Console's submitted sitemaps, and the child sitemaps a sitemap index names. Capped
in files, bytes, and time (endpoints.LIVE_SITEMAP_*). A redirect off the property is refused.
"""

from __future__ import annotations

import gzip
import urllib.request
import xml.etree.ElementTree as ET
from typing import Callable, Iterable

from webcli import __version__
from webcli.sites.gsc.endpoints import LIVE_SITEMAP_MAX_BYTES, LIVE_SITEMAP_MAX_FILES, LIVE_SITEMAP_TIMEOUT_S
from webcli.sites.gsc.parse import url_in_property

USER_AGENT = f"webcli-gsc/{__version__} (sitemap check)"


def parse_sitemap(body: bytes) -> tuple[list[str], list[str]]:
    """A sitemap (or sitemap index) body, gzipped or not -> (page URLs, child sitemap URLs)."""
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)[:LIVE_SITEMAP_MAX_BYTES + 1]
    if len(body) > LIVE_SITEMAP_MAX_BYTES:
        raise ValueError("bigger than the 20 MB cap")
    root = ET.fromstring(body)
    kind = root.tag.rsplit("}", 1)[-1]
    locs = [(el.text or "").strip() for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "loc"]
    locs = [u for u in locs if u]
    if kind == "sitemapindex":
        return [], locs
    if kind != "urlset":
        raise ValueError(f"not a sitemap (root <{kind}>)")
    return locs, []


def _get(url: str, prop: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/xml,text/xml,*/*"})
    with urllib.request.urlopen(req, timeout=LIVE_SITEMAP_TIMEOUT_S) as res:  # noqa: S310 (https/http, checked)
        final = res.geturl()
        if not url_in_property(final, prop):
            raise ValueError(f"redirected off the property to {final}")
        body = res.read(LIVE_SITEMAP_MAX_BYTES + 1)
    if len(body) > LIVE_SITEMAP_MAX_BYTES:
        raise ValueError("bigger than the 20 MB cap")
    return body


def check(url: str, sitemaps: Iterable[str], prop: str, get: Callable[[str, str], bytes] = _get) -> dict:
    """{inLiveSitemap, liveSitemaps (the files listing url), liveSitemapsRead, liveSitemapUrls, liveSitemapErrors}.
    inLiveSitemap is None when no sitemap could be read."""
    queue = [s for s in sitemaps if url_in_property(s, prop)]
    seen: set[str] = set()
    found, errors, total, read = [], [], 0, 0
    while queue and len(seen) < LIVE_SITEMAP_MAX_FILES:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        if not url_in_property(sm, prop):
            errors.append({"sitemap": sm, "error": "outside the property; not fetched"})
            continue
        try:
            pages, children = parse_sitemap(get(sm, prop))
        except Exception as exc:  # network, HTTP, XML: report it, keep going
            errors.append({"sitemap": sm, "error": str(exc)[:200] or type(exc).__name__})
            continue
        read += 1
        total += len(pages)
        if url in pages:
            found.append(sm)
        queue.extend(c for c in children if c not in seen)
    if queue:
        errors.append({"sitemap": None, "error": f"stopped after {LIVE_SITEMAP_MAX_FILES} sitemap files"})
    return {"inLiveSitemap": (bool(found) if read else None), "liveSitemaps": found, "liveSitemapsRead": read,
            "liveSitemapUrls": total, "liveSitemapErrors": errors}
