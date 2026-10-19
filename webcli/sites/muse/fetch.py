"""Browser-side work (musecli's fetch.py on the shared core): login, status, ask, chats, chat. (`models` needs no
browser: muse.ai has no model picker.)

Chat features drive muse.ai's own UI through selectors.py, the way a person would. Why not the API: Muse chat runs
over an end-to-end encrypted (Noise) WebSocket to the account's cloud VM (wss://*.metaaivm.com), with token signing
nobody has published. The page's own JS does that crypto; the guard lets that one socket host through.

Nothing sensitive reaches Python: cookies are never read, and the one in-page API call (POST /api/auth/check, which
answers with an access token) hands back only allowlisted name/flag fields. Every command but `login` sits behind
guard.POLICY.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator, Callable
from urllib.parse import urlsplit

from webcli.core import browser, guard
from webcli.core.errors import AuthRequired, NotFound
from webcli.sites.muse import parse
from webcli.sites.muse.endpoints import APP_HOSTS, AUTH_CHECK, EXPECTED_BLOCKS, MAIN, ORIGIN, ROUTES, thread_url
from webcli.sites.muse.guard import POLICY
from webcli.sites.muse.selectors import SELECTORS

if TYPE_CHECKING:
    from playwright.async_api import Locator, Page

HOME = APP_URL = ORIGIN + ROUTES["home"]
NEW_THREAD = ORIGIN + ROUTES["new_thread"]
CAPTURE_HINT = ("muse.ai's page may have changed: run `python -m webcli.core.capture muse` and fix "
                "webcli/sites/muse/endpoints.py or selectors.py.")
POLL = 0.5
SETTLE_SECONDS = 30.0  # page load, plus the auth.muse.ai -> facebook -> instagram session cycle
STABLE_SECONDS = 3.0  # answer text unchanged this long, with nothing streaming: done
NAV_SECONDS = 15.0  # an in-app navigation (a sidebar click, a new chat getting its id) shows in the URL by then

# outer: outermost matches only, so nested elements that match the same selector list count once.
# textOf: a message's text without its screen-reader and search copies (sel.surrogate). It reads the
# assistant body when there is one. A subtree holding a surrogate is read node by node, with line
# breaks around block elements the way innerText puts them.
_OUTER = """
const outer = (s) => { const els = Array.from(document.querySelectorAll(s));
  return els.filter((el) => !els.some((o) => o !== el && o.contains(el))); };
const shown = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
const links = (el) => Array.from(el.querySelectorAll('a[href]')).map((a) => ({href: a.href, text: a.innerText}));
const clean = (node) => {
  if (node.nodeType === 3) return node.textContent;
  if (node.nodeType !== 1 || node.matches(sel.surrogate)) return '';
  if (node.tagName === 'BR') return '\\n';
  const display = getComputedStyle(node).display;
  if (display === 'none') return '';
  const text = node.querySelector(sel.surrogate) ? Array.from(node.childNodes).map(clean).join('') : node.innerText;
  return display.startsWith('inline') || display === 'contents' ? text : '\\n' + text + '\\n';
};
const textOf = (el) => clean(el.querySelector(sel.assistant_body) || el);
"""
SNAPSHOT_JS = "(sel) => {" + _OUTER + """
  const any = (s) => Array.from(document.querySelectorAll(s)).some(shown);
  const replies = outer(sel.assistant_message);
  const last = replies[replies.length - 1];
  return {path: location.pathname, login: any(sel.login), composer: any(sel.composer), replies: replies.length,
          lastText: last ? textOf(last) : '', streaming: !!document.querySelector(sel.streaming),
          stop: any(sel.stop), approvals: outer(sel.approval).filter(shown).length};
}"""
TURN_JS = "([sel, from]) => {" + _OUTER + """
  return outer(sel.assistant_message).slice(from).map((el) => ({id: el.getAttribute('data-message-id'),
    role: 'assistant', text: textOf(el), links: links(el)}));
}"""
MESSAGES_JS = "(sel) => {" + _OUTER + """
  const both = sel.user_message + ', ' + sel.assistant_message;
  return outer(both).map((el) => ({id: el.getAttribute('data-message-id'),
    role: el.matches(sel.user_message) ? 'user' : 'assistant', text: textOf(el), links: links(el)}));
}"""
# A sidebar row: the title from span[title], the relative time ("2m", "Tue") from a sibling span.
ROWS_JS = """(sel) => Array.from(document.querySelectorAll(sel.thread_row)).map((row) => {
  const title = row.querySelector('span[title]');
  const others = title ? Array.from(title.parentElement.children).filter((el) => el !== title && el.tagName === 'SPAN') : [];
  return {title: title ? (title.getAttribute('title') || title.innerText) : row.innerText,
          updated: others.map((el) => el.innerText.trim()).filter(Boolean).pop() || null};
})"""
# Only allowlisted scalar fields leave the page: the body also carries an access token.
AUTH_CHECK_JS = """
async ([path, keys, nested]) => {
  const res = await fetch(path, {method: 'POST', credentials: 'include', redirect: 'manual', cache: 'no-store'});
  let data = null;
  try { data = JSON.parse(await res.text()); } catch (e) {}
  const pick = (o) => (o && typeof o === 'object' && !Array.isArray(o)) ? Object.fromEntries(Object.entries(o)
    .filter(([k, v]) => keys.includes(k) && (k === 'error' || typeof v !== 'object'))
    .map(([k, v]) => [k, k === 'error' ? true : v])) : {};
  const out = pick(data);
  for (const k of nested) if (data && data[k] && typeof data[k] === 'object') out[k] = pick(data[k]);
  return {status: res.type === 'opaqueredirect' ? 0 : res.status, data: out};
}
"""


@asynccontextmanager
async def open_page(*, headless: bool, send: bool = False) -> AsyncIterator["Page"]:
    """A page under the muse policy: a fresh tab in the identity daemon's Chrome, else a Chrome of this command's own
    (visible only for --headed). send=True is only for `ask` / `-p`."""
    async with browser.site_page(POLICY, mode="headless" if headless else "visible", send=send) as page:
        yield page


def why() -> str:
    """What the guard blocked this command (minus expected telemetry), then the capture hint."""
    seen = [b for b in dict.fromkeys(guard.state.blocked) if not any(e in b for e in EXPECTED_BLOCKS)]
    blocked = f"The guard blocked: {'; '.join(seen[:6])}." if seen else "The guard blocked no app requests."
    return f"{blocked} {CAPTURE_HINT}"


def signed_out() -> dict:
    return {"signedIn": False, "account": None, "access": None}


def _on_app(page: "Page") -> bool:
    return urlsplit(page.url).hostname in APP_HOSTS


async def _wait_for(page: "Page", selector: str, timeout_ms: float) -> bool:
    """True once `selector` matches; False after timeout_ms (the caller then reads what is there)."""
    try:
        await page.locator(selector).first.wait_for(state="attached", timeout=timeout_ms)
        return True
    except Exception as exc:
        if type(exc).__name__ != "TimeoutError":
            raise
        return False


async def snapshot(page: "Page") -> dict:
    return await page.evaluate(SNAPSHOT_JS, SELECTORS)


async def settle(page: "Page") -> dict | None:
    """Wait up to SETTLE_SECONDS until muse.ai shows its composer, its login form, or a hold page.
    The last snapshot, or None when the tab never got back to muse.ai."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SETTLE_SECONDS
    snap = None
    while True:
        if _on_app(page):
            try:
                snap = await snapshot(page)
            except Exception:  # mid-navigation: the execution context was destroyed
                snap = None
            if snap and (snap["composer"] or snap["login"] or parse.gate(snap["path"])):
                return snap
        if loop.time() >= deadline:
            return snap if _on_app(page) else None
        await asyncio.sleep(POLL)


async def steady(page: "Page") -> dict:
    """The snapshot once the reply count has held still for STABLE_SECONDS (up to SETTLE_SECONDS).
    A chat's history keeps rendering after the composer shows; counting too early would pass old
    replies off as new ones."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SETTLE_SECONDS
    snap, since = await snapshot(page), loop.time()
    while loop.time() - since < STABLE_SECONDS and loop.time() < deadline:
        await asyncio.sleep(POLL)
        now = await snapshot(page)
        if now["replies"] != snap["replies"]:
            since = loop.time()
        snap = now
    return snap


async def open_app(page: "Page", url: str = HOME) -> dict:
    """Go to a muse.ai page and make sure it is the signed-in chat UI. Returns the snapshot."""
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    snap = await settle(page)
    if snap is None:
        raise AuthRequired(f"muse.ai sent the browser to {urlsplit(page.url).hostname} to sign in.")
    if snap["login"]:
        raise AuthRequired("Not signed in to Muse.")
    hold = parse.gate(snap["path"])
    if hold:
        raise AuthRequired(f"Muse is holding this account on its {hold} page. A person signs in again and "
                           "finishes that page in the window.")
    if not snap["composer"]:
        raise RuntimeError(f"muse.ai loaded, but its chat composer never appeared. {why()}")
    return snap


async def session(page: "Page") -> dict:
    """{signedIn, account, access}. One check shared by login and status.

    Signed in: on muse.ai, no login form, and POST /api/auth/check answers 2xx without an
    explicit signed-out flag. The composer is not required, so a wrong composer selector can't make
    sign-in look impossible. access is "ok" or the hold page ("waitlist", "invite", "blocked", ...).
    """
    out = signed_out()
    if not _on_app(page):
        return out
    snap = await snapshot(page)
    if snap["login"]:
        return out
    hold = parse.gate(snap["path"])
    res = await page.evaluate(AUTH_CHECK_JS, [AUTH_CHECK, list(parse.AUTH_KEYS), list(parse.AUTH_NESTED)])
    if not 200 <= res["status"] < 300:
        return out
    ok, name = parse.auth_check(res["data"])
    if ok:
        out.update(signedIn=True, account=name, access=hold or "ok")
    return out


async def status(*, headed: bool = False) -> dict:
    async with open_page(headless=not headed) as page:
        await page.goto(HOME, wait_until="domcontentloaded", timeout=60_000)
        await settle(page)
        return await session(page)


async def login(timeout: int, on_state: Callable[[str], None] = lambda _state: None) -> dict:
    """A visible Chrome on muse.ai with no guard (plain Chrome, policy None: the Meta sign-in crosses facebook.com and
    instagram.com, maybe in a popup); wait until a tab is signed in with full access. on_state hears "signing in"
    once. Returns {signedIn, account, access, name} (name: what `identity login` prints)."""
    on_state("signing in")
    async with browser.open_page(None, mode="visible") as page:
        await page.goto(HOME, wait_until="domcontentloaded")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last = None
        while loop.time() < deadline:
            await asyncio.sleep(2)
            tabs = [p for p in page.context.pages if not p.is_closed()]
            if not tabs:
                raise AuthRequired("The Chrome window closed before sign-in finished.")
            for tab in tabs:  # Facebook/Instagram sign-in may finish in a popup or another tab
                try:
                    info = await session(tab)
                except Exception:  # tab is mid-navigation
                    continue
                if info["signedIn"]:
                    if info["access"] == "ok":
                        return {**info, "name": info["account"]}
                    last = info
        if last:
            raise AuthRequired(f"Signed in, but Muse still shows its {last['access']} page.")
        raise AuthRequired(f"Timed out after {timeout}s waiting for Muse sign-in.")


async def _wait_url(page: "Page", test: Callable[[str], bool]) -> bool:
    """True once test(page.url) holds; False after NAV_SECONDS. In-app navigation changes the URL in place."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + NAV_SECONDS
    while not test(page.url):
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(POLL)
    return True


async def wait_for_answer(page: "Page", before: dict, timeout: float) -> list[dict]:
    """Every assistant message after the ones in `before` (the snapshot taken before sending), once
    the turn is finished.

    Finished: the newest reply has text, nothing on the page is streaming (SELECTORS["streaming"],
    or a stop button if that selector ever matches), and the text hasn't changed for
    STABLE_SECONDS. A stop button that was already showing before the send is ignored. A new
    message restarts the clock. Only approval prompts that appear after the send count. The
    selectors match by data-message-role, so the user's own message is never taken for a reply
    (an answer that repeats the prompt, "pong" to "pong", counts).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_text, changed_at = None, loop.time()
    stop_counts = not before["stop"]
    while loop.time() < deadline:
        await asyncio.sleep(POLL)
        snap = await snapshot(page)
        if snap["approvals"] > before["approvals"]:
            chat = parse.thread_id(page.url)
            raise RuntimeError(f"Muse is asking for approval in chat {chat or '(new)'}. llmcli muse never approves; "
                               f"decide at {thread_url(chat) if chat else HOME}.")
        busy = snap["streaming"] or (snap["stop"] and stop_counts)
        text = snap["lastText"].strip()
        if snap["replies"] <= before["replies"] or not text:
            continue
        if (snap["replies"], text) != last_text:
            last_text, changed_at = (snap["replies"], text), loop.time()
            continue
        if not busy and loop.time() - changed_at >= STABLE_SECONDS:
            raw = await page.evaluate(TURN_JS, [SELECTORS, before["replies"]])
            return [m for m in map(parse.message, raw) if m]
    chat = parse.thread_id(page.url)
    seen = f"{len(last_text[1])} chars so far" if last_text else "no reply seen"
    later = f" Read it later: llmcli muse chats show {chat}" if chat else ""
    raise TimeoutError(f"No finished answer after {timeout:g}s ({seen}).{later} {why()}")


async def ask(prompt: str, *, chat_id: str | None = None, timeout: float = 300, headed: bool = False) -> dict:
    """Send one prompt and return {chatId, chatUrl, prompt, answer, citations}.

    chatId comes from the URL: a new chat moves from /thread/new to /thread/<uuid> once Muse has
    created it, and the main chat stays at "/" ("main"). There is no model to report: muse.ai has no model
    picker, Muse chooses its model itself.
    """
    async with open_page(headless=not headed, send=True) as page:
        await open_app(page, thread_url(chat_id) if chat_id else NEW_THREAD)
        before = await steady(page)
        composer = page.locator(SELECTORS["composer"]).filter(visible=True).first
        await composer.fill(prompt)
        await composer.press("Enter")
        replies = await wait_for_answer(page, before, timeout)
        await _wait_url(page, lambda url: urlsplit(url).path != ROUTES["new_thread"])
        opened = parse.thread_id(page.url) or chat_id
    return {"chatId": opened, "chatUrl": thread_url(opened) if opened else None, "prompt": prompt,
            "answer": "\n\n".join(r["text"] for r in replies),
            "citations": list({c["url"]: c for r in replies for c in r.get("citations", [])}.values())}


async def _back_to_main(page: "Page", main_row: "Locator") -> None:
    """From a side chat back to the main chat: its sidebar row, else the browser's Back."""
    if await main_row.is_visible():
        await main_row.click()
    else:
        await page.go_back()
    if not await _wait_url(page, lambda url: parse.thread_id(url) == MAIN):
        raise RuntimeError(f"Couldn't get back to the main chat from {urlsplit(page.url).path}. {why()}")


async def chats(limit: int, *, headed: bool = False) -> list[dict]:
    """Up to `limit` chats in muse.ai's sidebar order: "Main chat" (id "main"), then the side chats.

    Side-chat rows carry no link or id, so each one is clicked in turn, its /thread/<id> is read
    off the URL, and the Main chat row (or the browser's Back) is clicked to come back. Those are
    in-app navigations: no HTTP write, but opening a chat may mark it seen (chat.mark_seen inside
    the VM socket), as viewing it in a browser does. Nothing else is clicked.
    """
    async with open_page(headless=not headed) as page:
        await open_app(page)
        await _wait_for(page, SELECTORS["thread_row"], 8_000)
        row = page.locator(SELECTORS["thread_row"])
        out: list[dict] = []
        for index in range(limit):
            shown = await page.evaluate(ROWS_JS, SELECTORS)  # again each time: the list may have moved
            if index >= len(shown):
                break
            if index == 0:  # "Main chat", the page open now
                out.append(parse.chat_row(shown[0], MAIN))
                continue
            await row.nth(index).click()
            if not await _wait_url(page, lambda url: parse.thread_id(url) not in (None, MAIN)):
                raise RuntimeError(f"Clicking chat {shown[index].get('title')!r} in the sidebar didn't open "
                                   f"a /thread/<id> page. {why()}")
            opened = parse.thread_id(page.url)
            if all(r["id"] != opened for r in out):
                out.append(parse.chat_row(shown[index], opened))
            await _back_to_main(page, row.first)
        if not out:
            raise NotFound("No chats found.", hint=why())
        return out


async def chat(chat_id: str, *, headed: bool = False) -> dict:
    """One chat's messages, oldest first: {id, url, messages: [{id, role, text, citations?}]}."""
    async with open_page(headless=not headed) as page:
        await open_app(page, thread_url(chat_id))
        if not parse.on_chat(page.url, chat_id):
            raise NotFound(f"No chat {chat_id}: muse.ai went to {urlsplit(page.url).path} instead.")
        await _wait_for(page, f"{SELECTORS['user_message']}, {SELECTORS['assistant_message']}", 15_000)
        await steady(page)
        messages = [m for m in map(parse.message, await page.evaluate(MESSAGES_JS, SELECTORS)) if m]
        if not messages:
            raise NotFound(f"No messages found in chat {chat_id}.", hint=why())
        return {"id": chat_id, "url": thread_url(chat_id), "messages": messages}
