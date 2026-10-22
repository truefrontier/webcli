"""Browser-side work for webcli whatsapp: login, status, the chat list, one chat's messages, search, and the one
write, send.

WhatsApp Web's traffic is one end-to-end encrypted WebSocket, so every verb reads the rendered page: a pane's
outerHTML, parsed offline by parse.py (the same code the fixture tests run). Every command but `login` runs behind
guard.POLICY in a hidden Chrome on the whatsapp:default profile.

Gentle by design (WhatsApp's terms prohibit automated access): one command at a time, on demand only, no polling
loops, a pause after every scroll, at most MAX_HISTORY_SCROLLS / MAX_LIST_SCROLLS scrolls. A block or challenge page
stops the command (exit 5, "STOPPED: ...") and nothing on it is clicked.

What each verb touches in the page: `chats` scrolls the chat list; `search` types into the search box; `read` (and
`send`) click the chat's row (from the list, else from search results under "Chats") and `read` scrolls the
conversation up for older messages. Opening a chat is what WhatsApp counts as reading it: it may send read receipts
(blue ticks) for the messages it shows, unless read receipts are off on the phone. Only a confirmed `send` types into
a compose box and clicks Send; see send().
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Callable, NoReturn
from urllib.parse import urlsplit

from webcli.core import browser
from webcli.core.errors import AuthRequired, NotFound, UsageError
from webcli.sites.google import blocked
from webcli.sites.whatsapp import dom, parse
from webcli.sites.whatsapp.endpoints import (
    APP_HOST,
    APP_URL,
    BLOCK_TEXT,
    CONFIRM_SECONDS,
    DEFAULT_LIMIT,
    LOAD_SECONDS,
    LOGGED_OUT_TEXT,
    LOGIN_URL,
    MAX_HISTORY_SCROLLS,
    MAX_LIMIT,
    MAX_LIST_SCROLLS,
    OPEN_SECONDS,
    POLL_MS,
    ROWS_SECONDS,
    SCROLL_MS,
    SCROLL_STEP,
    SEARCH_SETTLE_MS,
    SECTIONS,
    SELECTORS,
    SETTLE_MS,
    STALL_SCROLLS,
    TYPE_DELAY_MS,
)
from webcli.sites.whatsapp.guard import POLICY

if TYPE_CHECKING:
    from playwright.async_api import Locator, Page

S = SELECTORS
SNIFF_HINT = "WhatsApp Web may have changed: fix the selectors in webcli/sites/whatsapp/endpoints.py."
NOT_LINKED = "WhatsApp Web isn't linked to your phone here (it shows its QR code)."
NOTHING = "Nothing was sent."
CLICK_MS = 5_000

STATE_JS = """/* wa:state */ (s) => {
  const n = sel => { try { return document.querySelectorAll(sel).length; } catch (e) { return 0; } };
  const body = document.body ? document.body.innerText || '' : '';
  const low = body.toLowerCase();
  const label = el => (el.innerText || el.getAttribute('aria-label') || '').trim();
  const buttons = [...document.querySelectorAll('button, [role="button"]')].map(label);
  const list = n(s.chatList) > 0;
  return {path: location.pathname, list, rows: n(s.row), main: n(s.main) > 0,
          qr: !list && (n(s.qr) > 0 || s.qrText.some(t => body.includes(t))),
          loading: n(s.loading) > 0 || s.loadingText.some(t => body.includes(t)),
          useHere: buttons.some(t => s.useHere.includes(t)),
          block: list ? null : (s.blockText.find(t => low.includes(t)) || null),
          loggedOut: !list && s.loggedOutText.some(t => low.includes(t))};
}"""
USE_HERE_JS = """/* wa:use-here */ (labels) => {
  const label = el => (el.innerText || el.getAttribute('aria-label') || '').trim();
  const el = [...document.querySelectorAll('button, [role="button"]')].find(b => labels.includes(label(b)));
  if (el) { el.click(); return true; }
  return false;
}"""
HTML_JS = """/* wa:html */ (sels) => {
  for (const sel of sels) { const el = document.querySelector(sel); if (el) return el.outerHTML; }
  return null;
}"""
SCROLL_LIST_JS = """/* wa:scroll-list */ (sels) => {
  for (const sel of sels) {
    const el = document.querySelector(sel);
    if (!el) continue;
    const before = el.scrollTop;
    el.scrollTop = before + Math.max(200, el.clientHeight * 0.8);
    return el.scrollTop !== before;
  }
  return false;
}"""
# Scroll the conversation up by about a screen (not straight to the top: WhatsApp only renders the rows near the
# viewport, so each step is parsed on the way). True when it moved; at the top WhatsApp loads older rows by itself.
SCROLL_UP_JS = """/* wa:scroll-up */ ([sels, step]) => {
  let el = null;
  for (const sel of sels) {
    const found = document.querySelector(sel);
    if (found && found.scrollHeight > found.clientHeight) { el = found; break; }
  }
  const main = document.querySelector('#main');
  if (!el && main) {
    el = [...main.querySelectorAll('div')].find(d => /(auto|scroll)/.test(getComputedStyle(d).overflowY)
                                                     && d.scrollHeight > d.clientHeight + 10) || null;
  }
  if (!el) return false;
  const before = el.scrollTop;
  el.scrollTop = Math.max(0, before - Math.max(200, el.clientHeight * step));
  return el.scrollTop !== before;
}"""
# Mark the one row named `name` (exactly, as parse reads a row's name) for a real click. In search results only rows
# under a chats heading count (never a contact or a message hit). Returns how many rows matched: only 1 is marked.
MARK_JS = """/* wa:mark */ ([s, sections, name]) => {
  const clean = t => (t || '').replace(/[\\u200b-\\u200f\\u202a-\\u202e\\u2066-\\u2069\\ufeff]/g, '')
                                .replace(/\\s+/g, ' ').trim();
  document.querySelectorAll('[data-webcli-target]').forEach(e => e.removeAttribute('data-webcli-target'));
  const pane = s.pane.map(sel => document.querySelector(sel)).find(Boolean);
  if (!pane) return 0;
  const all = [...pane.querySelectorAll(s.row)];
  const rows = all.filter(r => !all.some(o => o !== r && o.contains(r)));
  let kind = 'chat';
  const hits = [];
  for (const row of rows) {
    const text = clean(row.innerText || row.textContent).toLowerCase();
    if (text in sections) { kind = sections[text]; continue; }
    if (kind !== 'chat') continue;
    for (const sel of s.rowName) {
      const el = row.querySelector(sel);
      if (el) { if (clean(el.getAttribute('title') || el.textContent) === clean(name)) hits.push(row); break; }
    }
  }
  if (hits.length === 1) { hits[0].setAttribute('data-webcli-target', '1'); hits[0].scrollIntoView({block: 'center'}); }
  return hits.length;
}"""
TARGET = '[data-webcli-target="1"]'


def _polls(seconds: float) -> int:
    return max(1, int(seconds * 1000 / POLL_MS))


def _state_args() -> dict:
    return {"chatList": S["chatList"], "row": S["row"], "main": S["main"], "qr": S["qr"], "qrText": S["qrText"],
            "loading": S["loading"], "loadingText": S["loadingText"], "useHere": S["useHere"],
            "blockText": list(BLOCK_TEXT), "loggedOutText": list(LOGGED_OUT_TEXT)}


def _mark_args() -> dict:
    return {"pane": dom.alternatives(S["chatList"]), "row": S["row"], "rowName": dom.alternatives(S["rowName"])}


async def _snapshot(page: "Page") -> dict:
    return await page.evaluate(STATE_JS, _state_args())


def stop_blocked(what: str) -> NoReturn:
    raise RuntimeError(f"STOPPED: WhatsApp Web shows a block or challenge page ({what!r}). Nothing on it was clicked "
                       "and nothing was worked around: a person should look at it (`webcli whatsapp login` opens a "
                       "visible window).")


async def _state(page: "Page", snap: dict) -> str:
    """parse.state(snap), after clicking "Use here" when another tab of this profile holds WhatsApp. AuthRequired
    when the app isn't linked; RuntimeError (STOPPED) on a block or challenge page."""
    kind = parse.state(snap)
    if kind == "blocked":
        stop_blocked(snap.get("block") or "blocked")
    if kind == "unlinked":
        raise AuthRequired(NOT_LINKED)
    if kind == "use_here":
        await page.evaluate(USE_HERE_JS, S["useHere"])
    return kind


async def ready(page: "Page") -> None:
    """Wait for the chat list (it can take a while after a fresh load: WhatsApp syncs first)."""
    for _ in range(_polls(LOAD_SECONDS)):
        snap = await _snapshot(page)
        if await _state(page, snap) == "linked" and (snap.get("rows") or not snap.get("loading")):
            await page.wait_for_timeout(SETTLE_MS)
            return
        await page.wait_for_timeout(POLL_MS)
    raise RuntimeError(f"WhatsApp Web didn't show its chat list within {LOAD_SECONDS}s.{blocked()} {SNIFF_HINT}")


async def open_app(page: "Page") -> None:
    await page.goto(APP_URL, wait_until="domcontentloaded", timeout=60_000)
    landed = urlsplit(page.url).hostname or ""
    if landed != APP_HOST:
        raise RuntimeError(f"{APP_HOST} redirected to {landed}.")
    await ready(page)


async def _html(page: "Page", selector: str) -> str | None:
    return await page.evaluate(HTML_JS, dom.alternatives(selector))


async def _list_rows(page: "Page") -> list[dict]:
    return parse.chats(await _html(page, S["chatList"]), positions=True)


async def _first_usable(page: "Page", selector: str) -> "Locator | None":
    for sel in dom.alternatives(selector):
        loc = page.locator(sel).first
        try:
            if await loc.count() and await loc.is_visible():
                return loc
        except Exception:  # detached mid-check
            continue
    return None


async def _search(page: "Page", query: str) -> list[dict]:
    """Type `query` into WhatsApp's search box (searched on this computer) and read the results."""
    box = await _first_usable(page, S["search"])
    if box is None:
        raise RuntimeError(f"No search box in WhatsApp Web. {SNIFF_HINT}")
    await box.click(timeout=CLICK_MS)
    await page.keyboard.type(query, delay=TYPE_DELAY_MS)
    await page.wait_for_timeout(SEARCH_SETTLE_MS)
    rows = parse.search(await _html(page, S["chatList"]))
    if not rows:  # results can take a moment longer; look once more
        await page.wait_for_timeout(SEARCH_SETTLE_MS)
        rows = parse.search(await _html(page, S["chatList"]))
    return rows


async def _resolve(page: "Page", query: str) -> dict:
    """The existing chat `query` names: one row in the loaded list with exactly that name, else WhatsApp's search
    results under "Chats" (parse.resolve: exact, then ignoring case, then containing). Contacts without a chat are
    never candidates. NotFound when nothing matches; UsageError listing the candidates when several do."""
    exact = [row for row in await _list_rows(page) if parse.names_equal(row["name"], query)]
    if len(exact) == 1:
        return exact[0]
    chats = [row for row in await _search(page, query) if row["type"] == "chat"]
    found = parse.resolve(chats, query)
    if found is None:
        raise NotFound(f"No existing WhatsApp chat matches {query!r}.",
                       hint="Only existing chats are opened (never a new contact): see `webcli whatsapp search`.")
    return found


async def _header(page: "Page") -> str | None:
    return parse.header(await _html(page, S["main"]))


async def _open(page: "Page", row: dict) -> str:
    """Click `row`'s chat (in the list or the search results) and wait until the conversation header names it
    exactly. Returns the header."""
    count = await page.evaluate(MARK_JS, [_mark_args(), {k: v for k, v in SECTIONS.items() if v}, row["name"]])
    if count == 0:
        raise NotFound(f"The chat {row['name']!r} isn't shown in WhatsApp's list or search results.")
    if count > 1:
        raise UsageError(f"{count} chats are named exactly {row['name']!r}. Nothing was opened.",
                         hint="WhatsApp shows several chats with that name; rename one on the phone.")
    await page.locator(TARGET).first.click(timeout=CLICK_MS)
    shown = None
    for _ in range(_polls(OPEN_SECONDS)):
        shown = await _header(page)
        if parse.names_equal(shown, row["name"]):
            await page.wait_for_timeout(SETTLE_MS)
            return shown
        await page.wait_for_timeout(POLL_MS)
    raise RuntimeError(f"The chat {row['name']!r} didn't open within {OPEN_SECONDS}s (the header shows "
                       f"{shown!r}). {SNIFF_HINT}")


async def _thread(page: "Page", chat: str) -> dict:
    return parse.thread(await _html(page, S["main"]), chat)


def _clamp(limit: int | None) -> int:
    return min(limit or DEFAULT_LIMIT, MAX_LIMIT)


async def status(*, headed: bool = False) -> dict:
    """{signedIn, linked, status}: linked when the app shows its chat list rather than its QR code."""
    async with browser.site_page(POLICY) as page:
        try:
            await open_app(page)
        except AuthRequired:
            return {"signedIn": False, "linked": False, "status": "needs_link"}
    return {"signedIn": True, "linked": True, "status": "active"}


async def chats(limit: int | None = None, unread: bool = False, *, headed: bool = False) -> list[dict]:
    """The chat list, top first (pinned chats, then the newest). Scrolls the list (gently, at most MAX_LIST_SCROLLS
    times) until `limit` rows (only unread ones with `unread`) are in hand or the list ends."""
    limit = _clamp(limit)
    keep = (lambda r: r["unread"] > 0) if unread else (lambda r: True)
    async with browser.site_page(POLICY) as page:
        await open_app(page)
        seen = await _list_rows(page)
        stall = 0
        for _ in range(MAX_LIST_SCROLLS):
            if sum(map(keep, seen)) >= limit or stall >= STALL_SCROLLS:
                break
            if not await page.evaluate(SCROLL_LIST_JS, dom.alternatives(S["pane"])):
                break
            await page.wait_for_timeout(SCROLL_MS)
            stall = 0 if parse.merge(seen, await _list_rows(page)) else stall + 1
    return parse.public([row for row in seen if keep(row)][:limit])


async def _rendered(page: "Page", chat: str) -> dict:
    """The opened chat's first look (parse.thread) once its message rows have rendered and parse, waiting up to
    ROWS_SECONDS. RuntimeError when no row shows, or rows show but none parses: never an empty read that looks
    like an empty chat."""
    found = {"rows": 0, "messages": []}
    for _ in range(_polls(ROWS_SECONDS)):
        found = await _thread(page, chat)
        if found["messages"]:
            await page.wait_for_timeout(SETTLE_MS)  # let the rest of the screen hydrate
            return await _thread(page, chat)
        await page.wait_for_timeout(POLL_MS)
    if not found["rows"]:
        raise RuntimeError(f"The chat {chat!r} opened, but no message rows rendered within {ROWS_SECONDS}s. "
                           f"{SNIFF_HINT}")
    raise RuntimeError(f"The chat {chat!r} opened and shows {found['rows']} message rows, but none could be read "
                       f"(sender, time, text) within {ROWS_SECONDS}s. {SNIFF_HINT}")


async def messages(query: str, limit: int | None = None, since: str | None = None, *,
                   headed: bool = False) -> dict:
    """{chat, messages, rows, missing}: the last `limit` messages of the chat `query` names, oldest first (at or
    after `since`, a local ISO time, when given). WhatsApp renders only the rows near the viewport, so it parses
    each screen as it scrolls up (about a screen at a time, SCROLL_MS apart, at most MAX_HISTORY_SCROLLS times)
    and merges them by message id. `rows`: message rows seen; `missing`: rows inside the returned span that never
    rendered. Opening the chat may send WhatsApp's read receipts (see the module doc)."""
    limit = _clamp(limit)
    cut = datetime.fromisoformat(since) if since else None
    cut_iso = cut.isoformat(timespec="minutes") if cut else None
    async with browser.site_page(POLICY) as page:
        await open_app(page)
        row = await _resolve(page, query)
        await _open(page, row)
        seen = parse.Collected()
        seen.add(await _rendered(page, row["name"]))
        stall = 0
        for _ in range(MAX_HISTORY_SCROLLS):
            rows = seen.messages()
            oldest = next((r["datetime"] for r in rows if r["datetime"]), None)
            if cut_iso is not None and oldest and oldest < cut_iso:
                break
            if len(parse.since_filter(rows, cut)) >= limit or stall >= STALL_SCROLLS:
                break
            moved = await page.evaluate(SCROLL_UP_JS, [dom.alternatives(S["scroller"]), SCROLL_STEP])
            await page.wait_for_timeout(SCROLL_MS)
            added = seen.add(await _thread(page, row["name"]))
            stall = 0 if moved or added else stall + 1
    rows = parse.since_filter(seen.messages(), cut)[-limit:]
    return {"chat": row["name"], "messages": parse.public(rows), "rows": seen.rows, "missing": seen.missing(rows)}


async def search(query: str, limit: int | None = None, *, headed: bool = False) -> list[dict]:
    """WhatsApp's own search for `query`: matching chats, contacts, and messages, as its results pane shows them.
    Opens nothing."""
    limit = _clamp(limit)
    async with browser.site_page(POLICY) as page:
        await open_app(page)
        rows = await _search(page, query)
    return rows[:limit]


async def _compose_text(compose: "Locator") -> str:
    try:
        return (await compose.inner_text()) or ""
    except Exception:
        return ""


async def _clear(page: "Page", compose: "Locator") -> None:
    """Empty the compose box (best effort: a refusal stands either way)."""
    try:
        await compose.click(timeout=CLICK_MS)
        await page.keyboard.press("ControlOrMeta+A")
        await page.keyboard.press("Backspace")
    except Exception:
        pass


async def _refuse(page: "Page", compose: "Locator | None", why: str) -> NoReturn:
    if compose is not None:
        await _clear(page, compose)
    raise RuntimeError(f"REFUSED: {why} {NOTHING}")


async def _type(page: "Page", compose: "Locator", text: str) -> None:
    """Type `text` into the compose box without pressing Enter (Enter sends): lines are joined with Shift+Enter."""
    await compose.click(timeout=CLICK_MS)
    for i, line in enumerate(text.split("\n")):
        if i:
            await page.keyboard.press("Shift+Enter")
        if line:
            await page.keyboard.insert_text(line)


async def _confirm(page: "Page", chat: str, text: str, before: int) -> tuple[bool, str | None]:
    """Whether the thread shows one more message of yours with exactly `text`, with a tick (pending, sent,
    delivered, or read), within CONFIRM_SECONDS; and that tick."""
    status = None
    for _ in range(_polls(CONFIRM_SECONDS)):
        try:
            mine = parse.sent_matches((await _thread(page, chat))["messages"], text)
        except Exception:
            return False, None
        if len(mine) > before:
            status = mine[-1]["status"]
            if status:
                return True, status
        await page.wait_for_timeout(POLL_MS)
    return False, status


async def send(query: str, text: str, expect_chat: str, *, confirm: bool = False, headed: bool = False) -> dict:
    """One text to one EXISTING chat (never a new contact), one message per call.

    Without confirm, a dry run: it resolves the chat from the list or search and stops; it opens, types, and clicks
    nothing. Returns {dryRun, chat, expectChat, expectChatMatches, text}.
    With confirm:
      1. the resolved chat's name must equal expect_chat exactly, else REFUSED before anything is opened;
      2. open the chat; its header must equal expect_chat exactly;
      3. the compose box must be there and empty (an unsent draft is left alone: REFUSED);
      4. type the text (no Enter), then: the compose box reads back exactly the text and the header still equals
         expect_chat; else the box is emptied and REFUSED;
      5. click Send once, then wait for the thread to show one more message of yours with that text and a tick.
    Never retried. Returns {..., sent: true, confirmed, status}."""
    try:
        text = parse.message_text(text)
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    expect = dom.clean(expect_chat)
    if not expect:
        raise UsageError("--expect-chat is empty.")
    async with browser.site_page(POLICY) as page:
        await open_app(page)
        row = await _resolve(page, query)
        matches = parse.names_equal(row["name"], expect)
        out = {"dryRun": not confirm, "chat": row["name"], "expectChat": expect, "expectChatMatches": matches,
               "text": text}
        if not confirm:
            return out
        if not matches:
            raise RuntimeError(f"REFUSED: {query!r} is the chat {row['name']!r}, not --expect-chat {expect!r}. "
                               f"{NOTHING}")
        shown = await _open(page, row)
        if not parse.names_equal(shown, expect):
            raise RuntimeError(f"REFUSED: the open chat's header is {shown!r}, not --expect-chat {expect!r}. {NOTHING}")
        compose = await _first_usable(page, S["compose"])
        if compose is None:
            raise RuntimeError(f"REFUSED: the chat {row['name']!r} has no compose box (you may not be able to send "
                               f"there). {NOTHING}")
        if (await _compose_text(compose)).strip():
            raise RuntimeError(f"REFUSED: the chat {row['name']!r} has an unsent draft in its compose box; it was "
                               f"left alone. {NOTHING}")
        before = len(parse.sent_matches((await _thread(page, row["name"]))["messages"], text))
        await _type(page, compose, text)
        if not parse.compose_matches(await _compose_text(compose), text):
            await _refuse(page, compose, "the compose box doesn't hold exactly the text.")
        if not parse.names_equal(await _header(page), expect):
            await _refuse(page, compose, f"the open chat is no longer {expect!r}.")
        button = await _first_usable(page, S["sendButton"])
        if button is None:
            await _refuse(page, compose, "no Send button.")
        try:
            await button.click(timeout=CLICK_MS)
        except Exception as exc:
            await _refuse(page, compose, f"the Send click failed ({type(exc).__name__}).")
        confirmed, status = await _confirm(page, row["name"], text, before)
        if not confirmed and parse.compose_matches(await _compose_text(compose), text):
            await _clear(page, compose)
            raise RuntimeError(f"the Send click sent nothing within {CONFIRM_SECONDS}s (the text stayed in the "
                               f"compose box, now emptied). {NOTHING}")
    return {**out, "sent": True, "confirmed": confirmed, "status": status}


async def _linked_tab(page: "Page") -> bool:
    """True once a tab on web.whatsapp.com shows the chat list. STOPPED on a block or challenge page."""
    tabs = [p for p in page.context.pages if not p.is_closed()]
    if not tabs:
        raise AuthRequired("The Chrome window closed before linking finished.")
    for tab in tabs:
        if (urlsplit(tab.url).hostname or "") != APP_HOST:
            continue
        try:
            snap = await tab.evaluate(STATE_JS, _state_args())
        except Exception:  # mid-navigation
            continue
        kind = parse.state(snap)
        if kind == "blocked":
            stop_blocked(snap.get("block") or "blocked")
        if kind == "linked" and snap.get("rows"):
            return True
    return False


async def login(timeout: int, on_state: Callable[[str], None] = lambda _state: None) -> dict:
    """A visible Chrome on WhatsApp Web with no guard. A PERSON scans its QR code with the phone (WhatsApp >
    Settings > Linked devices > Link a device); nothing here scans, clicks, or works around it. Done once a tab shows
    the chat list; then, with the window closed, a fresh hidden Chrome on the same profile must read as linked too,
    as every command will. Only `webcli whatsapp login` / `webcli identity login whatsapp:default` call it."""
    on_state("signing in")
    async with browser.open_page(None, mode="visible") as page:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not await _linked_tab(page):
            if loop.time() >= deadline:
                raise AuthRequired(f"Timed out after {timeout}s waiting for the phone to link (scan the QR code).")
            await asyncio.sleep(2)
        await asyncio.sleep(5)  # let the first sync settle before the window closes
    info = await status()
    if not info["signedIn"]:
        raise AuthRequired("The phone linked in the visible window, but a fresh hidden Chrome on the same profile "
                           "isn't linked: run `webcli whatsapp login` again.")
    return info
