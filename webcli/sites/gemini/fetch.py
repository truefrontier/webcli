"""Browser-side work: login, status, chats, models, ask.

Chat history comes from Gemini's own batchexecute RPCs, called with fetch() inside a
gemini.google.com page (credentials: 'include'). The page's cookies and its `at` token,
build label, and session id (WIZ_global_data) stay in the page: the JS below reads them
there and hands Python only the response body. (The request guard in guard.py does see
each outgoing batchexecute body, in memory only, to check its rpcids.)

`ask` and `models` drive the UI instead (selectors.py), because sending a prompt needs
tokens only the app can mint. Gemini now sends the prompt over a WebChannel (processSession,
protobuf, never read here), so `ask` reads the answer off the page: the rendered text of the new
model-response once it stops changing. If Gemini goes back to StreamGenerate, that response is
parsed instead: it carries the markdown, chat id, and citations.

A reply that cites its sources shows a chip for them at the end of each cited paragraph ("MD + 1", "Wikipedia"). The
page names a web source only by its site, so once such a reply settles, `ask` reads its turn from the chat's
transcript instead: the stored markdown with [cite: N] markers, and every source's file name or title and URL
(parse.cite_refs). parse.footnote turns those into [^n] marks and a footnote block.

An image reply is done once its generated images have loaded (its text may be empty). `ask --image-out`
then reads the chat's transcript for the images' ids and downloads each at full size the way Gemini's
"Download full size image" button does (c8o8Fe, then a few image-server hops; see endpoints.IMAGE_HOSTS).
`ask --canvas` turns on the Canvas tool the same way and, once the reply has settled, reads the chat's
transcript for the Canvas doc's markdown and the answer text around it (the page shows the doc's chip inline).
"""

from __future__ import annotations

import asyncio
import html
import math
import re
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator
from urllib.parse import parse_qsl, urljoin, urlsplit

from webcli.core import browser, guard
from webcli.core.errors import AuthRequired, NotFound, RateLimited
from webcli.sites.gemini import images, parse, selectors as sel
from webcli.sites.gemini.guard import POLICY, describe, image_url_allowed
from webcli.sites.gemini.endpoints import APP_HOST, APP_URL, CHANNEL_HOST, CHANNEL_PATH, GENERATE_PATH, LOGIN_URL, RPC

if TYPE_CHECKING:
    from playwright.async_api import Locator, Page, Request, Response

SNIFF_HINT = ("Gemini's API may have changed; run `python -m webcli.core.capture gemini` and fix "
              "webcli/sites/gemini/endpoints.py.")
SELECTOR_HINT = "Gemini's page may have changed; rerun with --headed and fix gemini_web_cli/selectors.py."
CHATS_FIRST_PAGE, CHATS_NEXT_PAGE = 34, 20  # what the web app asks for
TURNS_PAGE = 10
MAX_PAGES = 200
STABLE_SECONDS = 1.5  # quiet period after an explicit DOM completion marker and no visible stop control
SETTLE_SECONDS = 1.0  # --chat: the history counts as rendered once the response count holds this long
SEND_SECONDS = 60  # after the click: no sign of the prompt leaving by then means it was never sent
MENU_SECONDS = 30  # opening the model menu, retries included
MENU_CLICKS = 3
MENU_OPEN_MS = 4_000  # a click whose menu rows haven't shown by then gets Escape and another click
PICK_SECONDS = 3  # after clicking a model row: how long the picker's label gets to name it
THINKING_LEVELS = ("low", "medium", "high")  # the Thinking level submenu's rows, in menu order
TURN_SECONDS = 20  # after a reply renders: how long the transcript gets to list its images or Canvas doc
UPLOAD_SECONDS = 180  # after attaching: how long the --file uploads get to finish
IMAGE_HOPS = 5  # redirects and text/plain "next URL" answers followed per image download
IMAGE_TRIES = 3  # times an image download starts over after a 403, 429, or 5xx
SENT_HINT = "The prompt was sent: check `llmcli gemini chats list` before sending it again."
RATE_LIMIT_CODE = 1037  # GOOGLE_ERRORS: the usage limit for this model was reached
GOOGLE_ERRORS = {
    1013: "a temporary Gemini error; retry",
    1037: "the usage limit for this model was reached; try another --model or wait",
    1050: "the model doesn't match this chat's history; use the chat's model or --new",
    1052: "the model is unavailable",
    1060: "Google temporarily blocked requests from this IP",
}

# Runs inside the page. Tokens never leave it; Python gets {status, contentType, body}.
BATCH_JS = """
async ({rpcid, payload, sourcePath}) => {
  const wiz = window.WIZ_global_data || {};
  if (!wiz.SNlM0e) return {status: 0, contentType: '', body: ''};
  const params = new URLSearchParams({rpcids: rpcid, 'source-path': sourcePath,
    hl: document.documentElement.lang || 'en', _reqid: String(100000 + Math.floor(Math.random() * 900000)), rt: 'c'});
  if (wiz.cfb2h) params.set('bl', wiz.cfb2h);
  if (wiz.FdrFJe) params.set('f.sid', wiz.FdrFJe);
  const body = new URLSearchParams({'f.req': JSON.stringify([[[rpcid, JSON.stringify(payload), null, 'generic']]]),
    at: wiz.SNlM0e});
  try {
    const res = await fetch('/_/BardChatUi/data/batchexecute?' + params, {method: 'POST', credentials: 'include',
      redirect: 'manual', signal: AbortSignal.timeout(60000),
      headers: {'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8', 'X-Same-Domain': '1'}, body});
    return {status: res.type === 'opaqueredirect' ? 0 : res.status,
            contentType: res.headers.get('content-type') || '', body: await res.text()};
  } catch (e) {
    return {status: -1, contentType: '', body: String(e)};
  }
}
"""
HAS_TOKEN_JS = "() => !!(window.WIZ_global_data && window.WIZ_global_data.SNlM0e)"
# The account button's label reads "Google Account: Name  (email)". oPEP7c is where Google
# apps keep the signed-in email in WIZ_global_data (a fallback; not verified on Gemini).
ACCOUNT_JS = """
(selector) => {
  const el = document.querySelector(selector);
  const label = el ? (el.getAttribute('aria-label') || '') : '';
  const email = (label.match(/[\\w.+-]+@[\\w-]+(?:\\.[\\w-]+)+/) || [])[0]
    || (window.WIZ_global_data || {}).oPEP7c || null;
  const name = label.replace(/^[^:]*:\\s*/, '').replace(/\\([^)]*\\)\\s*$/, '').trim() || null;
  return {name: name && !name.includes('@') ? name : null, email};
}
"""
# selectors.py values may list alternatives; `first` honors their order (first one that matches wins).
_FIRST = "const first = (root, sel) => { for (const s of sel.split(', ')) { const el = root.querySelector(s); if (el) return el; } return null; };"
# Snapshot response identities as well as count: Angular may replace a node in place.
# Text is extracted from a clone so KaTeX's accessible and visual trees cannot both leak through.
# Generated images count once their img has a src and has settled (loaded, or failed: the saved file comes from
# the transcript, not this preview); a generated-image without one is still rendering.
# Each citation chip becomes a \ue000 (parse.CHIP) in the text; chips lists its {title, aria} in page order.
LAST_RESPONSE_JS = """
([responseSel, contentSel, busySel, stopSel, imageSel, canvasSel, chipSel, chipTitleSel]) => {
  %s
  const visible = el => !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
  const all = [...document.querySelectorAll(responseSel)].filter(visible);
  if (!all.length) return null;
  const ids = window.__gwebResponseIds ||= {map: new WeakMap(), next: 0};
  const key = el => {
    const content = first(el, contentSel);
    if (content?.id) return content.id;
    if (el.id) return el.id;
    if (!ids.map.has(el)) ids.map.set(el, ++ids.next);
    return 'node-' + ids.map.get(el);
  };
  const last = all[all.length - 1];
  const content = first(last, contentSel);
  const users = [...document.querySelectorAll('user-query')].filter(visible);
  let turn = last.parentElement;
  while (turn && !turn.querySelector('user-query')) turn = turn.parentElement;
  const currentTurn = !users.length || (turn?.querySelector('user-query') === users[users.length - 1]);
  let text = '';
  const chips = [];
  if (content) {
    const clone = content.cloneNode(true);
    // Replace the whole KaTeX wrapper, not just MathML: the HTML glyph tree duplicates it.
    for (const math of [...clone.querySelectorAll('.katex, math')]) {
      if (!clone.contains(math)) continue;
      const tex = math.querySelector('annotation[encoding="application/x-tex"]')?.textContent;
      if (tex) math.replaceWith(document.createTextNode(tex));
      else {
        math.querySelectorAll('annotation').forEach(e => e.remove());
        const source = math.querySelector('math') || math.querySelector('.katex-html') || math;
        math.replaceWith(document.createTextNode(source.textContent));
      }
    }
    clone.querySelectorAll('[aria-hidden="true"], [hidden], script, style, annotation').forEach(e => e.remove());
    for (const chip of [...clone.querySelectorAll(chipSel)]) {
      if (!clone.contains(chip)) continue;
      chips.push({title: (chip.querySelector(chipTitleSel)?.textContent || '').trim(),
                  aria: chip.querySelector('[aria-label]')?.getAttribute('aria-label') || ''});
      chip.replaceWith(document.createTextNode('\ue000'));
    }
    // innerText needs a rendered node; a detached clone falls back to concatenated textContent.
    const scratch = document.createElement('div');
    scratch.style.cssText = 'position:fixed;left:-100000px;white-space:normal';
    scratch.append(clone);
    document.body.append(scratch);
    text = clone.innerText;
    scratch.remove();
  }
  const settled = (img) => !!img.getAttribute('src') && img.complete;
  const generated = [...last.querySelectorAll(imageSel)].map((g) => [...g.querySelectorAll('img')].filter(settled).length);
  return {count: all.length, keys: all.map(key), key: key(last), text, chips, currentTurn,
          images: generated.reduce((a, b) => a + b, 0), imagesPending: generated.filter((n) => !n).length,
          canvases: canvasSel ? last.querySelectorAll(canvasSel).length : 0,
          complete: last.matches('[aria-busy="false"]') || !!last.querySelector('[aria-busy="false"]'),
          busy: last.matches(busySel) || !!last.querySelector(busySel)
            || [...document.querySelectorAll(stopSel)].some(visible)};
}
""" % _FIRST
MENU_JS = """
([itemSel, titleSel, descSel]) => {
  %s
  const alt = itemSel.split(', ').find((s) => document.querySelector(s));
  if (!alt) return {selector: null, items: []};
  const items = [...document.querySelectorAll(alt)].map((el) => {
    const title = first(el, titleSel);
    const desc = first(el, descSel);
    const lines = el.innerText.split('\\n').map((s) => s.trim()).filter(Boolean);
    const name = (title ? title.innerText.trim() : lines[0]) || null;
    const cls = el.className && el.className.toString ? el.className.toString() : '';
    return {
      name,
      description: (desc ? desc.innerText.trim() : lines.filter((l) => l !== name).join(' ')) || null,
      current: el.getAttribute('aria-checked') === 'true' || el.getAttribute('aria-selected') === 'true'
        || /(^|\\s)(is-)?selected(\\s|$)/.test(cls),
      disabled: el.getAttribute('aria-disabled') === 'true' || el.hasAttribute('disabled'),
    };
  });
  return {selector: alt, items};
}
""" % _FIRST


# Attachment chips in the composer: how many, how many still uploading, how many failed.
UPLOADS_JS = """
([chipSel, busySel, errorSel]) => {
  const chips = [...document.querySelectorAll(chipSel)];
  return {count: chips.length, busy: chips.filter((c) => c.querySelector(busySel)).length,
          failed: chips.filter((c) => c.querySelector(errorSel)).length};
}
"""
UPLOAD_ERROR = re.compile(r"upload error|(?:couldn't|could not|failed to) upload|upload failed", re.IGNORECASE)


# Text of each visible notice (toast, dialog, banner) outside the transcript and the prompt box.
NOTICES_JS = """
([noticeSel, skipSel]) => [...document.querySelectorAll(noticeSel)]
  .filter((el) => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden'
    && !el.closest(skipSel) && !el.querySelector(skipSel))
  .map((el) => (el.innerText || el.textContent || '').trim()).filter(Boolean)
"""


@asynccontextmanager
async def open_page(*, headless: bool, send: bool = False, sign_in: bool = False,
                    download: bool = False) -> AsyncIterator["Page"]:
    """A gemini.google.com page under the gemini policy: a fresh tab in the identity daemon's Chrome, else a Chrome
    of this command's own (visible only for --headed and sign-in). send=True is only for `ask`; download=True only
    for `ask --image-out`; sign_in=True only for `identity login`."""
    async with browser.site_page(POLICY, mode="headless" if headless else "visible", send=send, sign_in=sign_in,
                                 download=download) as page:
        yield page


def first_limit_notice(texts: list[str] | None) -> str | None:
    return next((text for text in texts or [] if parse.limit_notice(text)), None)


async def _check_limit(page: "Page", *, sent: bool) -> None:
    """RateLimited when the page shows a usage-limit notice. A check that fails counts as no notice."""
    try:
        text = first_limit_notice(await page.evaluate(NOTICES_JS, [sel.NOTICE, sel.NOTICE_SKIP]))
    except Exception:
        return
    if text:
        raise RateLimited(text, sent=sent, resets_at=parse.resets_at(text))


async def _watch_limit(page: "Page", operation, *, sent: bool):
    """Watch notices even while a send, menu, or network response is blocked."""
    task = asyncio.ensure_future(operation)
    try:
        while True:
            await _check_limit(page, sent=sent)
            done, _ = await asyncio.wait({task}, timeout=0.2)
            if done:
                await _check_limit(page, sent=sent)
                return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def signed_out() -> dict:
    return {"signedIn": False, "name": None, "email": None, "api": None}


def _on_app(page: "Page") -> bool:
    return urlsplit(page.url).hostname == APP_HOST


async def open_app(page: "Page", url: str = APP_URL) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    host = urlsplit(page.url).hostname or ""
    if host in ("accounts.google.com", "consent.google.com"):
        raise AuthRequired(f"Gemini sent Chrome to {host}; the Google session is missing or expired.")
    if not _on_app(page):
        raise RuntimeError(f"gemini.google.com redirected to {host}. {SNIFF_HINT}")


async def _signed_in_page(page: "Page", url: str = APP_URL) -> None:
    await open_app(page, url)
    if not await page.evaluate(HAS_TOKEN_JS):
        raise AuthRequired("Not signed in to Gemini.")


async def rpc(page: "Page", name: str, payload: Any, *, item: str | None = None) -> Any:
    """One read RPC (endpoints.RPC[name]) from inside the page. Its decoded payload.

    HTTP 0/401/403 or a permission code -> AuthRequired. For an `item` lookup, Google's
    not-found and permission-denied codes mean NotFound (someone else's or a made-up id).
    """
    rpcid = RPC[name]
    source = urlsplit(page.url).path or "/app"
    res = await page.evaluate(BATCH_JS, {"rpcid": rpcid, "payload": payload, "sourcePath": source})
    status = res["status"]
    if status in (0, 401, 403):
        raise AuthRequired(f"Gemini refused {rpcid} (HTTP {status}); the session is missing or expired.")
    if status == -1:
        raise RuntimeError(f"{rpcid} request failed in the page ({res['body']}). {SNIFF_HINT}")
    if not 200 <= status < 300:
        raise RuntimeError(f"{rpcid} failed: HTTP {status}. {SNIFF_HINT}")
    try:
        return parse.rpc_payload(res["body"], rpcid)
    except parse.RpcError as exc:
        if item and exc.code in parse.NOT_FOUND_CODES | {7}:
            raise NotFound(f"{item} not found.", hint="If every chat fails like this, check `llmcli gemini status`.") from None
        if exc.code in parse.AUTH_CODES:
            raise AuthRequired(f"Gemini refused {rpcid} (code {exc.code}); the session is missing or expired.") from None
        raise RuntimeError(f"{exc} {SNIFF_HINT}") from None
    except ValueError:
        raise RuntimeError(f"{rpcid} answered something that isn't batchexecute JSON. {SNIFF_HINT}") from None


async def _account(page: "Page") -> dict:
    try:
        await page.wait_for_selector(sel.ACCOUNT_BUTTON, state="attached", timeout=10_000)
    except Exception:
        pass  # the label is a nicety; the token already proved the session
    return await page.evaluate(ACCOUNT_JS, sel.ACCOUNT_BUTTON)


async def session(page: "Page") -> dict:
    """{signedIn, name, email, api}. One check shared by login and status.

    signedIn: the page has an `at` token (Google serves none to signed-out visitors).
    api: "ok" when the chat-list RPC answers, else what went wrong.
    """
    out = signed_out()
    if not _on_app(page) or not await page.evaluate(HAS_TOKEN_JS):
        return out
    out.update(signedIn=True, **await _account(page))
    try:
        await rpc(page, "list_chats", [CHATS_FIRST_PAGE, None, [1, None, 1]])
        out["api"] = "ok"
    except AuthRequired:
        return signed_out()
    except (RuntimeError, ValueError) as exc:
        out["api"] = str(exc)
    return out


async def status(*, headed: bool = False) -> dict:
    async with open_page(headless=not headed) as page:
        try:
            await open_app(page)
        except AuthRequired:
            return signed_out()
        return await session(page)


async def login(timeout: int, on_state=lambda _state: None) -> dict:
    """Visible Chrome on Google's sign-in page for Gemini (the guard stays on, in sign-in mode); wait until a tab is
    signed in. on_state hears "signing in" once."""
    on_state("signing in")
    async with open_page(headless=False, sign_in=True) as page:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await asyncio.sleep(2)
            tabs = [p for p in page.context.pages if not p.is_closed()]
            if not tabs:
                raise AuthRequired("The Chrome window closed before sign-in finished.")
            for tab in tabs:  # sign-in may finish in another tab
                try:
                    info = await session(tab)
                except Exception:  # tab is mid-navigation
                    continue
                if info["signedIn"]:
                    return info
        raise AuthRequired(f"Timed out after {timeout}s waiting for Google sign-in.")


async def chats(limit: int, *, headed: bool = False) -> list[dict]:
    """Up to `limit` chats in sidebar order: pinned first, then the rest, most recent first.

    MaZiqc lists one kind per call ([size, cursor, [1, null, 1]] pinned, [0, null, 1] the rest; VERIFIED
    2026-10-17), so the pinned list is paged first and then the rest, each with its own cursor.
    """
    async with open_page(headless=not headed) as page:
        await _signed_in_page(page)
        rows: list[dict] = []
        seen: set[str] = set()
        for pinned in (1, 0):
            payload: list = [CHATS_FIRST_PAGE, None, [pinned, None, 1]]
            for _ in range(MAX_PAGES):
                if len(rows) >= limit:
                    return rows[:limit]
                batch, cursor = parse.chats(await rpc(page, "list_chats", payload))
                fresh = [r for r in batch if r["id"] not in seen]
                seen.update(r["id"] for r in fresh)
                rows += fresh
                if not cursor or not fresh:
                    break
                payload = [CHATS_NEXT_PAGE, cursor, [pinned, None, 1]]
        return rows[:limit]


async def _transcript(page: "Page", chat_id: str, *, max_pages: int = MAX_PAGES) -> list[dict]:
    """Every message, oldest first: newest page first, then older pages via the cursor."""
    messages: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        payload = [parse.rpc_chat_id(chat_id), TURNS_PAGE, cursor, 1, [0], [4], None, 1]
        page_messages, next_cursor = parse.chat_messages(await rpc(page, "read_chat", payload, item=f"Chat {chat_id}"))
        messages = page_messages + messages
        if not next_cursor or next_cursor == cursor or not page_messages:
            break
        cursor = next_cursor
    if not messages:
        raise NotFound(f"Chat {chat_id} not found (or it has no messages).", hint="run: llmcli gemini chats list")
    for message in messages:
        if message["role"] == "user":
            message["text"] = html.unescape(message["text"])  # prompts come back HTML-escaped
    return messages


async def chat(chat_id: str, *, headed: bool = False) -> dict:
    async with open_page(headless=not headed) as page:
        await _signed_in_page(page)
        messages = await _transcript(page, chat_id)
        return {"chatId": chat_id, "url": parse.chat_url(chat_id), "messages": messages}


async def _open_model_menu(page: "Page") -> tuple[str | None, list[dict]]:
    """Open the model menu: (the MODEL_ITEM alternative that matched, its rows).

    Right after load the picker can be on the page before Angular wires up its click handler, so a
    click that opens nothing gets Escape and another click: MENU_CLICKS in all, within MENU_SECONDS.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + MENU_SECONDS

    def ms_left(cap: float = MENU_SECONDS * 1000) -> float:
        return max(1, min(cap, (deadline - loop.time()) * 1000))  # Playwright reads 0 as "no timeout"

    button = page.locator(sel.MODEL_BUTTON).first
    try:
        await button.wait_for(state="visible", timeout=ms_left())
        for click in range(MENU_CLICKS):
            await button.click(timeout=ms_left())
            try:
                await page.wait_for_selector(sel.MODEL_ITEM, state="visible", timeout=ms_left(MENU_OPEN_MS))
                break
            except Exception:
                if click == MENU_CLICKS - 1:
                    raise
                await page.keyboard.press("Escape")
    except Exception:
        raise RuntimeError(f"Couldn't open Gemini's model menu. {SELECTOR_HINT}") from None
    menu = await page.evaluate(MENU_JS, [sel.MODEL_ITEM, sel.MODEL_ITEM_TITLE, sel.MODEL_ITEM_DESCRIPTION])
    return menu["selector"], menu["items"]


async def _current_model(page: "Page") -> str | None:
    """The picker's label ("Flash"), else the tail of its aria-label ("Open mode picker, currently Flash").

    The button's own text is the last resort: it can include icon ligatures ("arrow_drop_down").
    """
    async def text_of(selector: str) -> str:
        try:
            return " ".join((await page.locator(selector).first.inner_text(timeout=5_000)).split())
        except Exception:
            return ""

    if label := await text_of(sel.MODEL_BUTTON_LABEL):
        return label
    try:
        aria = await page.locator(sel.MODEL_BUTTON).first.get_attribute("aria-label", timeout=2_000) or ""
    except Exception:
        aria = ""
    if "currently " in aria:
        return aria.rpartition("currently ")[2].strip() or None
    return await text_of(sel.MODEL_BUTTON) or None


def _model_name(name: str) -> str:
    """Menu and picker labels differ by an optional leading version number."""
    return re.sub(r"^\d+(?:\.\d+)*\s+", "", " ".join(name.casefold().split()))


def _labelled_row(items: list[dict], label: str | None) -> int | None:
    """The one row the picker's label names ("Flash" -> "3.8 Flash", not "3.5 Flash-Lite"), else None."""
    if not label:
        return None
    raw = [" ".join((i["name"] or "").casefold().split()) for i in items]
    exact = [i for i, name in enumerate(raw) if name == " ".join(label.casefold().split())]
    if len(exact) == 1:
        return exact[0]
    names = [_model_name(name) for name in raw]
    key = _model_name(label)
    for test in (lambda n: n == key, lambda n: n.endswith(" " + key), lambda n: key in n, lambda n: n in key):
        hits = [n for n, name in enumerate(names) if name and test(name)]
        if len(hits) == 1:
            return hits[0]
    return None


def _mark_current(items: list[dict], current: str | None) -> list[dict]:
    """When the menu marks no row, the row the picker's label names ("Flash" -> "3.5 Flash")."""
    if not any(i["current"] for i in items) and (row := _labelled_row(items, current)) is not None:
        items[row]["current"] = True
    return items


async def models(*, headed: bool = False) -> list[dict]:
    """The model menu's entries. Opens the menu, reads it, closes it with Escape; picks nothing."""
    async with open_page(headless=not headed) as page:
        await _signed_in_page(page)
        current = await _current_model(page)
        _, items = await _open_model_menu(page)
        await page.keyboard.press("Escape")
        return [{k: i[k] for k in ("name", "description", "current")}
                for i in _mark_current(items, current) if i["name"]]


def match_model(items: list[dict], wanted: str) -> int:
    """Index of the menu item `wanted` names: exact (case-insensitive) first, then a unique substring."""
    names = [" ".join((i["name"] or "").casefold().split()) for i in items]
    key = " ".join(wanted.casefold().split())
    exact = [n for n, name in enumerate(names) if name == key]
    normalized = [_model_name(name) for name in names]
    hits = exact
    if not hits:
        hits = [n for n, name in enumerate(normalized) if name == _model_name(key)]
    if not hits:
        hits = [n for n, name in enumerate(normalized) if _model_name(key) in name]
    if len(hits) == 1:
        return hits[0]
    available = ", ".join(i["name"] for i in items if i["name"]) or "none"
    if hits:
        raise NotFound(f"{wanted!r} matches more than one model: {', '.join(items[h]['name'] for h in hits)}.",
                       hint="run: llmcli gemini models")
    raise NotFound(f"No model matches {wanted!r}. Available: {available}.", hint="run: llmcli gemini models")


async def _pick_model(page: "Page", wanted: str) -> str:
    """Pick `wanted` in the model menu and return the picker's label once it names that row.

    A click that doesn't take leaves the label on the old model ("Flash" after picking "3.5 Flash-Lite"),
    so the menu is opened and the row clicked once more before giving up.
    """
    loop = asyncio.get_running_loop()
    for _ in range(2):
        item_selector, items = await _open_model_menu(page)
        try:
            index = match_model(items, wanted)
        except NotFound:
            await page.keyboard.press("Escape")
            raise
        name = items[index]["name"]
        if items[index]["disabled"]:
            await page.keyboard.press("Escape")
            raise NotFound(f"Model {name!r} isn't available on this account.", hint="run: llmcli gemini models")
        if items[index]["current"]:
            await page.keyboard.press("Escape")
        else:
            await page.locator(item_selector).nth(index).click()
        deadline = loop.time() + PICK_SECONDS
        while True:
            await page.wait_for_timeout(500)
            shown = await _current_model(page)
            if _labelled_row(items, shown) == index:
                return shown
            if loop.time() >= deadline:
                break
    raise RuntimeError(f"Picked {name!r} in Gemini's model menu, but the picker still shows {shown!r}. {SELECTOR_HINT}")


async def _open_thinking_menu(page: "Page") -> tuple[str | None, list[dict]]:
    """Open the model menu, then its Thinking level submenu: (the THINKING_ITEM alternative that matched,
    its rows). NotFound when the model menu has no Thinking level row."""
    await _open_model_menu(page)
    try:
        await page.wait_for_selector(sel.THINKING_ROW, state="visible", timeout=MENU_OPEN_MS)
    except Exception:
        await page.keyboard.press("Escape")
        raise NotFound("Gemini's model menu has no Thinking level for this model.",
                       hint=f"Drop --thinking, or try another --model. {SELECTOR_HINT}") from None
    try:
        await page.locator(sel.THINKING_ROW).first.click(timeout=5_000)
        await page.wait_for_selector(sel.THINKING_ITEM, state="visible", timeout=MENU_OPEN_MS)
    except Exception:
        raise RuntimeError(f"Couldn't open Gemini's Thinking level menu. {SELECTOR_HINT}") from None
    menu = await page.evaluate(MENU_JS, [sel.THINKING_ITEM, sel.MODEL_ITEM_TITLE, sel.MODEL_ITEM_DESCRIPTION])
    return menu["selector"], menu["items"]


async def _pick_thinking(page: "Page", level: str) -> str:
    """Pick `level` (one of THINKING_LEVELS) in the Thinking level submenu; return it once the reopened
    submenu marks that row selected. A click that doesn't take gets one more before giving up."""
    for attempt in range(3):  # read, click, check, click, check
        item_selector, items = await _open_thinking_menu(page)
        names = [(i["name"] or "").lower() for i in items]
        if level not in names:
            await page.keyboard.press("Escape")
            available = ", ".join(i["name"] for i in items if i["name"]) or "none"
            raise NotFound(f"Gemini's Thinking level menu has no {level!r}. Available: {available}.",
                           hint=SELECTOR_HINT)
        index = names.index(level)
        if items[index]["disabled"]:
            await page.keyboard.press("Escape")
            raise NotFound(f"Thinking level {items[index]['name']!r} isn't available for this model.",
                           hint="Try another --thinking or --model.")
        if items[index]["current"]:
            await page.keyboard.press("Escape")
            return level
        if attempt < 2:
            await page.locator(item_selector).nth(index).click()  # closes both menus
    await page.keyboard.press("Escape")
    shown = next((i["name"] for i in items if i["current"]), None)
    raise RuntimeError(f"Picked thinking level {level!r}, but Gemini's menu still shows {shown!r}. {SELECTOR_HINT}")


async def _attach(page: "Page", files: list[str]) -> None:
    """Hand the files to Gemini's own upload control, the way a person's file picker would."""
    try:
        await page.locator(sel.UPLOAD_MENU_BUTTON).first.click(timeout=15_000)
        async with page.expect_file_chooser(timeout=10_000) as chooser:
            await page.locator(sel.UPLOAD_FILES_ITEM).first.click(timeout=10_000)
        await (await chooser.value).set_files(files)
        return
    except Exception:
        pass
    try:  # some builds keep a file input in the page instead of opening a chooser
        await page.locator(sel.FILE_INPUT).first.set_input_files(files, timeout=5_000)
    except Exception:
        raise RuntimeError(f"Couldn't attach {len(files)} file(s) with Gemini's upload menu. {SELECTOR_HINT}") from None


TOOLS = {"Create image": (sel.CREATE_IMAGE_ITEM, sel.IMAGE_TOOL_CHIP), "Canvas": (sel.CANVAS_ITEM, sel.CANVAS_TOOL_CHIP)}


async def _tool(page: "Page", name: str) -> None:
    """Turn on one of the composer's TOOLS, as a person would in the Upload & tools menu, so the prompt uses it
    whatever its wording. Left alone when its chip already shows it on (a click would turn it off)."""
    item, chip = TOOLS[name]
    if await page.locator(chip).count():
        return
    try:
        await page.locator(sel.UPLOAD_MENU_BUTTON).first.click(timeout=15_000)
        await page.locator(item).first.click(timeout=10_000)
        await page.locator(chip).first.wait_for(state="visible", timeout=10_000)
    except Exception:
        raise RuntimeError(f"Couldn't turn on Gemini's {name} tool. {SELECTOR_HINT}") from None


async def _uploaded(page: "Page", count: int) -> None:
    """Wait until Gemini shows `count` attachments with none still uploading. RuntimeError (nothing sent yet) when
    one failed or they're still uploading after UPLOAD_SECONDS: the send button is enabled meanwhile, and a prompt
    sent then would go without its files."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + UPLOAD_SECONDS
    while True:
        state = await page.evaluate(UPLOADS_JS, [sel.ATTACHMENT, sel.ATTACHMENT_BUSY, sel.ATTACHMENT_ERROR])
        notice = next((t for t in await page.evaluate(NOTICES_JS, [sel.NOTICE, sel.NOTICE_SKIP])
                       if UPLOAD_ERROR.search(t)), None)
        if state["failed"] or notice:
            detail = " ".join((notice or f"{state['failed']} of {state['count']} failed").split())
            raise RuntimeError(f"Gemini couldn't upload the --file attachments ({detail}). The prompt was not sent.")
        if state["count"] >= count and not state["busy"]:
            return
        if loop.time() >= deadline:
            raise RuntimeError(f"Gemini was still uploading the --file attachments after {UPLOAD_SECONDS}s "
                               f"({state['count'] - state['busy']} of {count} done). The prompt was not sent.")
        await asyncio.sleep(0.5)


async def _type_prompt(page: "Page", prompt: str) -> "Locator":
    """Type the prompt like a person (Quill ignores programmatic fills). Returns the prompt box."""
    editor = page.locator(sel.PROMPT_INPUT).first
    try:
        await editor.wait_for(state="visible", timeout=30_000)
    except Exception:
        raise RuntimeError(f"Gemini's prompt box never appeared. {SELECTOR_HINT}") from None
    await editor.click()
    for n, line in enumerate(prompt.split("\n")):
        if n:
            await page.keyboard.press("Shift+Enter")  # Enter alone would send
        if line:
            await page.keyboard.insert_text(line)
    return editor


async def _send_button_ready(page: "Page", *, uploading: bool) -> bool:
    """Wait for the send button to enable: it doesn't until the text registers, nor while a file uploads."""
    try:
        await page.wait_for_function(
            "(s) => { const b = document.querySelector(s); return b && !b.disabled"
            " && b.getAttribute('aria-disabled') !== 'true'; }",
            arg=sel.SEND_BUTTON, timeout=180_000 if uploading else 15_000)
        return True
    except Exception:
        return False


LAST_RESPONSE_SELECTORS = [sel.MODEL_RESPONSE, sel.RESPONSE_CONTENT, sel.RESPONSE_BUSY, sel.STOP_BUTTON,
                           sel.GENERATED_IMAGE, sel.CANVAS_CHIP, sel.SOURCE_CHIP, sel.SOURCE_CHIP_TITLE]


async def _last_response(page: "Page") -> dict | None:
    """Identity, completion state and extracted text for the page's last model-response, or None when there is none."""
    return await page.evaluate(LAST_RESPONSE_JS, LAST_RESPONSE_SELECTORS)


async def _settled_last_response(page: "Page") -> dict | None:
    """_last_response once the number of responses stops changing: a chat's history renders after load."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10
    count, since, state = -1, loop.time(), None
    while loop.time() < deadline:
        state = await _last_response(page)
        now = tuple(state["keys"]) if state and "keys" in state else (state or {}).get("count", 0)
        if now != count:
            count, since = now, loop.time()
        elif loop.time() - since >= SETTLE_SECONDS:
            break
        await asyncio.sleep(0.25)
    return state


def _fresh_response(state: dict | None, before: dict | None) -> bool:
    if not state or state.get("currentTurn") is False:
        return False
    if not before:
        return True
    if "keys" in before and "key" in state:
        return state["key"] not in before["keys"]
    return state["count"] > before["count"]


async def _dom_answer(page: "Page", before: dict | None, deadline: float) -> dict | None:
    """{text, chips, images, canvases, key} of the latest new response once it holds still with text or images in it.
    Only the latest new response can finish; placeholders, streaming, and rendering images reset the quiet period."""
    loop = asyncio.get_running_loop()
    last, stable_since = None, loop.time()
    while loop.time() < deadline:
        await _check_limit(page, sent=True)
        state = await _last_response(page)
        fresh = _fresh_response(state, before)
        text = (state["text"] or "").strip() if fresh else ""
        shown = state.get("images", 0) if fresh else 0
        # A usage-limit notice where the answer belongs may never be marked complete: it only has to hold still.
        busy = fresh and (state["busy"] or not state.get("complete", True) or state.get("imagesPending", 0)) \
            and not parse.limit_reply(text)
        candidate = (state.get("key"), text, shown) if fresh else None
        if busy or candidate != last or not (text or shown):
            last, stable_since = None if busy else candidate, loop.time()
        elif loop.time() - stable_since >= STABLE_SECONDS:
            return {"text": text, "chips": state.get("chips", []), "images": shown,
                    "canvases": state.get("canvases", 0), "key": state.get("key")}
        await asyncio.sleep(0.5)
    return None


async def _reply_turn(page: "Page", chat_id: str, response_id: str | None, ready) -> dict | None:
    """parse.reply_turn for the new response once ready(turn) holds: the transcript can lag the page, and a new
    chat's first read can fail, so failed reads are retried too. After TURN_SECONDS, the last turn read (None if the
    transcript never had it), or the last read's error."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TURN_SECONDS
    payload = [parse.rpc_chat_id(chat_id), TURNS_PAGE, None, 1, [0], [4], None, 1]
    while True:
        try:
            turn = parse.reply_turn(await rpc(page, "read_chat", payload, item=f"Chat {chat_id}"), response_id)
        except (NotFound, RuntimeError):  # a signal arrives as CancelledError, which isn't caught here
            if loop.time() >= deadline:
                raise
            turn = None
        if (turn and ready(turn)) or loop.time() >= deadline:
            return turn
        await asyncio.sleep(1)


def _response_id(key: str | None) -> str | None:
    """The r_ id at the end of a response's content id (model-response-message-contentr_<hex>)."""
    return (re.search(r"r_[0-9a-f]+$", key or "") or [None])[0]


def _fife(url: str, options: str) -> str:
    """An image-server URL with its size/format options ("=s1024-rj") and query replaced by `options`."""
    return re.sub(r"=[a-z][\w-]*$", "", url.split("?", 1)[0]) + options


async def _get_image(page: "Page", start: str) -> bytes:
    """GET an image with the profile's cookies (page.request), following redirects and the text/plain "next
    URL" answers Gemini's image server gives. page.request skips the route guard, so every hop is checked
    with guard.image_url_allowed. The last hop answers 403 now and then (seen 2026-10-17), so a 403, 429,
    or 5xx restarts the chain, IMAGE_TRIES times in all. Errors name the host and path shape only: these URLs are signed."""
    url, hops, tries = start, 0, 1
    while hops < IMAGE_HOPS:
        hops += 1
        where = describe("GET", url)
        if not image_url_allowed(url):
            raise RuntimeError(f"Refused to fetch an image from {where}: not one of Gemini's image hosts. {SNIFF_HINT}")
        try:
            res = await page.request.get(url, max_redirects=0, timeout=60_000)
            data = await res.body()
        except Exception:
            raise RuntimeError(f"Fetching the image failed ({where}).") from None
        if res.status in (301, 302, 303, 307, 308):
            url = urljoin(url, res.headers.get("location", ""))
        elif res.status in (403, 429) or res.status >= 500:
            if tries >= IMAGE_TRIES:
                raise RuntimeError(f"Gemini's image server answered HTTP {res.status} ({where}).")
            await asyncio.sleep(tries)
            url, hops, tries = start, 0, tries + 1
        elif not 200 <= res.status < 300:
            raise RuntimeError(f"Gemini's image server answered HTTP {res.status} ({where}).")
        elif res.headers.get("content-type", "").startswith("text/plain"):
            url = data.decode("utf-8", "replace").strip()
        elif images.sniff(data)[0]:
            return data
        else:
            raise RuntimeError(f"Gemini's image server answered with something that isn't an image ({where}). {SNIFF_HINT}")
    raise RuntimeError(f"Gemini's image server redirected more than {IMAGE_HOPS} times. {SNIFF_HINT}")


async def _download_images(page: "Page", chat_id: str, response_id: str | None, shown: int, fmt: str) -> list[dict]:
    """[{data, source_url_kind}] for each image of the new response, in order: "full_size" from Gemini's own
    full-size link (c8o8Fe), else "preview" from the transcript's (smaller) image URL."""
    turn = await _reply_turn(page, chat_id, response_id, lambda t: len(t["images"]) >= shown)
    listed = len(turn["images"]) if turn else 0
    if listed < shown:
        raise RuntimeError(f"Gemini showed {shown} image(s), but chat {chat_id}'s transcript lists {listed}. "
                           f"{SENT_HINT} {SNIFF_HINT}")
    options = f"=d-I{images.FIFE_FORMAT[fmt]}?alr=yes"
    out = []
    for image in turn["images"]:
        try:
            link = parse.at(await rpc(page, "full_size_image", parse.full_size_payload(turn, image["imageId"])), 0)
            if not isinstance(link, str):
                raise RuntimeError("c8o8Fe answered without a link")
            out.append({"data": await _get_image(page, _fife(link, options)), "source_url_kind": "full_size"})
        except (RuntimeError, ValueError):
            out.append({"data": await _get_image(page, _fife(image["url"], options)), "source_url_kind": "preview"})
    return out


def _prompt_request(request: "Request") -> str | None:
    """What a request that carries a prompt is: "StreamGenerate", "processSession" (a WebChannel
    POST other than its TYPE=terminate), or None."""
    parts = urlsplit(request.url)
    if parts.path == GENERATE_PATH:
        return "StreamGenerate"
    if (request.method == "POST" and (parts.hostname, parts.path) == (CHANNEL_HOST, CHANNEL_PATH)
            and dict(parse_qsl(parts.query)).get("TYPE") != "terminate"):
        return "processSession"
    return None


async def _sent(page: "Page", before: dict | None, url_before: str, outbound: list[str], deadline: float) -> bool:
    """Whether the prompt left after the click, by `deadline`.

    Any of these shows it did: a request that carries a prompt (`outbound`, filled from the click
    on), the URL moving to a new /app/<id>, or more model-responses on the page than `before`.
    """
    loop = asyncio.get_running_loop()
    chat_before = parse.url_chat_id(url_before)
    while True:
        if outbound or parse.url_chat_id(page.url) not in (None, chat_before):
            return True
        await _check_limit(page, sent=True)
        state = await _last_response(page)
        if _fresh_response(state, before):
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.25)


async def _stream_end(response: "Response", failed: asyncio.Future, seconds: float) -> None:
    """Wait for the StreamGenerate body to finish. Playwright's finished() never resolves for a request
    that fails mid-stream, so the requestfailed event (`failed`) is raced against it."""
    finished = asyncio.ensure_future(response.finished())
    try:
        done, _ = await asyncio.wait({finished, failed}, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        finished.cancel()
    if failed in done:
        raise RuntimeError(f"Gemini's answer stream broke ({failed.result()}). {SENT_HINT}")
    if not done:
        raise RuntimeError(f"Gemini didn't finish answering in time (raise --timeout). {SENT_HINT}")


NO_STREAM = {"chatId": None, "answer": None, "refs": [], "images": 0, "done": False, "error": None}


async def _streamed(streamed: asyncio.Future, failed: asyncio.Future, send_deadline: float, deadline: float) -> dict:
    """parse.generate of the StreamGenerate answer once its body has finished; NO_STREAM if unreadable."""
    loop = asyncio.get_running_loop()
    done, _ = await asyncio.wait({streamed, failed}, timeout=max(0.0, send_deadline - loop.time()),
                                 return_when=asyncio.FIRST_COMPLETED)
    if streamed not in done:
        if failed in done:
            raise RuntimeError(f"Gemini's answer stream broke ({failed.result()}). {SENT_HINT}")
        raise RuntimeError(f"Gemini didn't start answering within {SEND_SECONDS:g}s. {SENT_HINT}")
    response = streamed.result()
    await _stream_end(response, failed, max(1.0, deadline - loop.time()))
    if not 200 <= response.status < 300:
        raise RuntimeError(f"Gemini answered the prompt with HTTP {response.status}. {SENT_HINT}")
    try:
        result = parse.generate(await response.text())
    except Exception:  # body unreadable: fall back to the page
        return dict(NO_STREAM)
    if result["error"]:
        reason = GOOGLE_ERRORS.get(result["error"], "unknown error")
        message = f"Gemini refused the prompt: error {result['error']} ({reason})."
        raise RateLimited(message, sent=True) if result["error"] == RATE_LIMIT_CODE else RuntimeError(message)
    if result["answer"] and not result["done"] and not parse.limit_reply(result["answer"]):
        raise RuntimeError(f"Gemini's answer ended without its completion marker, so it may be cut off. "
                           f"{SENT_HINT} {SNIFF_HINT}")
    return result


async def _ask_impl(prompt: str, *, model: str | None = None, thinking: str | None = None, chat_id: str | None = None,
              files: list[str] | None = None, timeout: float = 150, headed: bool = False,
              image_format: str | None = None, canvas: bool = False, footnotes: bool = True,
              progress: dict | None = None) -> dict:
    """Send one prompt through Gemini's UI and return
    {chatId, chatUrl, model, thinking, prompt, answer, citations, imageCount, canvas[, imageData]}.

    A cited answer has [^n] marks and a footnote block (parse.footnote); footnotes=False leaves both out. citations
    lists the sources either way ([] when none).

    `thinking` (one of THINKING_LEVELS) is picked after the model; None leaves Gemini's level alone and
    reports null. `image_format` (an images.FIFE_FORMAT key) turns on the Create image tool and downloads the
    reply's images in that format as imageData [{data, source_url_kind}]. `canvas` turns on the Canvas tool and
    reads the reply's Canvas doc from the transcript: canvas is {title, markdown}, or None (also without `canvas`),
    and the answer becomes the transcript's text for the turn, without the doc's inline chip. Everything that can
    fail without sending (sign-in, --chat, --model, --thinking, the tool, --file, the prompt box) is checked first.
    The prompt is submitted exactly once: by the send button, or by Enter when the button can't be found; never both.

    The prompt counts as sent when, within SEND_SECONDS of the click, a StreamGenerate or
    processSession request goes out, the URL moves to a new /app/<id>, or a new model-response
    appears. A StreamGenerate answer is parsed off the network; otherwise the answer is the new
    model-response's text once it stops changing, and the chat id comes from the /app/<id> URL.
    """
    progress = progress if progress is not None else {}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    failed: asyncio.Future = loop.create_future()
    streamed: asyncio.Future = loop.create_future()
    outbound: list[str] = []  # requests carrying the prompt, from the click on
    prompt_requests: set = set()

    def on_request(request: "Request") -> None:
        if kind := _prompt_request(request):
            outbound.append(kind)
            prompt_requests.add(request)

    def on_response(response: "Response") -> None:
        if (response.request in prompt_requests and urlsplit(response.url).path == GENERATE_PATH
                and response.request.method == "POST" and not streamed.done()):
            streamed.set_result(response)

    def on_failed(request: "Request") -> None:
        if request in prompt_requests and urlsplit(request.url).path == GENERATE_PATH and not failed.done():
            failed.set_result(request.failure or "request failed")

    async with open_page(headless=not headed, send=True, download=image_format is not None) as page:
        progress["stage"] = "loading Gemini"
        await _signed_in_page(page, parse.chat_url(chat_id) if chat_id else APP_URL)
        await _check_limit(page, sent=False)
        try:
            if chat_id:
                await _transcript(page, chat_id, max_pages=1)  # NotFound before anything is sent
            progress["stage"] = "selecting the model"
            chosen = await _watch_limit(page, _pick_model(page, model), sent=False) if model else await _current_model(page)
            progress["stage"] = "selecting the thinking level"
            level = await _watch_limit(page, _pick_thinking(page, thinking), sent=False) if thinking else None
            for name, wanted in (("Create image", image_format), ("Canvas", canvas)):
                if wanted:
                    progress["stage"] = f"turning on the {name} tool"
                    await _watch_limit(page, _tool(page, name), sent=False)
            progress["stage"] = "preparing the prompt and attachments"
            if files:
                await _watch_limit(page, _attach(page, files), sent=False)
                progress["stage"] = "uploading the attachments"
                await _watch_limit(page, _uploaded(page, len(files)), sent=False)
                progress["stage"] = "preparing the prompt"
            editor = await _watch_limit(page, _type_prompt(page, prompt), sent=False)
            use_button = await _watch_limit(page, _send_button_ready(page, uploading=bool(files)), sent=False)  # before the send timer starts
            before = await _watch_limit(page, _settled_last_response(page), sent=False)  # snapshot immediately before submission, after history loads
        except AuthRequired:
            raise
        except RuntimeError:  # a usage-limit dialog can block the menus and the prompt box
            await _check_limit(page, sent=False)
            raise
        await _check_limit(page, sent=False)  # final check immediately before submission
        progress["stage"] = "sending the prompt"
        send_deadline, url_before = min(deadline, loop.time() + SEND_SECONDS), page.url
        for event, handler in (("request", on_request), ("response", on_response), ("requestfailed", on_failed)):
            page.context.on(event, handler)  # only now: nothing from before the click can count
        progress["submitted"] = True  # submission may reach Gemini even if the click times out
        if use_button:
            await _watch_limit(page, page.locator(sel.SEND_BUTTON).first.click(timeout=5_000), sent=True)
        else:
            await _watch_limit(page, editor.press("Enter"), sent=True)
        if not await _sent(page, before, url_before, outbound, send_deadline):
            seen = list(dict.fromkeys(guard.state.blocked))
            detail = f" The guard blocked: {'; '.join(seen)}." if seen else ""
            raise RuntimeError(f"Gemini never sent the prompt (no new response within {SEND_SECONDS:g}s).{detail} "
                               f"{SELECTOR_HINT}")
        progress["stage"] = "waiting for the final answer"
        result = await _watch_limit(page, _streamed(streamed, failed, send_deadline, deadline), sent=True) if "StreamGenerate" in outbound \
            else dict(NO_STREAM)
        await _check_limit(page, sent=True)
        # A streamed answer is complete on its own, unless it made images or images are wanted: those are
        # counted on the page once they render, --image-out or not.
        reply = {"text": result["answer"], "chips": [], "images": 0, "canvases": 0, "key": None} \
            if result["answer"] and not (image_format or canvas or result["images"]) \
            else await _dom_answer(page, before, deadline)
        if reply and result["answer"]:
            reply["text"] = result["answer"]  # the stream's markdown beats the page's rendered text
        if reply:
            reply["images"] = max(reply["images"], result["images"])  # in case the page settled before its images
        if not reply:
            raise RuntimeError(f"Gemini's answer was empty, unreadable, or still rendering at the deadline. "
                               f"{SENT_HINT} {SELECTOR_HINT}")
        # The stream's markdown and its cite refs, else the page's text with what its chips say.
        text, refs = (result["answer"], result["refs"]) if result["answer"] \
            else parse.chip_refs(reply["text"], reply["chips"])
        chat = result["chatId"] or chat_id or parse.url_chat_id(page.url)
        out = {"chatId": chat, "chatUrl": parse.chat_url(chat) if chat else None, "model": chosen, "thinking": level,
               "prompt": prompt, "answer": text, "citations": [], "imageCount": reply["images"], "canvas": None}
        if canvas:
            if not chat:
                raise RuntimeError(f"Gemini answered, but the page shows no chat id to read the Canvas doc from. "
                                   f"{SENT_HINT}")
            progress["stage"] = "reading the Canvas doc"
            # No doc chip on the page: one read, in case the chip selector is stale. Else wait for the doc.
            turn = await _reply_turn(page, chat, _response_id(reply["key"]),
                                     lambda t: t["canvas"] or not reply["canvases"])
            if turn:
                out["canvas"] = turn["canvas"]
                if turn["answer"]:
                    text, refs = turn["answer"], turn["refs"]
        elif reply["chips"] and chat:
            progress["stage"] = "reading the answer's sources"
            try:
                turn = await _reply_turn(page, chat, _response_id(reply["key"]), lambda t: t["refs"])
            except Exception:  # Gemini answered: keep what the chips say rather than lose the answer
                turn = None
            if turn and turn["refs"]:
                text, refs = turn["answer"], turn["refs"]
        out["answer"], out["citations"] = parse.footnote(text, refs, footnotes=footnotes)
        if image_format and reply["images"]:
            if not chat:
                raise RuntimeError(f"Gemini made {reply['images']} image(s), but the page shows no chat id to "
                                   f"download them from. {SENT_HINT}")
            progress["stage"] = "downloading the images"
            out["imageData"] = await _download_images(page, chat, _response_id(reply["key"]), reply["images"],
                                                      image_format)
            out["imageCount"] = len(out["imageData"])
        elif image_format:
            out["imageData"] = []
        return out


async def ask(prompt: str, *, model: str | None = None, thinking: str | None = None,
              chat_id: str | None = None, files: list[str] | None = None,
              timeout: float = 150, headed: bool = False, image_format: str | None = None,
              canvas: bool = False, footnotes: bool = True) -> dict:
    """Bound the entire operation, including lock, navigation, menus, uploads, send and answer."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("--timeout must be a finite positive number of seconds.")
    progress = {"stage": "opening Chrome and waiting for the profile lock", "submitted": False}
    try:
        return await asyncio.wait_for(_ask_impl(prompt, model=model, thinking=thinking, chat_id=chat_id,
            files=files, timeout=timeout, headed=headed, image_format=image_format, canvas=canvas,
            footnotes=footnotes, progress=progress),
            timeout=timeout)
    except asyncio.TimeoutError:
        hint = SENT_HINT if progress["submitted"] else "The prompt was not submitted."
        raise RuntimeError(f"Timed out after {timeout:g}s while {progress['stage']} (raise --timeout). {hint}") from None
