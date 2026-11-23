"""Read a profile's Photos tab and download the images. READ-ONLY.

The photos tab and a photo page are full page loads. Their own GraphQL responses (and the same JSON
embedded in the HTML) are parsed; a photo link in the DOM is only used when that JSON has no photos.
Downloading fetches the image bytes from Facebook's CDN through Chrome, so the session cookies never
leave the browser and are never printed. Nothing here clicks, types, or submits.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from webcli.core import browser, guard
from webcli.core.errors import AuthRequired, NotFound, UsageError
from webcli.core.output import write_atomic
from webcli.sites import google
from webcli.sites.facebook import faces, fetch, parse
from webcli.sites.facebook.endpoints import (
    DOWNLOAD_PAUSE,
    FRIEND_SEARCH_QUERIES,
    PHOTO_MAX,
    PHOTO_MAX_SCROLLS,
    PHOTO_QUERIES,
    SCROLL_PAUSE,
    STALL_SCROLLS,
)
from webcli.sites.facebook.guard import image_url_allowed, read_policy

if TYPE_CHECKING:
    from playwright.async_api import Page

DOC_HTML_JS = "() => document.documentElement ? document.documentElement.outerHTML : ''"
_EXT = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}


class ResponseCapture:
    """GraphQL bodies the page itself requested. The guard has already refused anything that isn't a read."""

    def __init__(self) -> None:
        self.payloads: list = []
        self._tasks: set[asyncio.Task] = set()

    def attach(self, page: "Page") -> None:
        def on_response(response) -> None:
            if not _graphql(response):
                return
            task = asyncio.ensure_future(self._read(response))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        page.on("response", on_response)

    async def _read(self, response) -> None:
        if getattr(response, "status", 0) != 200:
            return
        data = await _response_json(response)
        if isinstance(data, dict):
            self.payloads.append(data)
        elif isinstance(data, list):
            self.payloads.extend(item for item in data if isinstance(item, (dict, list)))

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def take(self) -> list:
        found, self.payloads = self.payloads, []
        return found


def _graphql(response) -> bool:
    parts = urlsplit(getattr(response, "url", "") or "")
    return (parts.hostname or "").lower() == "www.facebook.com" and parts.path in ("/api/graphql/", "/api/graphql")


async def _response_json(response):
    try:
        data = await response.json()
    except Exception:
        data = None
    if isinstance(data, (dict, list)):
        return data
    try:
        text = await response.text()
    except Exception:
        return None
    text = (text or "").strip()
    if text.startswith("for (;;);"):
        text = text[9:].strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text or None


def _passes(photo: dict, since: str | None, solo: bool, subject: dict) -> bool:
    if since and not parse.on_or_after(photo, since):
        return False
    if solo and not parse.is_solo(photo, subject):
        return False
    return True


async def _gather(page: "Page", url: str, *, limit: int | None, accept) -> dict:
    """Scroll `url` until `accept` has `limit` hits, the tab stops growing, or a cap is hit.

    `accept(payloads, html) -> (rows, raw_count)` in page order. raw_count is how many new underlying
    records this pass saw, which is what decides that the tab has ended (a filter can hide every row
    of a pass that still loaded more).
    """
    capture = ResponseCapture()
    capture.attach(page)
    await fetch.open_fb(page, url)
    stall = scrolls = 0
    html_seen = False
    rows: list = []
    seen = 0
    stopped_by_limit = False
    while True:
        await capture.drain()
        html = await page.evaluate(DOC_HTML_JS)
        html_seen = html_seen or bool(html)
        if html and parse.unavailable(html):
            raise NotFound("That profile isn't available to you (deleted, private, or blocked).",
                           hint="Check the id with `webcli facebook friends search NAME`.")
        payloads = capture.take()
        if html:
            payloads.append(html)
        fresh_rows, raw_new = accept(payloads, html or "")
        for row in fresh_rows:
            rows.append(row)
        seen += raw_new
        if limit and len(rows) >= limit:
            rows = rows[:limit]
            stopped_by_limit = True
            break
        if seen >= PHOTO_MAX:
            break
        stall = 0 if raw_new else stall + 1
        if stall >= STALL_SCROLLS:
            break
        if scrolls >= PHOTO_MAX_SCROLLS:
            break
        await page.evaluate(fetch.SCROLL_JS, fetch.SELECTORS["main"])
        scrolls += 1
        await fetch.pause(SCROLL_PAUSE)
    complete = stopped_by_limit or (html_seen and not guard.state.blocked and seen < PHOTO_MAX
                                    and scrolls < PHOTO_MAX_SCROLLS)
    return {"rows": rows, "scrolls": scrolls, "complete": complete, "blocked": fetch.blocked_summary(),
            "html": html or ""}


async def friend_search(name: str, limit: int = 20, *, headed: bool = False) -> dict:
    """Friends whose name contains every word of `name`. {friends, scrolls, complete, blocked}."""
    query = (name or "").strip()
    if not query:
        raise UsageError("A name is required.", hint="webcli facebook friends search 'Jane Doe'")
    found: dict[str, dict] = {}

    def accept(payloads, html: str) -> tuple[list[dict], int]:
        hits = parse.friend_hits([*payloads, html], query)
        fresh = []
        for hit in hits:
            key = hit["id"] or hit["profileUrl"]
            if not key or key in found:
                continue
            found[key] = hit
            fresh.append(hit)
        return fresh, len(fresh)

    async with browser.site_page(read_policy(FRIEND_SEARCH_QUERIES)) as page:
        result = await _gather(page, parse.friends_search_url(query), limit=limit, accept=accept)
    if not result["rows"]:
        if result["blocked"]:
            raise RuntimeError("No friends parsed, and the guard blocked requests the search page sent. "
                               f"{fetch.SELECTOR_HINT} Blocked: {'; '.join(result['blocked'][:6])}")
        raise NotFound(f"No friend named {query!r}.",
                       hint="The match is every word of the name, and only people you're friends with.")
    return {"friends": result["rows"], "scrolls": result["scrolls"], "complete": result["complete"],
            "blocked": result["blocked"]}


async def _collect(page: "Page", who: parse.Person, *, mode: str, limit: int | None, since: str | None,
                   solo: bool) -> dict:
    photos: dict[str, dict] = {}
    subject: dict = {}
    shown: set[str] = set()

    def accept(payloads, html: str) -> tuple[list[dict], int]:
        nonlocal subject
        batch = parse.photos_from(payloads)
        if not subject:
            subject = parse.photo_subject(who, page.url, payloads, html)
        fresh_raw = 0
        for photo in batch:
            current = photos.get(photo["id"])
            if current is None:
                fresh_raw += 1
                photos[photo["id"]] = photo
            else:
                chosen = parse.prefer_photo(current, photo)
                if chosen is not current:
                    current.clear()
                    current.update(chosen)  # rows already handed out point at this dict
        matching = [item for item in photos.values() if item["id"] not in shown and _passes(item, since, solo, subject)]
        for item in matching:
            shown.add(item["id"])
        return matching, fresh_raw

    result = await _gather(page, parse.photos_url(who, mode), limit=limit, accept=accept)
    if not subject:
        subject = parse.photo_subject(who, page.url, [], result["html"])
    return {**result, "photos": result.pop("rows"), "subject": subject, "mode": mode}


async def _file_bytes(page: "Page", url: str) -> tuple[bytes, str]:
    """GET one CDN image through Chrome. Errors name the host and path only, never the query or a cookie."""
    status, headers, body = await google.fetch_file(page, url, image_url_allowed, what="the photo")
    ctype = (headers.get("content-type") or "").split(";")[0].strip().lower()
    if status in (401, 403):
        raise AuthRequired("Facebook refused the photo file. The session may have expired.")
    where = guard.describe("GET", url)
    if status != 200 or not body or "text/html" in ctype or not ctype.startswith("image/"):
        raise RuntimeError(f"Facebook didn't return an image (HTTP {status}, {ctype or 'no type'}, {where}).")
    return body, ctype


async def _full_image(page: "Page", photo: dict) -> tuple[str, bytes, str]:
    """The photo page's largest rendition, then its bytes. One polite pause per photo."""
    await fetch.pause(DOWNLOAD_PAUSE)
    capture = ResponseCapture()
    capture.attach(page)
    await fetch.open_fb(page, photo["photoUrl"])
    await capture.drain()
    html = await page.evaluate(DOC_HTML_JS)
    payloads = capture.take()
    if html:
        payloads.append(html)
    url = parse.best_image_for(payloads, photo["id"], photo.get("imageUrl"))
    if not url:
        raise RuntimeError(f"Photo {photo['id']} has no image URL. {fetch.SELECTOR_HINT}")
    body, ctype = await _file_bytes(page, url)
    return url, body, ctype


def _ext(ctype: str) -> str:
    return _EXT.get(ctype, "jpg")


async def list_photos(who: dict, *, mode: str = "of", limit: int | None = None, since: str | None = None,
                      solo: bool = False, max_faces: int | None = None, headed: bool = False) -> dict:
    """Photos from the tab. With max_faces, each kept photo was counted on the downloaded image."""
    if max_faces is not None:
        faces.require()
    person = fetch.person(who)
    async with browser.site_page(read_policy(PHOTO_QUERIES)) as page:
        result = await _collect(page, person, mode=mode, limit=limit, since=since, solo=solo)
        photos = result["photos"]
        dropped = 0
        if max_faces is not None:
            kept = []
            for photo in photos:
                if limit and len(kept) >= limit:
                    break
                try:
                    url, body, _ctype = await _full_image(page, photo)
                    count = faces.count_faces(body)
                except AuthRequired:
                    raise
                except (RuntimeError, ValueError, OSError):
                    dropped += 1
                    continue
                if count > max_faces:
                    dropped += 1
                    continue
                kept.append({**photo, "imageUrl": url, "faceCount": count})
            photos = kept
        elif limit:
            photos = photos[:limit]
    if not photos:
        if not result["photos"] and result["blocked"]:
            raise RuntimeError("No photos parsed, and the guard blocked requests the photos tab sent. "
                               f"{fetch.SELECTOR_HINT} Blocked: {'; '.join(result['blocked'][:6])}")
        hint = (f"{dropped} photos were over {max_faces} faces or unreadable." if dropped
                else "Try without --solo or --since, or check the profile with `webcli facebook friends search`.")
        raise NotFound(f"No photos of {person.key} match.", hint=hint)
    return {"photos": photos, "subject": _public_subject(result["subject"]), "scrolls": result["scrolls"],
            "complete": result["complete"], "blocked": result["blocked"], "mode": mode, "dropped": dropped}


def _public_subject(subject: dict) -> dict:
    return {key: subject.get(key) for key in ("id", "name", "username", "profileUrl", "key")}


def _manifest_path(out: Path) -> Path:
    return out / "manifest.json"


def _read_manifest(path: Path) -> dict:
    if not path.is_file():
        return {"photos": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"photos": []}
    return data if isinstance(data, dict) else {"photos": []}


def _write_manifest(path: Path, data: dict) -> None:
    write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _existing_file(out: Path, photo_id: str) -> Path | None:
    matches = [path for path in out.glob(f"{photo_id}.*") if path.is_file() and path.suffix != ".json"
               and path.stat().st_size > 0 and not path.name.startswith(".")]
    return matches[0] if matches else None


def _entry(photo: dict, *, file: str | None, size: int, kept: bool, face_count) -> dict:
    return {
        "id": photo["id"], "file": file, "bytes": size, "imageUrl": photo.get("imageUrl"),
        "caption": photo.get("caption"), "createdTime": photo.get("createdTime"), "owner": photo.get("owner"),
        "photoUrl": photo.get("photoUrl"), "tags": photo.get("tags") or [], "tagCount": photo.get("tagCount") or 0,
        "faceCount": face_count, "kept": kept,
    }


async def download(who: dict, out_dir: str, *, mode: str = "of", limit: int | None = None, since: str | None = None,
                   solo: bool = False, max_faces: int | None = None, headed: bool = False) -> dict:
    """Save full-size images and manifest.json. Skips a file already on disk; resumes after a stop."""
    if max_faces is not None:
        faces.require()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = _manifest_path(out)
    previous = {item["id"]: item for item in _read_manifest(manifest_path).get("photos") or []
                if isinstance(item, dict) and item.get("id")}
    person = fetch.person(who)
    downloaded = skipped = dropped = 0
    async with browser.site_page(read_policy(PHOTO_QUERIES)) as page:
        result = await _collect(page, person, mode=mode, limit=limit, since=since, solo=solo)
        photos = result["photos"]
        entries: list[dict] = []
        for photo in photos:
            prior = previous.get(photo["id"]) or {}
            existing = _existing_file(out, photo["id"])
            have_count = prior.get("faceCount") is not None
            if existing and (max_faces is None or have_count):
                count = prior.get("faceCount")
                keep = True if max_faces is None else count <= max_faces
                if keep:
                    skipped += 1
                    entries.append(_entry(photo, file=existing.name, size=existing.stat().st_size, kept=True,
                                          face_count=count))
                else:
                    existing.unlink()
                    dropped += 1
                    entries.append(_entry(photo, file=None, size=0, kept=False, face_count=count))
                _save(manifest_path, result, entries, previous, max_faces, since, solo)
                continue
            if existing and max_faces is not None and not have_count:
                try:
                    count = faces.count_faces(existing.read_bytes())
                except (ValueError, OSError):
                    count = None
                if count is not None and count <= max_faces:
                    skipped += 1
                    entries.append(_entry(photo, file=existing.name, size=existing.stat().st_size, kept=True,
                                          face_count=count))
                    _save(manifest_path, result, entries, previous, max_faces, since, solo)
                    continue
                if count is not None:
                    existing.unlink()
                    dropped += 1
                    entries.append(_entry(photo, file=None, size=0, kept=False, face_count=count))
                    _save(manifest_path, result, entries, previous, max_faces, since, solo)
                    continue
            try:
                url, body, ctype = await _full_image(page, photo)
            except AuthRequired:
                raise
            except (RuntimeError, OSError) as exc:
                dropped += 1
                entries.append({**_entry(photo, file=None, size=0, kept=False, face_count=None), "error": str(exc)})
                _save(manifest_path, result, entries, previous, max_faces, since, solo)
                continue
            count = None
            if max_faces is not None:
                try:
                    count = faces.count_faces(body)
                except ValueError:
                    count = None
                if count is None or count > max_faces:
                    dropped += 1
                    entries.append(_entry({**photo, "imageUrl": url}, file=None, size=0, kept=False, face_count=count))
                    _save(manifest_path, result, entries, previous, max_faces, since, solo)
                    continue
            name = f"{photo['id']}.{_ext(ctype)}"
            path = google.save_file(out, name, body)
            downloaded += 1
            entries.append(_entry({**photo, "imageUrl": url}, file=path.name, size=len(body), kept=True,
                                  face_count=count))
            _save(manifest_path, result, entries, previous, max_faces, since, solo)
    kept = sum(1 for item in entries if item.get("kept"))
    if not entries:
        if not result["photos"] and result["blocked"]:
            raise RuntimeError("No photos parsed, and the guard blocked requests the photos tab sent. "
                               f"{fetch.SELECTOR_HINT} Blocked: {'; '.join(result['blocked'][:6])}")
        raise NotFound(f"No photos of {person.key} match.",
                       hint="Try without --solo or --since, or check the profile with `webcli facebook friends search`.")
    return {"dir": str(out), "manifest": str(manifest_path), "downloaded": downloaded, "skipped": skipped,
            "dropped": dropped, "kept": kept, "complete": result["complete"], "blocked": result["blocked"],
            "mode": mode, "solo": solo, "maxFaces": max_faces}


def _save(path: Path, result: dict, entries: list[dict], previous: dict, max_faces, since, solo) -> None:
    by_id = dict(previous)
    for item in entries:
        by_id[item["id"]] = item
    ordered = list(entries) + [item for item in previous.values() if item["id"] not in {row["id"] for row in entries}]
    _write_manifest(path, {
        "profile": _public_subject(result["subject"]),
        "mode": result["mode"],
        "solo": solo,
        "since": since,
        "maxFaces": max_faces,
        "photos": ordered if ordered else list(by_id.values()),
    })
