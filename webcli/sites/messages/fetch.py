"""Browser-side work for webcli messages (gmsg's browser.ts on the shared core): login, status, the conversation list,
one conversation's messages, and the one write, send.

Google Messages for web shows what the phone sends it, end-to-end encrypted on the way, so there is no answer to read
off the wire: every verb reads the rendered app (UI) with gmsg's selectors (endpoints.py). Every command but `login`
runs behind guard.POLICY, which passes the app's listing calls and aborts every phone action that changes anything,
MESSAGE_READ included, so reading a conversation never marks it read and nothing types.

Google Messages keeps one active web session. When the app says another tab or computer holds it ("Use here"), the
command takes it over, as gmsg did; the other one then offers "Use here" in turn.

`send` without confirm only reads: it resolves the conversation and stops. With confirm (and only then) its page
opens with send on and send_policy(scope); see send() for the checks made before the guard's one slot opens.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable, NoReturn
from urllib.parse import urlsplit

from webcli.core import browser, guard
from webcli.core.errors import AuthRequired, NotFound, UsageError
from webcli.sites.google import blocked
from webcli.sites.messages import parse
from webcli.sites.messages.endpoints import (
    APP_HOST,
    APP_URL,
    BUTTON_SECONDS,
    CONFIRM_SECONDS,
    EMPTY_POLLS,
    INPUTS,
    LOAD_SECONDS,
    LOGIN_URL,
    MESSAGES_SECONDS,
    POLL_MS,
    SELECTORS,
    SEND_BUTTON,
    SEND_SECONDS,
    SETTLE_MS,
    THREAD_SECONDS,
)
from webcli.sites.messages.guard import POLICY, SendScope, send_policy

if TYPE_CHECKING:
    from playwright.async_api import Locator, Page

SNIFF_HINT = ("Google Messages may have changed: run `python -m webcli.core.capture messages` and fix "
              "webcli/sites/messages/endpoints.py.")
NOT_PAIRED = "Google Messages isn't paired with your phone here (it shows its QR code)."
LIST_HINT = "Use a conversation id from `webcli messages list`: only conversations in that list can be opened."
NOTHING = "Nothing was sent."
OUTGOING = ("SEND_MESSAGE", "RESEND_MESSAGE", "SEND_REPLY")  # actions that would send a message
CLICK_MS = 5_000

STATE_JS = """/* messages:state */ (s) => {
  const n = sel => document.querySelectorAll(sel).length;
  const label = el => (el.innerText || el.getAttribute('aria-label') || '').trim();
  const page = document.body ? document.body.innerText || '' : '';
  const buttons = [...document.querySelectorAll('button, [role="button"]')].map(label);
  return {path: location.pathname, qr: n(s.qr) > 0 || s.pairText.some(t => page.includes(t)), list: n(s.list) > 0,
          items: n(s.item), loading: n(s.spinner) > 0,
          useHere: buttons.some(t => s.useHere.some(p => new RegExp(p, 'i').test(t))),
          messages: n(s.message), thread: n(s.thread) > 0, compose: n(s.compose) > 0};
}"""
USE_HERE_JS = """/* messages:use-here */ (patterns) => {
  const label = el => (el.innerText || el.getAttribute('aria-label') || '').trim();
  for (const p of patterns) {
    const el = [...document.querySelectorAll('button, [role="button"]')].find(b => new RegExp(p, 'i').test(label(b)));
    if (el) { el.click(); return true; }
  }
  return false;
}"""
CONVERSATIONS_JS = """/* messages:conversations */ (s) => [...document.querySelectorAll(s.item)].map(conv => {
  const pick = sels => { for (const sel of sels) { const el = conv.querySelector(sel); if (el) return el; } return null; };
  const text = el => el ? (el.textContent || '').trim() : '';
  const link = pick(s.link);
  return {href: link ? link.getAttribute('href') : null, name: text(pick(s.name)), preview: text(pick(s.preview)),
          timestamp: text(pick(s.time)),
          unread: (link && link.getAttribute('data-e2e-is-unread') === 'true') || !!conv.querySelector(s.unread)};
})"""
OPEN_JS = """/* messages:open */ ([s, id]) => {
  for (const conv of document.querySelectorAll(s.item)) {
    const link = s.link.map(sel => conv.querySelector(sel)).find(Boolean);
    const m = link && (link.getAttribute('href') || '').split(/[?#]/)[0].match(/(?:^|\\/)conversations\\/([^/]+)$/);
    if (m && m[1] === id) { link.click(); return true; }
  }
  return false;
}"""
MESSAGES_JS = """/* messages:messages */ (s) => [...document.querySelectorAll(s.message)].map(msg => {
  const pick = sels => { for (const sel of sels) { const el = msg.querySelector(sel); if (el) return el; } return null; };
  const core = msg.querySelector(s.core) || msg;
  const out = msg.getAttribute('is-outgoing') || core.getAttribute('data-e2e-message-outgoing');
  const text = pick(s.text), part = msg.querySelector(s.aria), stamp = pick(s.stamp);
  return {sent: out === 'true' || msg.classList.contains('outgoing') || core.classList.contains('outgoing')
                || msg.classList.contains('sent') || !!msg.querySelector(s.outgoing),
          text: text ? (text.textContent || '').trim() : '', image: !!msg.querySelector(s.image),
          aria: part ? part.getAttribute('aria-label') || '' : '', time: stamp ? (stamp.textContent || '').trim() : ''};
})"""
HEADER_JS = """/* messages:header */ (sels) => {
  for (const sel of sels) { const el = document.querySelector(sel); if (el) return (el.textContent || '').trim(); }
  return null;
}"""
READ_BACK_JS = "el => (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') ? el.value : el.innerText"


def _polls(seconds: float) -> int:
    return max(1, int(seconds * 1000 / POLL_MS))


async def _snapshot(page: "Page") -> dict:
    return await page.evaluate(STATE_JS, SELECTORS)


async def _state(page: "Page", snap: dict) -> str:
    """parse.state(snap), after clicking "Use here" when another session holds Messages. AuthRequired when the app
    shows pairing."""
    kind = parse.state(snap)
    if kind == "unpaired":
        raise AuthRequired(NOT_PAIRED)
    if kind == "use_here":
        await page.evaluate(USE_HERE_JS, SELECTORS["useHere"])
    return kind


async def ready(page: "Page") -> None:
    """Wait for the conversation list: its rows, or a list that stays empty with no spinner. AuthRequired when the
    app shows pairing; RuntimeError when neither comes within LOAD_SECONDS."""
    empty = 0
    for _ in range(_polls(LOAD_SECONDS)):
        snap = await _snapshot(page)
        if await _state(page, snap) == "paired":
            empty = 0 if snap.get("items") else empty + 1
            if snap.get("items") or empty > EMPTY_POLLS:
                await page.wait_for_timeout(SETTLE_MS)
                return
        await page.wait_for_timeout(POLL_MS)
    raise RuntimeError(f"Google Messages didn't show its conversation list within {LOAD_SECONDS}s.{blocked()} "
                       f"{SNIFF_HINT}")


async def open_app(page: "Page") -> None:
    await page.goto(APP_URL, wait_until="domcontentloaded", timeout=60_000)
    landed = urlsplit(page.url).hostname or ""
    if landed != APP_HOST:
        raise RuntimeError(f"{APP_HOST} redirected to {landed}.")
    await ready(page)


async def _rows(page: "Page") -> list[dict]:
    return parse.conversations(await page.evaluate(CONVERSATIONS_JS, SELECTORS))


async def _find(page: "Page", query: str) -> dict:
    found = parse.resolve(await _rows(page), query)
    if found is None:
        raise NotFound(f"No conversation matching {query!r} in the conversation list.", hint=LIST_HINT)
    if not found["id"]:
        raise RuntimeError(f"The conversation {found['name']!r} has no link to open. {SNIFF_HINT}")
    return found


async def _open(page: "Page", row: dict) -> None:
    """Open `row`'s conversation from the list, as a click would, and wait until the page shows that conversation:
    its id in the URL, and its messages or compose box."""
    if not await page.evaluate(OPEN_JS, [SELECTORS, row["id"]]):
        raise NotFound(f"Conversation {row['id']} isn't in the conversation list.", hint=LIST_HINT)
    for _ in range(_polls(THREAD_SECONDS)):
        snap = await _snapshot(page)
        kind = await _state(page, snap)
        if kind != "use_here" and parse.thread_id(snap.get("path")) == row["id"] and (snap.get("compose")
                                                                                     or snap.get("thread")):
            await page.wait_for_timeout(SETTLE_MS)
            return
        await page.wait_for_timeout(POLL_MS)
    shown = parse.thread_id(urlsplit(page.url).path)
    raise RuntimeError(f"Conversation {row['id']} didn't open within {THREAD_SECONDS}s"
                       f"{f' (the page shows {shown})' if shown else ''}.{blocked()} {SNIFF_HINT}")


async def _bubbles(page: "Page") -> list[dict]:
    return parse.messages(await page.evaluate(MESSAGES_JS, SELECTORS))


async def _loaded(page: "Page") -> list[dict]:
    """The open conversation's bubbles, once they have hydrated (or MESSAGES_SECONDS have passed)."""
    for _ in range(_polls(MESSAGES_SECONDS)):
        if (await _snapshot(page)).get("messages"):
            break
        await page.wait_for_timeout(POLL_MS)
    await page.wait_for_timeout(SETTLE_MS)
    return await _bubbles(page)


async def status(*, headed: bool = False) -> dict:
    """{signedIn, paired, status}: paired when the app shows its conversation list rather than its QR code (gmsg's
    status). headed is accepted for the daemon contract; data sites always run headless."""
    async with browser.site_page(POLICY) as page:
        try:
            await open_app(page)
        except AuthRequired:
            return {"signedIn": False, "paired": False, "status": "needs_pairing"}
    return {"signedIn": True, "paired": True, "status": "active"}


async def conversations(limit: int | None = None, *, headed: bool = False) -> list[dict]:
    """The conversation list as the app has loaded it (newest first), at most `limit` rows."""
    async with browser.site_page(POLICY) as page:
        await open_app(page)
        rows = await _rows(page)
    return rows[:limit] if limit else rows


async def messages(query: str, limit: int | None = None, *, headed: bool = False) -> dict:
    """{conversation, messages}: the messages the app shows for the conversation `query` names (oldest first, the
    last `limit`). READ-ONLY: the guard aborts the app's mark-as-read."""
    async with browser.site_page(POLICY) as page:
        await open_app(page)
        row = await _find(page, query)
        await _open(page, row)
        rows = await _loaded(page)
    return {"conversation": row, "messages": rows[-limit:] if limit else rows}


async def _usable(loc: "Locator") -> bool:
    try:
        return await loc.is_visible() and await loc.is_enabled()
    except Exception:  # detached mid-check: not usable
        return False


async def _compose(page: "Page") -> "Locator | None":
    for selector in INPUTS:
        loc = page.locator(selector).first
        if await loc.count() and await _usable(loc):
            return loc
    return None


async def _send_button(page: "Page") -> "Locator | None":
    buttons = page.locator(SEND_BUTTON)
    for _ in range(_polls(BUTTON_SECONDS)):
        for i in range(await buttons.count()):
            if await _usable(buttons.nth(i)):
                return buttons.nth(i)
        await page.wait_for_timeout(POLL_MS)
    return None


def _tried_send() -> list[str]:
    """The sends the guard has blocked this command (the page's own, a retry or a queued message), as describe()
    lines."""
    return [line for line in guard.state.blocked if any(line.endswith(f"action={name}") for name in OUTGOING)]


def _sent_count(rows: list[dict], text: str) -> int:
    return sum(1 for row in rows if row["direction"] == "sent" and row["text"] == text)


async def _click(button: "Locator") -> str | None:
    """Click send. The error's name if the click failed: whether a send went out is the guard's to say."""
    try:
        await button.click(timeout=CLICK_MS)
    except Exception as exc:
        return type(exc).__name__
    return None


async def _until(page: "Page", done: Callable[[], bool], seconds: float) -> bool:
    """Poll done() for up to `seconds`. A page that dies meanwhile ends the wait: done() still says what happened."""
    try:
        for _ in range(_polls(seconds)):
            if done():
                return True
            await page.wait_for_timeout(POLL_MS)
    except Exception:
        pass
    return done()


async def _shows(page: "Page", text: str, before: int) -> bool:
    """The conversation shows one more sent bubble with `text` than `before`, within CONFIRM_SECONDS. A page that
    dies meanwhile reads as not shown: the send already went out either way."""
    for _ in range(_polls(CONFIRM_SECONDS)):
        try:
            if _sent_count(await _bubbles(page), text) > before:
                return True
            await page.wait_for_timeout(POLL_MS)
        except Exception:
            return False
    return False


async def _refuse(compose: "Locator", why: str) -> NoReturn:
    """Empty the compose box (best effort: the refusal stands either way) and refuse."""
    try:
        await compose.fill("")
    except Exception:
        pass
    raise RuntimeError(f"REFUSED: {why} {NOTHING}")


async def send(query: str, text: str, *, confirm: bool = False, headed: bool = False) -> dict:
    """One text to one EXISTING conversation (the one `query` names in the list; never a new chat).

    Without confirm, a dry run: it resolves the conversation and stops, typing and clicking nothing. With confirm:
      1. open the conversation from the list and wait until the page shows exactly it (its id in the URL); a title
         the page shows must name it;
      2. type the text into the compose box and wait for the send button;
      3. then, last: the URL must still be that conversation's, the compose box must read back exactly the text, and
         the page must have tried no send of its own this command (a queued message or a retry the guard blocked);
      4. arm the guard's one slot and click send: the slot passes one SEND_MESSAGE, or closes after SEND_SECONDS;
      5. wait for the thread to show one more sent bubble with the text (confirmed). Never retried.
    """
    try:
        text = parse.message_text(text)
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    scope = SendScope() if confirm else None
    async with browser.site_page(send_policy(scope), send=confirm) as page:
        await open_app(page)
        row = await _find(page, query)
        out = {"dryRun": not confirm, "to": row["name"], "threadId": row["id"], "text": text}
        if not confirm:
            return out
        await _open(page, row)
        header = await page.evaluate(HEADER_JS, SELECTORS["header"])
        if header and not parse.names_match(header, row["name"]):
            raise RuntimeError(f"REFUSED: the open conversation is titled {header!r}, not {row['name']!r}. {NOTHING}")
        compose = await _compose(page)
        if compose is None:
            raise RuntimeError(f"REFUSED: no compose box in conversation {row['id']}. {NOTHING} {SNIFF_HINT}")
        before = _sent_count(await _loaded(page), text)
        await compose.fill(text)
        button = await _send_button(page)
        if button is None:
            await _refuse(compose, f"no enabled send button within {BUTTON_SECONDS}s.")
        # Last, right before the slot opens (finding the button can take seconds): still that conversation, still
        # exactly the text, and no send of the page's own so far.
        shown = parse.thread_id((await _snapshot(page)).get("path"))
        if shown != row["id"]:  # the compose box on screen now is another conversation's: leave it alone
            raise RuntimeError(f"REFUSED: the page moved to conversation {shown}, not {row['id']}. {NOTHING}")
        if ((await compose.evaluate(READ_BACK_JS)) or "").strip() != text:
            await _refuse(compose, "the compose box doesn't hold exactly the text.")
        if tried := _tried_send():
            await _refuse(compose, f"the page tried a send of its own ({'; '.join(tried)}).")
        scope.arm(SEND_SECONDS)
        try:
            failed = await _click(button)
            await _until(page, lambda: scope.sent, SEND_SECONDS)
        finally:
            scope.disarm()
        if not scope.sent:  # the guard is the only way a SEND_MESSAGE leaves, and it passed none
            why = f"the click failed ({failed})" if failed else f"the click sent nothing within {SEND_SECONDS}s"
            raise RuntimeError(f"{why}. {NOTHING}{blocked()}")
        confirmed = await _shows(page, text, before)
    return {**out, "sent": True, "confirmed": confirmed, "titleChecked": bool(header)}


async def _paired_tab(page: "Page") -> bool:
    """True once a tab on messages.google.com shows the conversation list."""
    tabs = [p for p in page.context.pages if not p.is_closed()]
    if not tabs:
        raise AuthRequired("The Chrome window closed before pairing finished.")
    for tab in tabs:
        if (urlsplit(tab.url).hostname or "") != APP_HOST:
            continue
        try:
            if parse.state(await tab.evaluate(STATE_JS, SELECTORS)) == "paired":
                return True
        except Exception:  # mid-navigation
            continue
    return False


async def login(timeout: int, on_state: Callable[[str], None] = lambda _state: None) -> dict:
    """A visible Chrome on Google Messages with no guard (pairing uses the relay's pairing calls, which every other
    command blocks). A person scans its QR code with the phone (Google Messages > Device pairing > QR code scanner).
    Done once a tab shows the conversation list; then, with the window closed, a fresh headless Chrome on the same
    profile must read as paired too, as every command will. Needs a person; only `identity login` calls it."""
    on_state("signing in")
    async with browser.open_page(None, mode="visible") as page:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not await _paired_tab(page):
            if loop.time() >= deadline:
                raise AuthRequired(f"Timed out after {timeout}s waiting for the phone to pair.")
            await asyncio.sleep(2)
    info = await status()
    if not info["signedIn"]:
        raise AuthRequired("The phone paired in the pairing window, but a fresh hidden Chrome on the same profile "
                           "isn't paired: turn on \"Remember this computer\" on the QR code page and pair again.")
    return info
