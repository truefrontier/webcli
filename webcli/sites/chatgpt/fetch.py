"""Browser-side work: login, status, chats, models, ask.

Reads use ChatGPT's own JSON API (endpoints.py), called with fetch() inside a chatgpt.com page. The API wants a
bearer token, which the page gets from /api/auth/session; the JS below does the same, keeps the token in a local
variable, and hands Python only the API's answer. Python never sees the token, the session cookie, or the session
JSON (session_summary returns only signedIn, email, name, and plan).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import quote, urlencode, urlsplit

import base64
import math

from webcli.core import browser, guard
from webcli.core.errors import AuthRequired, Challenged, NotFound, RateLimited
from webcli.sites.chatgpt import parse, selectors as sel
from webcli.sites.chatgpt.endpoints import APP_HOST, APP_URL, CONVERSATION_PATHS, LOGIN_URL
from webcli.sites.chatgpt.guard import POLICY

if TYPE_CHECKING:
    from playwright.async_api import Page

PROBE_HINT = ("ChatGPT's API may have changed; run `python -m webcli.core.capture chatgpt` and fix "
              "webcli/sites/chatgpt/endpoints.py.")
SELECTOR_HINT = "ChatGPT's page may have changed; rerun with --headed and fix chatgpt_web_cli/selectors.py."
CHATS_PAGE = 28  # what the web app asks for
SEND_SECONDS = 60  # after the click: no prompt request by then means it was never sent
UPLOAD_SECONDS = 180  # after attaching: how long the --file uploads get to finish
POLL_SECONDS = 2  # between reads of the chat record while the reply is written
IMAGE_GRACE = 90  # after the reply settles without its image: how long the image tool gets to deliver it
STALL_SECONDS = 20  # after the answer stream ends with nothing in the record: when to look for a cap notice
# The page's last lines, where ChatGPT shows an error or cap notice it got inside the answer stream.
TAIL_JS = "() => ((document.querySelector('main') || document.body).innerText || '').slice(-2000)"
SENT_HINT = "The prompt was sent: check `llmcli chatgpt chats list` before sending it again."
LOGIN_POLL = 2.5  # seconds between sign-in checks: page.url and title, then one session read on chatgpt.com

# Runs inside the page. The token never leaves this function; Python gets {status, contentType, body}.
API_JS = """
async ({path, method}) => {
  try {
    const s = await fetch('/api/auth/session', {credentials: 'include', signal: AbortSignal.timeout(30000)});
    if (!s.ok) return {status: s.status === 403 ? 403 : 401, contentType: '', body: '',
                       cfMitigated: s.headers.get('cf-mitigated')};
    const token = ((await s.json()) || {}).accessToken;
    if (!token) return {status: 401, contentType: '', body: ''};
    const res = await fetch(path, {method: method || 'GET', credentials: 'include', redirect: 'manual',
      signal: AbortSignal.timeout(60000), headers: {Authorization: 'Bearer ' + token, Accept: 'application/json'}});
    return {status: res.type === 'opaqueredirect' ? 0 : res.status, contentType: res.headers.get('content-type') || '',
            cfMitigated: res.headers.get('cf-mitigated'), body: await res.text()};
  } catch (e) {
    return {status: -1, contentType: '', body: String(e && e.name || e)};
  }
}
"""
# Only these four fields leave the page.
SESSION_JS = """
async () => {
  try {
    const s = await fetch('/api/auth/session', {credentials: 'include', signal: AbortSignal.timeout(30000)});
    if (!s.ok) return {status: s.status, cfMitigated: s.headers.get('cf-mitigated')};
    const data = (await s.json()) || {};
    const user = data.user || {};
    return {status: s.status, signedIn: !!(data.accessToken && user.id), email: user.email || null,
            name: user.name || null, plan: (data.account || {}).planType || null};
  } catch (e) {
    return {status: -1};
  }
}
"""


# Seconds a challenge may take to clear by itself before giving up. "visible" (--headed) is long: a person can
# click the check there.
CHALLENGE_GRACE = {"headless": 10, "visible": 120}
# Booleans only: no page HTML leaves the page.
CHALLENGE_JS = """() => ({title: document.title, url: location.href,
  form: !!document.querySelector('#challenge-form, #challenge-stage, #challenge-running, .cf-turnstile, [id^="cf-chl-widget"]'),
  iframe: !!document.querySelector('iframe[src*="challenges.cloudflare.com"]'),
  app: !!document.querySelector('.ProseMirror, nav, [aria-label="Select ChatGPT model"]')})"""


async def challenged(page: "Page") -> bool:
    """Whether the page shows Cloudflare's challenge (parse.cloudflare_challenge). A page mid-navigation is judged
    by its URL alone."""
    try:
        state = await page.evaluate(CHALLENGE_JS)
    except Exception:
        state = {"url": page.url}
    return parse.cloudflare_challenge(state)


async def wait_out_challenge(page: "Page", seconds: float) -> bool:
    """True once the page is past Cloudflare's challenge (Cloudflare's non-interactive check clears itself in a few
    seconds), False if it still shows it after `seconds`. Never clicks anything."""
    deadline = time.monotonic() + seconds
    while await challenged(page):
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(1)
    return True


async def past_challenge(page: "Page", mode: str) -> None:
    """browser.page_at's ready hook: Challenged when Cloudflare's check doesn't clear within CHALLENGE_GRACE[mode]."""
    if not await wait_out_challenge(page, CHALLENGE_GRACE[mode]):
        raise Challenged(mode, APP_HOST)


async def api(page: "Page", path: str) -> dict:
    """GET one /backend-api/ path from inside the page: {status, contentType, cfMitigated, body}."""
    return await page.evaluate(API_JS, {"path": path, "method": "GET"})


async def api_json(page: "Page", path: str, *, item: str | None = None) -> Any:
    """api(), decoded. Cloudflare's challenge -> Challenged; 401/403 -> AuthRequired; 404 on an `item` -> NotFound."""
    res = await api(page, path)
    status = res["status"]
    if status == 403 and res.get("cfMitigated") == "challenge":
        raise Challenged("page", APP_HOST)
    if status in (0, 401, 403):
        raise AuthRequired(f"ChatGPT refused {path.split('?')[0]} (HTTP {status}); the session is missing or expired.")
    if status == 404 and item:
        raise NotFound(f"{item} not found.", hint="If every chat fails like this, check `llmcli chatgpt status`.")
    if status == -1:
        raise RuntimeError(f"{path.split('?')[0]} failed in the page ({res['body']}). {PROBE_HINT}")
    if not 200 <= status < 300:
        raise RuntimeError(f"{path.split('?')[0]} failed: HTTP {status}. {PROBE_HINT}")
    try:
        return json.loads(res["body"])
    except ValueError:
        raise RuntimeError(f"{path.split('?')[0]} answered something that isn't JSON. {PROBE_HINT}") from None


async def session_summary(page: "Page") -> dict:
    """{status, signedIn, email, name, plan} from /api/auth/session, read in the page (no token leaves it)."""
    info = await page.evaluate(SESSION_JS)
    if info.get("status") == 403 and info.get("cfMitigated") == "challenge":
        raise Challenged("page", APP_HOST)
    return info


def signed_out() -> dict:
    return {"signedIn": False, "plan": None, "email": None}


def _account(info: dict) -> dict:
    """What `status` prints: signedIn, plan, and the email masked. Nothing else from the session."""
    if not info.get("signedIn"):
        return signed_out()
    return {"signedIn": True, "plan": info.get("plan"), "email": parse.mask_email(info.get("email"))}


def _check_app(page: "Page") -> None:
    """AuthRequired when chatgpt.com sent Chrome to a sign-in page."""
    parts = urlsplit(page.url)
    if parts.hostname != APP_HOST or parts.path.startswith("/auth/"):
        raise AuthRequired(f"ChatGPT sent Chrome to {parts.hostname}{parts.path}; the session is missing or expired.")


async def _signed_in(page: "Page") -> None:
    _check_app(page)
    if not (await session_summary(page)).get("signedIn"):
        raise AuthRequired("Not signed in to ChatGPT.")


async def with_app(work: Callable[["Page"], Awaitable[Any]], *, headed: bool = False, url: str = APP_URL,
                   send: bool = False) -> Any:
    """Run work(page) on a chatgpt.com page: a tab in the identity daemon's Chrome, else a headless Chrome of this
    command's own (a visible one with --headed). Never a headed fallback: a Cloudflare challenge headless Chrome
    can't pass raises Challenged."""
    async with browser.site_page(POLICY, mode="visible" if headed else "headless", url=url, send=send,
                                 ready=past_challenge) as page:
        return await work(page)


async def status(*, headed: bool = False) -> dict:
    async def work(page: "Page") -> dict:
        try:
            _check_app(page)
        except AuthRequired:
            return signed_out()
        return _account(await session_summary(page))

    return await with_app(work, headed=headed)


async def login(timeout: int, on_state: Callable[[str], None] = lambda _state: None) -> dict:
    """A visible Chrome window on ChatGPT's sign-in page; wait (up to `timeout` seconds) until a tab is signed in.

    Gentle on purpose: no request guard (plain Chrome, policy None), and every LOGIN_POLL seconds only each tab's URL
    and title, plus one session read once a tab is on chatgpt.com past Cloudflare. Cloudflare's challenge is a
    wait, not a failure: the person clicks "Verify you are human" in the window. on_state gets "cloudflare" or
    "signing in" each time that changes. AuthRequired when every tab is closed or time runs out.
    """
    async with browser.open_page(None, mode="visible") as page:
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception:
            pass  # a challenge or a slow redirect: the window stays open and the loop below waits
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            await asyncio.sleep(LOGIN_POLL)
            tabs = [tab for tab in page.context.pages if not tab.is_closed()]
            if not tabs:
                raise AuthRequired("The Chrome window closed before sign-in finished.")
            state = "signing in"
            for tab in tabs:  # sign-in may finish in another tab
                try:
                    url, title = tab.url, await tab.title()
                except Exception:  # mid-navigation
                    continue
                if parse.cloudflare_challenge({"title": title, "url": url}):
                    state = "cloudflare"
                    continue
                if urlsplit(url).hostname != APP_HOST or urlsplit(url).path.startswith("/auth/"):
                    continue
                try:
                    info = await tab.evaluate(SESSION_JS)
                except Exception:
                    continue
                if info.get("signedIn"):
                    return _account(info)
            if state != last:
                on_state(state)
                last = state
        raise AuthRequired(f"Timed out after {timeout}s waiting for ChatGPT sign-in.")


async def models(*, headed: bool = False) -> list[dict]:
    """ChatGPT's model list (GET /backend-api/models): what -m takes. Nothing is picked."""
    async def work(page: "Page") -> list[dict]:
        await _signed_in(page)
        return parse.models(await api_json(page, "/backend-api/models?" + urlencode({"iim": "false", "is_gizmo": "false"})))

    return await with_app(work, headed=headed)


async def chats(limit: int, *, headed: bool = False) -> list[dict]:
    """Up to `limit` chats, most recently updated first (GET /backend-api/conversations, offset paging). Chats inside
    projects are not in this list."""
    async def work(page: "Page") -> list[dict]:
        await _signed_in(page)
        rows: list[dict] = []
        seen: set[str] = set()
        offset = 0
        while len(rows) < limit:
            query = urlencode({"offset": offset, "limit": min(CHATS_PAGE, limit - len(rows)), "order": "updated"})
            payload = await api_json(page, "/backend-api/conversations?" + query)
            batch = [r for r in parse.chats(payload) if r["id"] not in seen]
            seen.update(r["id"] for r in batch)
            rows += batch
            offset += len(payload.get("items") or [])
            if not batch or offset >= (payload.get("total") or 0):
                break
        return rows[:limit]

    return await with_app(work, headed=headed)


async def record(page: "Page", chat_id: str) -> dict:
    """GET /backend-api/conversation/<id>: the whole record (NotFound for a made-up or someone else's id)."""
    return await api_json(page, f"/backend-api/conversation/{chat_id}", item=f"Chat {chat_id}")


async def chat(chat_id: str, *, headed: bool = False) -> dict:
    async def work(page: "Page") -> dict:
        await _signed_in(page)
        data = await record(page, chat_id)
        return {"chatId": chat_id, "url": parse.chat_url(chat_id), "title": data.get("title"),
                "messages": parse.messages(data)}

    return await with_app(work, headed=headed)


# Text of each visible notice (toast, banner, dialog) outside the transcript and the prompt box.
NOTICES_JS = """
([noticeSel, skipSel]) => [...document.querySelectorAll(noticeSel)]
  .filter((el) => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden'
    && !el.closest(skipSel) && !el.querySelector(skipSel))
  .map((el) => (el.innerText || el.textContent || '').trim()).filter(Boolean)
"""
# Runs inside the page: the download link and the bytes never leave it as a URL. Python gets
# {status, b64, type} or {status, error}. Only https links on chatgpt.com or *.oaiusercontent.com are fetched.
DOWNLOAD_JS = """
async ({path}) => {
  try {
    const s = await fetch('/api/auth/session', {credentials: 'include'});
    const token = ((await s.json()) || {}).accessToken;
    if (!token) return {status: 401, error: 'signed out'};
    const r = await fetch(path, {credentials: 'include', headers: {Authorization: 'Bearer ' + token}});
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.download_url) return {status: r.status, error: String((j && j.detail) || 'no download link').slice(0, 120)};
    const u = new URL(j.download_url, location.href);
    if (u.protocol !== 'https:' || !(u.host === location.host || /(^|\\.)oaiusercontent\\.com$/.test(u.hostname)))
      return {status: r.status, error: 'download link on an unexpected host: ' + u.hostname};
    const b = await fetch(u.href, {credentials: u.host === location.host ? 'include' : 'omit'});
    if (!b.ok) return {status: b.status, error: 'file download failed'};
    const buf = new Uint8Array(await b.arrayBuffer());
    let bin = '';
    for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
    return {status: b.status, type: b.headers.get('content-type'), b64: btoa(bin)};
  } catch (e) {
    return {status: -1, error: String(e && e.name || e)};
  }
}
"""
# Where a generated image's stored file comes from, in order of preference: the files endpoint as the "Download"
# button asks for it, as the page asks for it to show the image, the older post_id form, the conversation attachment
# route, and file-service:// files' own route. Each answers {download_url}; DOWNLOAD_JS fetches that link in the page.
DOWNLOAD_PATHS = (
    "/backend-api/files/download/{file}?conversation_id={chat}&download_intent=true&inline=false",
    "/backend-api/files/download/{file}?conversation_id={chat}&download_intent=false&inline=true",
    "/backend-api/files/download/{file}?post_id=&inline=false",
    "/backend-api/conversation/{chat}/attachment/{file}/download",
    "/backend-api/files/{file}/download",
)


async def _notices(page: "Page") -> list[str]:
    try:
        return await page.evaluate(NOTICES_JS, [sel.NOTICE, sel.NOTICE_SKIP])
    except Exception:
        return []


async def _check_limit(page: "Page", *, sent: bool) -> None:
    """RateLimited when the page shows a usage-cap notice. A check that fails counts as no notice."""
    text = next((t for t in await _notices(page) if parse.limit_notice(t)), None)
    if text:
        text = " ".join(text.split())
        raise RateLimited(text, sent=sent, resets_at=parse.resets_at(text))


async def _close_dialogs(page: "Page") -> None:
    """Escape closes announcement dialogs ("You now have access to Health in ChatGPT") without answering them."""
    for _ in range(3):
        if not await page.locator(sel.DIALOG).count():
            return
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.4)


async def _open_menu(page: "Page") -> None:
    """Open the model picker. An announcement dialog can render late and swallow the click, so each of the four
    tries closes dialogs first."""
    button = page.locator(sel.MODEL_BUTTON).first
    for _ in range(4):
        await _close_dialogs(page)
        try:
            await button.click(timeout=5_000)
            await page.locator(sel.MENU).first.wait_for(state="visible", timeout=4_000)
            return
        except Exception:
            await page.keyboard.press("Escape")
    raise RuntimeError(f"Couldn't open ChatGPT's model picker. {SELECTOR_HINT}")


async def _picker_state(page: "Page") -> dict:
    """{version: the checked radio's text, power: the slider's value} with the picker open."""
    return await page.evaluate("""([radioSel, sliderSel]) => {
      const checked = [...document.querySelectorAll(radioSel)].find((r) => r.getAttribute('aria-checked') === 'true');
      const slider = document.querySelector(sliderSel);
      return {version: checked ? checked.innerText.trim() : null,
              versions: [...document.querySelectorAll(radioSel)].map((r) => r.innerText.trim()),
              power: slider ? Number(slider.getAttribute('aria-valuenow')) : null};
    }""", [sel.VERSION_ITEM, sel.POWER_SLIDER])


async def _pick_model(page: "Page", choice: dict) -> None:
    """Pick choice's version radio and Power stop in the picker, the way a person would (a click, then arrow keys on
    the slider), and check the picker shows them. Nothing is sent: the guard checks the prompt's model anyway."""
    labels = {label.casefold() for label in choice["labels"]}
    await _open_menu(page)
    state = await _picker_state(page)
    if (state["version"] or "").casefold() not in labels:
        radios = page.locator(sel.VERSION_ITEM)
        index = next((i for i, text in enumerate(state["versions"]) if text.casefold() in labels), None)
        if index is None:
            await page.keyboard.press("Escape")
            raise NotFound(f"ChatGPT's model picker has no {choice['version']!r} (it shows: "
                           f"{', '.join(state['versions']) or 'nothing'}).", hint="run: llmcli chatgpt models")
        await radios.nth(index).click(timeout=10_000)
        await asyncio.sleep(0.6)
        if not await page.locator(sel.MENU).count():
            await _open_menu(page)
        state = await _picker_state(page)
    if state["power"] != choice["index"]:
        slider = page.locator(sel.POWER_SLIDER).first
        await slider.focus()
        await page.keyboard.press("Home")
        for _ in range(choice["index"]):
            await page.keyboard.press("ArrowRight")
        await asyncio.sleep(0.6)
        if not await page.locator(sel.MENU).count():  # a slider move can close the menu
            await _open_menu(page)
        state = await _picker_state(page)
    await page.keyboard.press("Escape")
    if (state["version"] or "").casefold() not in labels or state["power"] != choice["index"]:
        raise RuntimeError(f"Picked {choice['version']} {choice['preset']} in ChatGPT's model picker, but it shows "
                           f"{state['version']} at power {state['power']}. {SELECTOR_HINT}")


async def _attach(page: "Page", files: list[str]) -> None:
    """Hand the files to the composer's own file input, the way a person's file picker would."""
    try:
        await page.locator(sel.FILE_INPUT).first.set_input_files(files, timeout=10_000)
    except Exception:
        raise RuntimeError(f"Couldn't attach {len(files)} file(s) with ChatGPT's file input. {SELECTOR_HINT}") from None


async def _send_ready(page: "Page", seconds: float) -> bool:
    """Wait for the Send button to enable: it doesn't until the text registers, nor while a file uploads."""
    try:
        await page.wait_for_function(
            "(s) => { const b = document.querySelector(s); return b && !b.disabled"
            " && b.getAttribute('aria-disabled') !== 'true'; }", arg=sel.SEND_BUTTON, timeout=seconds * 1000)
        return True
    except Exception:
        return False


async def _type_prompt(page: "Page", prompt: str) -> None:
    """Type the prompt like a person (ProseMirror ignores programmatic fills); Shift+Enter between lines. Whatever
    the box held (a draft ChatGPT kept from an earlier visit) is cleared first."""
    editor = page.locator(sel.PROMPT_INPUT).first
    try:
        await editor.wait_for(state="visible", timeout=30_000)
    except Exception:
        raise RuntimeError(f"ChatGPT's prompt box never appeared. {SELECTOR_HINT}") from None
    try:
        await editor.click(timeout=5_000)
    except Exception:  # something still overlays it
        await editor.focus()
    await page.keyboard.press("ControlOrMeta+A")  # ChatGPT keeps unsent drafts: start from an empty box
    await page.keyboard.press("Backspace")
    for n, line in enumerate(prompt.split("\n")):
        if n:
            await page.keyboard.press("Shift+Enter")
        if line:
            await page.keyboard.insert_text(line)


async def _download(page: "Page", chat_id: str, image: dict) -> dict:
    """One generated image as {data, sourceWidth, sourceHeight, sourceBytes}: the stored file its pointer names."""
    errors = []
    for template in DOWNLOAD_PATHS:
        path = template.format(file=quote(image["fileId"], safe=""), chat=quote(chat_id, safe=""))
        res = await page.evaluate(DOWNLOAD_JS, {"path": path})
        if res.get("b64"):
            return {"data": base64.b64decode(res["b64"]), "sourceWidth": image["width"],
                    "sourceHeight": image["height"], "sourceBytes": image["bytes"]}
        errors.append(f"HTTP {res.get('status')} {res.get('error')}")
    error = "; ".join(dict.fromkeys(errors))
    raise RuntimeError(f"Downloading image {image['fileId']} failed ({error}). {SENT_HINT}")


async def _check_stalled(page: "Page", prompt: str) -> None:
    """The answer stream ended but the record has no reply: ChatGPT may have refused inside the stream (an HTTP 200
    whose body llmcli can't read). RateLimited when the page's last lines, minus the prompt, carry a cap notice."""
    try:
        tail = (await page.evaluate(TAIL_JS)).replace(prompt, " ")
    except Exception:
        return
    notice = next((line.strip() for line in reversed(tail.splitlines()) if parse.limit_notice(line)), None)
    if notice:
        raise RateLimited(notice, sent=True, resets_at=parse.resets_at(notice))


def _attachments(record: Any, user_message_id: str | None) -> list[dict]:
    """What the chat record says the prompt carried: [{name, bytes, mime}]."""
    for message in parse.branch(record):
        if message.get("id") == user_message_id:
            return [{"name": a.get("name"), "bytes": a.get("size"), "mime": a.get("mime_type")}
                    for a in (message.get("metadata") or {}).get("attachments") or [] if isinstance(a, dict)]
    return []


def _settled(turn: dict | None) -> bool:
    """The reply's last message ended the turn and nothing is still in progress."""
    return bool(turn and turn["endTurn"] and "in_progress" not in turn["statuses"])


def _done(turn: dict | None, want_images: bool, ended_at: float | None) -> bool:
    """A reply is done once it settled (_settled). With --image-out, a settled reply without images gets IMAGE_GRACE
    more seconds from when it first settled (`ended_at`): the image tool can deliver after the text."""
    if not _settled(turn):
        return False
    if want_images and not turn["images"] and ended_at is not None:
        return time.monotonic() - ended_at >= IMAGE_GRACE
    return True


async def _ask_impl(page: "Page", prompt: str, *, model: str | None, chat_id: str | None, files: list[str],
                    want_images: bool, deadline: float, progress: dict) -> dict:
    # Context listeners outlive the page in the daemon's long-lived Chrome: each one is removed when the ask ends.
    listeners: list[tuple[str, Callable]] = []
    try:
        return await _ask_steps(page, prompt, model=model, chat_id=chat_id, files=files, want_images=want_images,
                                deadline=deadline, progress=progress, listeners=listeners)
    finally:
        for event, handler in listeners:
            page.context.remove_listener(event, handler)


async def _ask_steps(page: "Page", prompt: str, *, model: str | None, chat_id: str | None, files: list[str],
                     want_images: bool, deadline: float, progress: dict, listeners: list) -> dict:
    loop = asyncio.get_running_loop()
    progress["stage"] = "loading ChatGPT"
    await _signed_in(page)
    if chat_id:
        await record(page, chat_id)  # NotFound before anything is sent
    progress["stage"] = "reading the model list"
    catalog = await api_json(page, "/backend-api/models?" + urlencode({"iim": "false", "is_gizmo": "false"}))
    slug = choice = None
    if model:
        try:
            slug = parse.match_model(parse.models(catalog), model)
        except LookupError as exc:
            raise NotFound(str(exc), hint="run: llmcli chatgpt models") from None
        choice = parse.picker_choice(catalog, slug)
        if not choice:
            raise NotFound(f"{slug} isn't in ChatGPT's model picker, so llmcli can't choose it.",
                           hint="Pick a slug the picker offers (llmcli chatgpt models; the GPT-6 / GPT-5.6 Instant and Thinking "
                                "slugs), or drop -m.")
    await page.locator(sel.PROMPT_INPUT).first.wait_for(state="visible", timeout=30_000)
    await _close_dialogs(page)
    await _check_limit(page, sent=False)
    if choice:
        progress["stage"] = "selecting the model"
        await _pick_model(page, choice)
    uploads: list[str] = []

    def on_upload(response) -> None:
        path = urlsplit(response.url).path
        if response.request.method == "POST" and response.ok and (
                path.endswith("/uploaded") or path.endswith("/process_upload_stream")):
            uploads.append(path)

    if files:
        progress["stage"] = "uploading the attachments"
        page.context.on("response", on_upload)
        listeners.append(("response", on_upload))
        await _attach(page, files)
    progress["stage"] = "preparing the prompt"
    await _type_prompt(page, prompt)
    if files:
        upload_deadline = loop.time() + UPLOAD_SECONDS
        while len(uploads) < len(files) and loop.time() < min(upload_deadline, deadline):
            await _check_limit(page, sent=False)
            await asyncio.sleep(0.5)
        if len(uploads) < len(files):
            raise RuntimeError(f"ChatGPT was still uploading the --file attachments after {UPLOAD_SECONDS}s "
                               f"({len(uploads)} of {len(files)} done). The prompt was not sent.")
    if not await _send_ready(page, 60 if files else 15):
        raise RuntimeError(f"ChatGPT's Send button never enabled. The prompt was not sent. {SELECTOR_HINT}")
    await _check_limit(page, sent=False)

    requests: list[dict] = []
    responses: list = []

    def on_request(request) -> None:
        parts = urlsplit(request.url)
        if request.method == "POST" and parts.hostname == APP_HOST and parts.path in CONVERSATION_PATHS:
            try:
                body = json.loads(request.post_data or "{}")
            except ValueError:
                body = {}
            messages = body.get("messages") or [{}]
            requests.append({"model": body.get("model"), "messageId": messages[-1].get("id")})

    def on_response(response) -> None:
        parts = urlsplit(response.url)
        if response.request.method == "POST" and parts.hostname == APP_HOST and parts.path in CONVERSATION_PATHS:
            responses.append(response)

    progress["stage"] = "sending the prompt"
    guard.state.expect_models = frozenset(choice["sends"]) if choice else None
    blocked_before = len(guard.state.blocked)
    page.context.on("request", on_request)
    page.context.on("response", on_response)
    listeners += [("request", on_request), ("response", on_response)]
    progress["submitted"] = True  # the click may reach ChatGPT even if it times out
    try:
        await page.locator(sel.SEND_BUTTON).first.click(timeout=5_000)
    except Exception:
        await asyncio.sleep(1)
        if not requests:  # never both: Enter only when the click sent nothing
            await page.locator(sel.PROMPT_INPUT).first.press("Enter")
    send_deadline = min(deadline, loop.time() + SEND_SECONDS)
    while not responses:
        aborted = [b for b in guard.state.blocked[blocked_before:]
                   if b in {f"POST {APP_HOST}{path}" for path in CONVERSATION_PATHS}]
        if aborted:
            raise RuntimeError(f"The model picker didn't take: the prompt asked for another model than "
                               f"{' or '.join(choice['sends'])}, so llmcli stopped it. The prompt was not sent. "
                               f"{SELECTOR_HINT}")
        await _check_limit(page, sent=True)
        if loop.time() >= send_deadline:
            if requests:  # the prompt went out; only ChatGPT's answer is late
                raise RuntimeError(f"ChatGPT got the prompt but didn't answer within {SEND_SECONDS}s. {SENT_HINT}")
            raise RuntimeError(f"ChatGPT never sent the prompt (no request within {SEND_SECONDS}s). {SELECTOR_HINT}")
        await asyncio.sleep(0.25)
    response = responses[0]
    user_message_id = requests[0]["messageId"] if requests else None
    if response.status >= 400:
        try:
            body = await response.text()
        except Exception:
            body = ""
        error = parse.stream_error(body) or {"message": f"HTTP {response.status}", "code": None, "clearsIn": None}
        message = " ".join(str(error["message"]).split())
        if response.status == 429 or parse.limit_notice(message) or "cap" in str(error.get("code") or ""):
            resets = parse.resets_after(error["clearsIn"]) if error.get("clearsIn") is not None \
                else parse.resets_at(message)
            raise RateLimited(message, sent=True, resets_at=resets)
        raise RuntimeError(f"ChatGPT refused the prompt: HTTP {response.status} ({message}). {SENT_HINT}")

    progress["stage"] = "waiting for the reply"
    stream_done = asyncio.ensure_future(response.finished())
    # It fails with "Target closed" once Chrome closes; read that so asyncio doesn't log it.
    stream_done.add_done_callback(lambda done: done.cancelled() or done.exception())
    stream_ended_at = None
    chat = chat_id
    while not chat:
        chat = parse.url_chat_id(page.url)
        if chat or loop.time() >= deadline:
            break
        await _check_limit(page, sent=True)
        await asyncio.sleep(0.5)
    if not chat:
        raise RuntimeError(f"ChatGPT answered, but the page shows no chat id to read the reply from. {SENT_HINT}")
    turn = data = None
    ended_at = None
    while loop.time() < deadline:
        await _check_limit(page, sent=True)
        try:
            data = await record(page, chat)
            turn = parse.reply_turn(data, user_message_id)
        except (NotFound, RuntimeError):
            turn = None  # a new chat's record can lag the page
        if _settled(turn) and ended_at is None:
            ended_at = time.monotonic()
        if _done(turn, want_images, ended_at):
            break
        if stream_done.done() and stream_ended_at is None:
            stream_ended_at = time.monotonic()
        if stream_ended_at and not (turn and turn["statuses"]) and time.monotonic() - stream_ended_at > STALL_SECONDS:
            await _check_stalled(page, prompt)
        await asyncio.sleep(POLL_SECONDS)
    else:
        raise RuntimeError(f"ChatGPT's reply wasn't finished at the deadline (raise --timeout). {SENT_HINT}")

    served = turn["servedModel"]
    # Not a fallback: the slug itself or its routed variant, or what the picker stop sends (Instant sends the auto
    # slug, and ChatGPT records that slug as the model).
    fallback = bool(slug) and served not in choice["sends"] and not parse.same_model(slug, served)
    out = {"chatId": chat, "chatUrl": parse.chat_url(chat), "requestedModel": slug, "servedModel": served,
           "servedModelDisplay": parse.display_name(catalog, served), "fallback": fallback,
           "fallbackFrom": slug if fallback else None, "fallbackTo": served if fallback else None,
           "prompt": prompt, "answer": turn["answer"], "imageCount": len(turn["images"]),
           "attachments": _attachments(data, turn["userMessageId"]), "citations": turn["citations"]}
    if want_images:
        progress["stage"] = "downloading the images"
        out["imageData"] = [await _download(page, chat, image) for image in turn["images"]]
    return out


async def ask(prompt: str, *, model: str | None = None, chat_id: str | None = None, files: list[str] | None = None,
              timeout: float = 180, headed: bool = False, want_images: bool = False) -> dict:
    """Send one prompt through ChatGPT's composer and read the reply back from the chat's record:
    {chatId, chatUrl, requestedModel, servedModel, servedModelDisplay, fallback, fallbackFrom, fallbackTo, prompt,
    answer, imageCount, attachments, citations[, imageData]}.

    Everything that can fail without sending (sign-in, Cloudflare, --chat, -m, the picker, --file uploads, the
    prompt box) is checked first.
    The prompt is submitted once, by the Send button or by Enter when the button can't be clicked; never both. With
    -m, the guard aborts a prompt that names another model, so a picker that didn't take sends nothing.
    The whole operation (lock wait included) is bounded by `timeout`.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("--timeout must be a finite positive number of seconds.")
    progress = {"stage": "opening Chrome and waiting for the profile lock", "submitted": False}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    async def work(page: "Page") -> dict:
        return await _ask_impl(page, prompt, model=model, chat_id=chat_id, files=list(files or []),
                               want_images=want_images, deadline=deadline, progress=progress)

    try:
        return await asyncio.wait_for(
            with_app(work, headed=headed, url=parse.chat_url(chat_id) if chat_id else APP_URL, send=True),
            timeout=timeout)
    except asyncio.TimeoutError:
        hint = SENT_HINT if progress["submitted"] else "The prompt was not submitted."
        raise RuntimeError(f"Timed out after {timeout:g}s while {progress['stage']} (raise --timeout). {hint}") from None
