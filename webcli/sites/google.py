"""What the Google data sites (keep, photos, youtube, recorder) share on the browser side: Google's own sign-in and
static hosts for their guards, opening the app page and telling a signed-out profile from a signed-in one, and
fetching and saving a file whose every redirect hop is checked (page.request skips the route guard).

They all run as google:<account>, the identity gemini's login signs in. None has a login of its own.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from urllib.parse import urljoin, urlsplit

from webcli.core import browser, guard
from webcli.core.errors import AuthRequired
from webcli.core.guard import Policy, host_matches

if TYPE_CHECKING:
    from playwright.async_api import Page

# GET/HEAD every Google data site may load besides its own hosts: sign-in and session checks, scripts, fonts,
# avatars and thumbnails, the account menu.
INFRA_HOSTS = ("accounts.google.com", "*.gstatic.com", "fonts.googleapis.com", "apis.google.com", "ogs.google.com",
               "*.googleusercontent.com", "www.google.com")
# GETs with side effects: aborted even on a listed host.
SIDE_EFFECT_GETS = (("accounts.google.com", "/Logout"), ("www.google.com", "/accounts/Logout"),
                    ("www.youtube.com", "/logout"))
# Cookie rotation keeps the shared Google session alive (gemini allows it too) and touches no user data.
SESSION_POSTS = frozenset({("accounts.google.com", "/RotateCookies")})
# Where a Google app sends a profile that isn't signed in: sign-in, consent, or the product's marketing page.
SIGNED_OUT_HOSTS = frozenset({"accounts.google.com", "consent.google.com", "workspace.google.com", "www.google.com"})
EMAIL_JS = ("() => { const wiz = window.WIZ_global_data || {}; "
            "return typeof wiz.oPEP7c === 'string' ? wiz.oPEP7c : null; }")
REDIRECTS = (301, 302, 303, 307, 308)
FILE_HOPS = 5
FILE_TIMEOUT_MS = 300_000


def allowed(method: str, url: str, hosts: tuple[str, ...]) -> bool:
    """The rules every Google data site's guard starts from: https on the default port only; GET/HEAD on the site's
    `hosts` or INFRA_HOSTS, minus SIDE_EFFECT_GETS (sign-out); POST only Google's cookie rotation. Anything else is
    the site's own guard's call (its API reads), and it fails closed."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" or parts.port not in (None, 443) or "@" in parts.netloc:
        return False
    if method in ("GET", "HEAD"):
        return (any(host_matches(pattern, host) for pattern in (*hosts, *INFRA_HOSTS))
                and not any(host == h and parts.path.startswith(prefix) for h, prefix in SIDE_EFFECT_GETS))
    return method == "POST" and (host, parts.path) in SESSION_POSTS


def blocked() -> str:
    """What the guard aborted during this command, for an error that says nothing came back (describe() lines:
    no query strings, tokens redacted)."""
    lines = list(dict.fromkeys(guard.state.blocked))[:6]
    return f" The guard blocked: {'; '.join(lines)}." if lines else ""


async def open_app(page: "Page", url: str) -> None:
    """Load `url`. AuthRequired when Google sends the profile to sign-in or the product's signed-out page."""
    host = urlsplit(url).hostname
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    landed = urlsplit(page.url).hostname or ""
    if landed in SIGNED_OUT_HOSTS:
        raise AuthRequired(f"{host} sent Chrome to {landed}: the Google session is missing or expired.")
    if landed != host:
        raise RuntimeError(f"{host} redirected to {landed}.")


async def status(policy: Policy, url: str) -> dict:
    """{signedIn, email}: signed in when the app loads without bouncing to Google's sign-in. email is best effort
    (Google's WIZ apps keep it in WIZ_global_data), else null."""
    async with browser.site_page(policy) as page:
        try:
            await open_app(page, url)
        except AuthRequired:
            return {"signedIn": False, "email": None}
        try:
            email = await page.evaluate(EMAIL_JS)
        except Exception:
            email = None
        return {"signedIn": True, "email": email}


async def fetch_file(page: "Page", url: str, ok: Callable[[str], bool], *, what: str) -> tuple[int, dict, bytes]:
    """GET `url` with the profile's cookies (page.request, which the route guard never sees), following at most
    FILE_HOPS redirects, each URL checked by ok() before it is fetched. (status, headers, body); body only for a 2xx.
    Errors name the host and path shape only: download links are signed."""
    for _ in range(FILE_HOPS + 1):
        where = guard.describe("GET", url)
        if not ok(url):
            raise RuntimeError(f"Refused to fetch {what} from {where}: not one of the site's download hosts.")
        try:
            res = await page.request.get(url, max_redirects=0, timeout=FILE_TIMEOUT_MS)
            try:
                if res.status in REDIRECTS:
                    url = urljoin(url, res.headers.get("location", ""))
                    continue
                return res.status, res.headers, await res.body() if 200 <= res.status < 300 else b""
            finally:
                await res.dispose()  # else the context keeps the body in memory until Chrome closes
        except Exception as exc:
            raise RuntimeError(f"Fetching {what} failed ({type(exc).__name__}, {where}).") from None
    raise RuntimeError(f"Fetching {what} redirected more than {FILE_HOPS} times.")


def safe_name(text: str | None, fallback: str) -> str:
    """A bare file name from a title: letters, digits, space, . - _ (everything else becomes _); `fallback` when
    nothing usable is left."""
    name = "".join(c if c.isalnum() or c in " .-_" else "_" for c in (text or "")).strip()
    return fallback if not name.strip(".") else name


def save_file(directory: str | Path, name: str, data: bytes) -> Path:
    """`data` as directory/name: an exclusive 0600 temp file there, renamed over `name` (a symlink at `name` is
    replaced, never followed). `name` must be a bare file name (safe_name)."""
    if "/" in name or name.strip(".") == "":
        raise ValueError(f"{name!r} is not a bare file name")
    directory = Path(directory)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{name}.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, directory / name)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return directory / name
