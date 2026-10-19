"""Browser-side work for webcli photos: status, albums, the photo grid (library, album, search), one photo's info,
and downloading an original. READ-ONLY.

Everything is read off the rendered page (UI), as google-photos-cli did: album and photo links, and a photo page's
aria-label ("Photo - Portrait - Nov 10, 2016, 11:22:34 AM"). The grid is virtualized, so `photos` collects the links
after every scroll. `download` reads the media URL the photo's page loads (photos.fife.usercontent.google.com/pw/...)
and fetches <base>=dv, then <base>=d, the originals, with the profile's cookies; every hop is checked
(guard.download_url_allowed). The file is written where the command asked, 0600, by whichever process runs this
(the identity daemon, or the command itself).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit

from webcli.core import browser
from webcli.core.errors import AuthRequired, NotFound
from webcli.sites import google
from webcli.sites.photos import parse, selectors as sel
from webcli.sites.photos.endpoints import (
    ALBUM_URL,
    ALBUMS_URL,
    APP_URL,
    INFO_MS,
    MAX_SCROLLS,
    MEDIA_BASE,
    PHOTO_URL,
    PLAY_MS,
    RENDER_MS,
    SCROLL_MS,
    SCROLL_PX,
    SEARCH_URL,
    STALL_SCROLLS,
)
from webcli.sites.photos.guard import POLICY, download_url_allowed

if TYPE_CHECKING:
    from playwright.async_api import Page

SNIFF_HINT = ("If there should be results, Photos' page may have changed; run `python -m webcli.core.capture photos` "
              "and fix webcli/sites/photos/endpoints.py.")
VIDEO_MIN_BYTES = 50_000  # an =dv answer smaller than this (and not video/*) is a poster: try =d

LINKS_JS = """
([selector, kind]) => {
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll(selector)) {
    const m = (a.getAttribute('href') || '').match(new RegExp('/' + kind + '/([^/?#]+)'));
    if (!m || seen.has(m[1])) continue;
    seen.add(m[1]);
    const label = (kind === 'album' ? (a.getAttribute('aria-label') || a.textContent) : a.getAttribute('aria-label'));
    out.push({id: m[1], label: (label || '').trim(), url: 'https://photos.google.com/' + kind + '/' + m[1]});
  }
  return out;
}
"""
INFO_JS = """
([imageSel, labelledSel]) => {
  const labels = [];
  for (const el of document.querySelectorAll(labelledSel)) {
    const v = (el.getAttribute('aria-label') || '').trim();
    if (/^(Photo|Video|Raw photo)\\b/i.test(v) && v.includes(',')) labels.push(v);
  }
  return {title: document.title || '', aria: labels[0] || '', image: !!document.querySelector(imageSel),
          url: location.href};
}
"""


async def status(*, headed: bool = False) -> dict:
    """{signedIn, email}. headed is accepted for the daemon contract; data sites always run headless."""
    return await google.status(POLICY, APP_URL)


async def albums(*, headed: bool = False) -> list[dict]:
    """[{id, title, url}] for the albums the albums page shows. NotFound when it shows none."""
    async with browser.site_page(POLICY) as page:
        await google.open_app(page, ALBUMS_URL)
        await page.wait_for_timeout(RENDER_MS)
        found = await page.evaluate(LINKS_JS, [sel.ALBUM_LINK, "album"])
        if not found:
            raise NotFound("No albums found.", hint=f"{google.blocked().strip()} {SNIFF_HINT}".strip())
    return [{"id": item["id"], "title": parse.clean_title(item["label"]), "url": item["url"]} for item in found]


async def _grid(page: "Page", limit: int) -> list[dict]:
    """Photo links in page order, collected after every scroll (the grid drops tiles that scroll away) until there
    are `limit`, or STALL_SCROLLS scrolls bring nothing new, or MAX_SCROLLS."""
    found: dict[str, dict] = {}
    stalled = 0
    for _ in range(MAX_SCROLLS):
        before = len(found)
        for item in await page.evaluate(LINKS_JS, [sel.PHOTO_LINK, "photo"]):
            found.setdefault(item["id"], item)
        stalled = stalled + 1 if len(found) == before else 0
        if len(found) >= limit or stalled >= STALL_SCROLLS:
            break
        await page.mouse.wheel(0, SCROLL_PX)
        await page.wait_for_timeout(SCROLL_MS)
    return list(found.values())[:limit]


async def photos(*, album: str | None = None, query: str | None = None, limit: int = 50,
                 headed: bool = False) -> list[dict]:
    """[{id, kind, taken_at, label, url}]: an album's photos, a search's results, or (neither) the library's most
    recent, newest first. NotFound when the page shows none."""
    if album:
        url, where = ALBUM_URL.format(id=quote(album, safe="")), f"album {album}"
    elif query:
        url, where = SEARCH_URL.format(query=quote(query, safe="")), f"a search for {query!r}"
    else:
        url, where = APP_URL, "the library"
    async with browser.site_page(POLICY) as page:
        await google.open_app(page, url)
        await page.wait_for_timeout(RENDER_MS)
        found = await _grid(page, limit)
        if not found:
            raise NotFound(f"No photos in {where}.", hint=f"{google.blocked().strip()} {SNIFF_HINT}".strip())
    return [parse.photo(item) for item in found]


async def _photo_page(page: "Page", media_id: str) -> dict:
    """Open a photo's page; NotFound when Photos doesn't show that photo."""
    await google.open_app(page, PHOTO_URL.format(id=quote(media_id, safe="")))
    await page.wait_for_timeout(INFO_MS)
    shown = await page.evaluate(INFO_JS, [sel.PHOTO_IMAGE, sel.LABELLED])
    if media_id not in urlsplit(shown["url"]).path or not (shown["aria"] or shown["image"]):
        raise NotFound(f"No photo {media_id}.", hint="See `webcli photos list --id-only`.")
    return shown


async def info(media_id: str, *, headed: bool = False) -> dict:
    """{id, kind, taken_at, label, url, page_url} from the photo's page."""
    async with browser.site_page(POLICY) as page:
        shown = await _photo_page(page, media_id)
    label = shown["aria"] or shown["title"]
    return {"id": media_id, **parse.described(label), "label": label, "url": PHOTO_URL.format(id=media_id),
            "page_url": shown["url"]}


async def download(media_id: str, out_dir: str, *, headed: bool = False) -> dict:
    """Save the original photo or video into `out_dir` (absolute) under the server's file name, else
    <id>.<ext>; replaces a file of that name. {id, path, bytes, mime}."""
    media: list[str] = []

    def on_request(request) -> None:
        match = MEDIA_BASE.match(request.url)
        if match:
            media.append(match.group(0))

    async with browser.site_page(POLICY) as page:
        page.on("request", on_request)
        await _photo_page(page, media_id)
        try:
            await page.keyboard.press("Space")  # a video loads its media URL once it plays
            await page.wait_for_timeout(PLAY_MS)
        except Exception:
            pass
        base = next(iter(dict.fromkeys(media)), None)
        if base is None:
            raise RuntimeError(f"Photo {media_id}'s page loaded no media URL.{google.blocked()} {SNIFF_HINT}")
        chosen = None
        for suffix in ("=dv", "=d"):  # the video original, then the still original
            status, headers, body = await google.fetch_file(page, base + suffix, download_url_allowed,
                                                            what="the original")
            if status in (401, 403):
                raise AuthRequired(f"Google Photos refused the original of {media_id} (HTTP {status}).")
            video = "video/" in headers.get("content-type", "") or len(body) > VIDEO_MIN_BYTES
            if status == 200 and body and (suffix == "=d" or video):  # a small still from =dv is only a poster
                chosen = (headers, body)
                break
    if chosen is None:
        raise RuntimeError(f"Google Photos answered no original for {media_id}.")
    headers, body = chosen
    mime = headers.get("content-type", "").split(";")[0].strip() or None
    name = google.safe_name(parse.download_name(headers.get("content-disposition"), mime, media_id),
                            f"{media_id}.bin")
    path = google.save_file(out_dir, name, body)
    return {"id": media_id, "path": str(path), "bytes": len(body), "mime": mime}
