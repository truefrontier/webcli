"""Browser-side work for webcli messages (gmsg's browser.ts on the shared core): login, status, the conversation list,
one conversation's messages, and the one write, send.

Google Messages for web shows what the phone sends it, end-to-end encrypted on the way, so there is no answer to read
off the wire: every verb reads the rendered app (UI) with gmsg's selectors (endpoints.py). Every command but `login`
runs behind guard.POLICY, which passes the app's listing calls and aborts every phone action that changes anything,
MESSAGE_READ included, so reading a conversation never marks it read and nothing types.

Google Messages keeps one active web session. Every page here holds session.hold() (one Messages session at a
time across processes and identities) for as long as it has the app loaded. When the app says Messages is open in
another window or tab (it offers "Use here"), the command stops with ProfileBusy ("messages_open_elsewhere", exit 5)
and never clicks "Use here": taking the session over would cut off whoever has it.

QUERY lookups go past the 25 conversations the app lists first (_find): Messages' own Start chat picker (its contacts
and its search of group conversations by member, both reads) and paced, capped "Load more" clicks on the list.

`send` without confirm only reads: it resolves the conversation (or, with new_ok, the recipients) and stops. With
confirm (and only then) its page opens with send on and send_policy(scope); see send() for the checks made before
the guard's slots open.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Callable, NoReturn
from urllib.parse import urlsplit

from webcli.core import browser, guard
from webcli.core.errors import AuthRequired, NotFound, ProfileBusy, UsageError
from webcli.sites import google
from webcli.sites.google import blocked
from webcli.sites.messages import parse, session
from webcli.sites.messages.endpoints import (
    APP_HOST,
    APP_URL,
    BUTTON_SECONDS,
    CLOSE_SECONDS,
    CONFIRM_SECONDS,
    CONTACT_PAGES,
    CREATE_SECONDS,
    DEEP_PAGES,
    DETAILS_MAX,
    DETAILS_SECONDS,
    EMPTY_POLLS,
    GROUP_BUTTON_SECONDS,
    IMAGE_SECONDS,
    INPUTS,
    LIST_PAGE,
    LOAD_SECONDS,
    LOGIN_URL,
    MAX_PAGES,
    MESSAGES_SECONDS,
    MORE_SECONDS,
    ORIGIN,
    PACE_MS,
    PICKER_SECONDS,
    POLL_MS,
    SELECTORS,
    SEND_BUTTON,
    SEND_SECONDS,
    SETTLE_MS,
    THREAD_SECONDS,
)
from webcli.sites.messages.guard import POLICY, SendScope, send_policy

if TYPE_CHECKING:
    from typing import AsyncIterator

    from playwright.async_api import Locator, Page

SNIFF_HINT = ("Google Messages may have changed: run `python -m webcli.core.capture messages` and fix "
              "webcli/sites/messages/endpoints.py.")
NOT_PAIRED = "Google Messages isn't paired with your phone here (it shows its QR code)."
LIST_HINT = ("See `webcli messages search QUERY` (it looks past the newest conversations), or raise --pages; "
             "a conversation id opens only once it is listed.")
ELSEWHERE = ("Google Messages is open in another window or tab (it offers \"Use here\"). webcli didn't click "
             "\"Use here\": that would cut off the other session. Close Messages there (or let that command finish), "
             "then retry.")
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
          elsewhere: [...document.querySelectorAll(s.elsewhereBoxes)].some(
            box => s.elsewhereText.some(p => new RegExp(p, 'i').test(box.innerText || ''))),
          messages: n(s.message), thread: n(s.thread) > 0, compose: n(s.compose) > 0};
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
  const at = link => { const m = link && (link.getAttribute('href') || '').split(/[?#]/)[0]
    .match(/(?:^|\\/)conversations\\/([^/]+)$/); return m && m[1] === id; };
  for (const conv of document.querySelectorAll(s.item)) {
    const link = s.link.map(sel => conv.querySelector(sel)).find(Boolean);
    if (at(link)) { link.click(); return true; }
  }
  const group = [...document.querySelectorAll(s.groupItem)].find(at);  // the picker's group results
  if (group) { group.click(); return true; }
  return false;
}"""
# Click the first rendered element `sel` matches (a JS click: Playwright's refuses buttons outside the viewport).
CLICK_JS = """/* messages:click */ (sel) => {
  const el = [...document.querySelectorAll(sel)].find(e => e.getClientRects().length && !e.disabled);
  if (!el) return false;
  el.click();
  return true;
}"""
PICKER_JS = """/* messages:picker */ (s) => {
  const text = el => el ? (el.textContent || '').trim() : '';
  const input = document.querySelector(s.contactInput);
  const chip = c => { const b = c.querySelector('button[role="option"]') || c;
    const m = (b.getAttribute('aria-label') || '').match(/^Contact:\\s*(.*?)\\.\\s*Press backspace/i);
    return m ? m[1].trim() : text(b); };
  const send = document.querySelector(s.sendTo);
  return {open: !!input, input: input ? input.value : null,
          contacts: [...document.querySelectorAll(s.contactRow)].map(row => ({
            name: text(row.querySelector(s.contactName)), number: text(row.querySelector(s.contactNumber)),
            continued: !row.querySelector(s.contactAvatar) && !text(row.querySelector(s.contactName))})),
          sendTo: send ? (text(send.querySelector(s.sendToNumber)) || text(send)) : null,
          groups: [...document.querySelectorAll(s.groupItem)].map(a => ({
            href: a.getAttribute('href'), name: text(a.querySelector(s.groupName))})),
          loading: !!document.querySelector(s.contactsLoading) || !!document.querySelector(s.groupsLoading),
          chips: [...document.querySelectorAll(s.chip)].map(chip), next: !!document.querySelector(s.next)};
}"""
# Click the picker's contact row i (counting every row), or its "Send to" button.
PICK_JS = """/* messages:pick */ ([s, kind, i]) => {
  const el = kind === 'sendTo' ? document.querySelector(s.sendTo)
    : (document.querySelectorAll(s.contactRow)[i] || null);
  const target = el && kind !== 'sendTo' ? el.querySelector(s.contactClick) || el : el;
  if (!target) return false;
  target.click();
  return true;
}"""
# A bubble's text: its <br> line breaks as "\n" (textContent drops them, LIVE 2026-10-23: so a sent text with a line
# break never matched), without a link preview's title and description.
MESSAGES_JS = """/* messages:messages */ (s) => [...document.querySelectorAll(s.message)].map(msg => {
  const pick = sels => { for (const sel of sels) { const el = msg.querySelector(sel); if (el) return el; } return null; };
  const textOf = el => { const copy = el.cloneNode(true);
    copy.querySelectorAll(s.linkPreview).forEach(e => e.remove());
    copy.querySelectorAll('br').forEach(br => br.replaceWith('\\n'));
    return (copy.textContent || '').trim(); };
  const core = msg.querySelector(s.core) || msg;
  const out = msg.getAttribute('is-outgoing') || core.getAttribute('data-e2e-message-outgoing');
  const text = pick(s.text), part = msg.querySelector(s.aria), stamp = pick(s.stamp);
  return {sent: out === 'true' || msg.classList.contains('outgoing') || core.classList.contains('outgoing')
                || msg.classList.contains('sent') || !!msg.querySelector(s.outgoing),
          text: text ? textOf(text) : '', image: !!msg.querySelector(s.image),
          images: msg.querySelectorAll(s.image).length,
          aria: part ? part.getAttribute('aria-label') || '' : '', time: stamp ? (stamp.textContent || '').trim() : ''};
})"""
# An image part's thumbnail <img>; open-image clicks it, as a click would, opening the lightbox.
IMAGE_PART_JS = """(s, i, k) => { const msg = document.querySelectorAll(s.message)[i];
  return msg ? msg.querySelectorAll(s.image)[k] || null : null; }"""
OPEN_IMAGE_JS = f"""/* messages:open-image */ ([s, i, k]) => {{
  const part = ({IMAGE_PART_JS})(s, i, k), el = part && (part.querySelector(s.imageOpen) || part);
  if (!el) return false;
  el.click();
  return true;
}}"""
PREVIEW_JS = f"""/* messages:preview */ ([s, i, k]) => {{
  const part = ({IMAGE_PART_JS})(s, i, k), img = part && part.querySelector('img');
  return img && img.complete && img.naturalWidth ? img.currentSrc || img.src : null;
}}"""
LIGHTBOX_JS = """/* messages:lightbox */ (s) => {
  const box = document.querySelector(s.lightbox), img = box && box.querySelector(s.lightboxImage);
  return box ? {src: img && img.complete && img.naturalWidth ? img.currentSrc || img.src : null} : null;
}"""
BLOB_JS = """/* messages:blob */ async (src) => {
  const blob = await (await fetch(src)).blob();
  return await new Promise((ok, fail) => {
    const reader = new FileReader();
    reader.onload = () => ok(reader.result);
    reader.onerror = () => fail(reader.error);
    reader.readAsDataURL(blob);
  });
}"""
BLOB_PREFIX = f"blob:{ORIGIN}/"  # an image the app decrypted into the page: reading it makes no request
HEADER_JS = """/* messages:header */ (sels) => {
  for (const sel of sels) { const el = document.querySelector(sel); if (el) return (el.textContent || '').trim(); }
  return null;
}"""
# The open conversation's Details dialog: its people (null while it isn't open), and whether a dialog or menu is open.
DETAILS_JS = """/* messages:details */ (s) => {
  const text = el => el ? (el.textContent || '').trim() : '';
  if (!document.querySelector(s.details)) return null;
  return [...document.querySelectorAll(s.participant)].map(li => ({
    name: text(li.querySelector(s.participantName)), number: text(li.querySelector(s.participantNumber))}));
}"""
OVERLAY_JS = """/* messages:overlay */ (s) => !!document.querySelector(s.details)
  || !!document.querySelector('.cdk-overlay-container .mat-mdc-menu-panel')"""
READ_BACK_JS = "el => (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') ? el.value : el.innerText"


def _polls(seconds: float) -> int:
    return max(1, int(seconds * 1000 / POLL_MS))


async def _snapshot(page: "Page") -> dict:
    return await page.evaluate(STATE_JS, SELECTORS)


async def _state(page: "Page", snap: dict) -> str:
    """parse.state(snap). AuthRequired when the app shows pairing; ProfileBusy ("messages_open_elsewhere", exit 5)
    when Messages is open in another window or tab: "Use here" is never clicked."""
    kind = parse.state(snap)
    if kind == "unpaired":
        raise AuthRequired(NOT_PAIRED)
    if kind == "use_here":
        raise ProfileBusy(ELSEWHERE, error="messages_open_elsewhere")
    return kind


@asynccontextmanager
async def _page(policy, **options) -> "AsyncIterator[Page]":
    """browser.site_page(policy) holding the Messages session lock: taken before the page opens the app, released
    after it has left it (about:blank), so two commands never have Messages open at once."""
    async with session.hold():
        async with browser.site_page(policy, **options) as page:
            try:
                yield page
            finally:
                with contextlib.suppress(Exception):
                    await page.goto("about:blank", timeout=5_000)


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


def _pages(pages: int | None, query: str = "", people: list[dict] = ()) -> int:
    """How many "Load more" pages a lookup may load: --pages when given; else DEEP_PAGES, or CONTACT_PAGES once the
    picker names the contact `query` means (the lookup stops as soon as that contact's 1:1 is listed)."""
    if pages is not None:
        return max(0, min(int(pages), MAX_PAGES))
    return CONTACT_PAGES if query and parse.contact_hits(people, query) else DEEP_PAGES


async def _load_more(page: "Page", count: int) -> bool:
    """Click the list's "Load more" (a read) and wait until it lists more than `count` rows: False when there is no
    such button or nothing more comes within MORE_SECONDS (the list is complete). Clicks again, at most every 3s,
    when a click didn't take."""
    reclick = _polls(3)
    for poll in range(_polls(MORE_SECONDS)):
        if poll % reclick == 0 and not await page.evaluate(CLICK_JS, SELECTORS["loadMore"]) and poll == 0:
            return False
        await page.wait_for_timeout(POLL_MS)
        snap = await _snapshot(page)
        await _state(page, snap)
        if (snap.get("items") or 0) > count:
            await page.wait_for_timeout(SETTLE_MS)
            return True
    return False


async def _deeper(page: "Page", rows: list[dict], done: Callable[[list[dict]], bool],
                  pages: int) -> tuple[list[dict], bool]:
    """Load up to `pages` more pages of the list, gently (PACE_MS between them), until done(rows). (rows, complete:
    the list has no more to load)."""
    for _ in range(pages):
        if done(rows):
            return rows, False
        if not await _load_more(page, len(rows)):
            return rows, True
        await page.wait_for_timeout(PACE_MS)
        rows = await _rows(page)
    return rows, False


async def _open_picker(page: "Page") -> None:
    """Open Start chat (a link: opening it is a read) and wait for its contact input."""
    for poll in range(_polls(THREAD_SECONDS)):
        snap = await page.evaluate(PICKER_JS, SELECTORS)
        if snap.get("open"):
            await page.wait_for_timeout(SETTLE_MS)
            return
        if poll % _polls(3) == 0:
            await page.evaluate(CLICK_JS, SELECTORS["start"])
        await page.wait_for_timeout(POLL_MS)
    raise RuntimeError(f"Google Messages' Start chat didn't open within {THREAD_SECONDS}s. {SNIFF_HINT}")


def _settled_key(snap: dict) -> str:
    return json.dumps([snap.get("contacts"), snap.get("sendTo"), snap.get("groups"), snap.get("chips")],
                      sort_keys=True)


async def _type(page: "Page", text: str) -> dict:
    """Type `text` into the open picker and return its snapshot (PICKER_JS) once settled: no skeleton, the input
    holding the text, and the same results three polls running (or PICKER_SECONDS passed)."""
    await page.locator(SELECTORS["contactInput"]).first.fill(text)
    await page.wait_for_timeout(3 * POLL_MS)  # the phone answers the search: let the old results go first
    last, same, snap = None, 0, {}
    for _ in range(_polls(PICKER_SECONDS)):
        snap = await page.evaluate(PICKER_JS, SELECTORS)
        key = _settled_key(snap)
        same = same + 1 if key == last and not snap.get("loading") and snap.get("input") == text else 0
        last = key
        if same >= 2:
            break
        await page.wait_for_timeout(POLL_MS)
    return snap


async def _type_number(page: "Page", number: str) -> tuple[dict, str]:
    """Type an E.164 number into the open picker in each form it may be saved in (parse.typed_forms) until a contact
    with that number shows; (the last snapshot, what was typed)."""
    forms, snap = parse.typed_forms(number), {}
    for typed in forms:
        snap = await _type(page, typed)
        if parse.has_number(parse.picker(snap)["contacts"], number):
            return snap, typed
    return snap, forms[0]  # no contact has it: "Send to" the number, typed as before


async def _look_up(page: "Page", query: str) -> dict:
    """What Messages' Start chat picker shows for `query` (a number typed in each form a contact may have saved it
    in, until one shows): its contacts and group conversations (parse.picker). The picker stays open; the list beside
    it is still there."""
    await _open_picker(page)
    number = parse.normalize_number(query)
    if number:
        return parse.picker((await _type_number(page, number))[0])
    return parse.picker(await _type(page, query))


async def _find(page: "Page", query: str, *, strict: bool = False, pages: int | None = None) -> dict:
    """The conversation `query` names. The newest conversations first (as before; for strict, only an id or an
    exact name settles it there); else deeper: the picker's contacts and group results, then up to `pages` (default
    DEEP_PAGES) "Load more" pages until the conversations of the contacts it names are listed. Several: UsageError
    (exit 2, listing them); none: NotFound (exit 3)."""
    rows = await _rows(page)
    try:
        found = parse.resolve(rows, query)
    except UsageError:  # several of the newest fit: the deeper lookup may still name one (an exact name, a 1:1)
        found = None
    if found is not None and _settles(found, query, strict):
        return _linked(found)
    look = await _look_up(page, query)
    people, group_rows = look["contacts"], look["groups"]
    budget = _pages(pages, query, people)
    rows, complete = await _deeper(page, rows, lambda have: parse.covered([*have, *group_rows], query, people),
                                   budget)
    rows, _complete, read = await _hunt(page, rows, complete, query, people, _pages(pages))
    number = parse.normalize_number(query)
    other = [r for r in rows if r["id"] in read and not _uses(read[r["id"]], number)] if number else []
    if other:  # a conversation its Details show uses another number isn't the one this number means
        rows = [r for r in rows if r not in other]
    try:
        found = parse.resolve_deep(rows, query, people, group_rows, strict=strict)
    except UsageError:
        # A number whose contact has several conversations by that name (one per number): the one that uses it.
        candidates = parse.strong(parse._unique([*rows, *group_rows]), query, people) if number else []
        found = await _by_number(page, candidates, number) if 1 < len(candidates) <= DETAILS_MAX else None
        if found is None:
            raise
    if found is None:
        named = [c["name"] or (c["numbers"][0]["number"] if c["numbers"] else "") for c in
                 parse.contact_hits(people, query)]
        hint = (f"{', '.join(named[:5])} {'is a contact' if len(named) == 1 else 'are contacts'} with no conversation "
                f"in the newest {len(rows)}: `send --new-ok` starts one, or raise --pages." if named else LIST_HINT)
        if other:
            hint = (f"{other[0]['name']}'s conversation ({other[0]['id']}) uses {', '.join(read[other[0]['id']])}, "
                    f"another of its numbers; none listed uses {number}. `send --new-ok` starts one, or raise --pages.")
        raise NotFound(f"No conversation matching {query!r} (looked through {len(rows)} conversations and Messages' "
                       "contact search).", hint=hint)
    return _linked(found)


def _uses(numbers: list[str] | None, number: str) -> bool:
    return bool(numbers) and len(numbers) == 1 and parse.same_number(numbers[0], number)


async def _hunt(page: "Page", rows: list[dict], complete: bool, query: str, people: list[dict],
                pages: int) -> tuple[list[dict], bool, dict[str, list[str]]]:
    """A number a contact has: the conversations named for that contact may use another of its numbers (LIVE: the
    picker shows only the number typed; the title can't say; their Details can, so each is opened from the list, a
    read). While every one
    listed does, keep loading (within the lookup's `pages`, counting those loaded already) until another by that
    name is listed, and check it: within --pages, else DEEP_PAGES (not CONTACT_PAGES: the contact's own conversation
    is listed already). (rows, complete, {id: the numbers its Details show})."""
    number = parse.normalize_number(query)
    names = {parse._fold(c["name"]) for c in parse.contact_hits(people, query)
             if number and c["name"]}
    read: dict[str, list[str] | None] = {}
    left = pages - -(-max(0, len(rows) - LIST_PAGE) // LIST_PAGE)
    while names:
        mine = [r for r in rows if r["id"] and parse._fold(r["name"]) in names]
        for row in mine[:DETAILS_MAX]:
            if row["id"] in read:
                continue
            read[row["id"]] = None
            if await page.evaluate(OPEN_JS, [SELECTORS, row["id"]]):
                try:
                    await _shown(page, row["id"])
                except RuntimeError:
                    continue
                read[row["id"]] = _numbers(await _participants(page))
        if complete or left <= 0 or len(read) >= DETAILS_MAX or any(_uses(v, number) for v in read.values()):
            break
        before = len(rows)
        rows, complete = await _deeper(page, rows, lambda have: any(
            r["id"] and r["id"] not in read and parse._fold(r["name"]) in names for r in have), left)
        left -= max(1, -(-(len(rows) - before) // LIST_PAGE))
    return rows, complete, {k: v for k, v in read.items() if v is not None}


def _settles(found: dict, query: str, strict: bool) -> bool:
    """A match among the newest conversations ends the lookup: an id, an exact name, or the conversation shown by
    the number; and (not strict) a 1:1 whose name contains the query. A group whose name merely contains it doesn't:
    the person's own 1:1 may be further back."""
    query = query.strip()
    if found["id"] == query or found["name"].casefold() == query.casefold():
        return True
    if parse.is_number(query):
        return len(parse.digits(found["name"])) >= 7 and parse.same_number(found["name"], query)
    return not strict and len(parse.header_parts(found["name"])) == 1


async def _by_number(page: "Page", candidates: list[dict], number: str) -> dict | None:
    """The one candidate conversation whose Details show it is the 1:1 with `number`, else None."""
    hits = []
    for row in candidates:
        if not row["id"]:
            continue
        await _open(page, row)
        members = await _participants(page)
        if members is not None and parse.uses_number(members, number):
            hits.append(row)
    return hits[0] if len(hits) == 1 else None


def _linked(found: dict) -> dict:
    if not found["id"]:
        raise RuntimeError(f"The conversation {found['name']!r} has no link to open. {SNIFF_HINT}")
    return found


async def _open(page: "Page", row: dict) -> None:
    """Open `row`'s conversation from the list (or the picker's group results), as a click would, else at its link;
    and wait until the page shows that conversation: its id in the URL, and its messages or compose box."""
    if not await page.evaluate(OPEN_JS, [SELECTORS, row["id"]]):
        await page.goto(f"{ORIGIN}/web/conversations/{row['id']}", wait_until="domcontentloaded", timeout=60_000)
    await _shown(page, row["id"])


async def _shown(page: "Page", thread: str) -> None:
    """Wait until the page shows conversation `thread`: its id in the URL, and its messages or compose box."""
    row = {"id": thread}
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


async def _overlay_closed(page: "Page") -> bool:
    """No Details dialog or menu open: Escape one that is, then its Cancel button; False when it stays."""
    for attempt in range(2):
        if not await page.evaluate(OVERLAY_JS, SELECTORS):
            return True
        if attempt == 0:
            await page.keyboard.press("Escape")
        else:
            await page.evaluate(CLICK_JS, SELECTORS["dialogCancel"])
        for _ in range(_polls(CLOSE_SECONDS)):
            if not await page.evaluate(OVERLAY_JS, SELECTORS):
                await page.wait_for_timeout(SETTLE_MS)
                return True
            await page.wait_for_timeout(POLL_MS)
    return False


async def _participants(page: "Page") -> list[dict] | None:
    """The open conversation's people with their numbers (parse.participants), from its menu's Details dialog:
    only the menu button and its Details button are clicked (never Archive, Move to trash, Report spam, Add people,
    or the mute toggle), and the dialog is closed again. Read once it lists the same people twice running. None
    when it lists nobody within DETAILS_SECONDS. RuntimeError when the dialog won't close (nothing else is done in
    the page then)."""
    members, last = None, None
    try:
        for poll in range(_polls(DETAILS_SECONDS)):
            raw = await page.evaluate(DETAILS_JS, SELECTORS)
            if raw:
                if raw == last:
                    members = parse.participants(raw)
                    break
                last = raw
            elif raw is None and poll % _polls(3) == 0:
                await page.evaluate(CLICK_JS, SELECTORS["menu"])
                await page.wait_for_timeout(POLL_MS)
                await page.evaluate(CLICK_JS, SELECTORS["detailsButton"])
            await page.wait_for_timeout(POLL_MS)
    finally:
        closed = await _overlay_closed(page)
    if not closed:
        raise RuntimeError(f"The conversation's Details dialog didn't close. {NOTHING} {SNIFF_HINT}")
    return members


def _numbers(members: list[dict] | None) -> list[str] | None:
    return None if members is None else [m["number"] for m in members]


async def _bubbles(page: "Page") -> list[dict]:
    return parse.messages(await page.evaluate(MESSAGES_JS, SELECTORS))


async def _hydrated(page: "Page") -> list[dict]:
    """The open conversation's raw bubbles (MESSAGES_JS), once they have hydrated (or MESSAGES_SECONDS have
    passed)."""
    for _ in range(_polls(MESSAGES_SECONDS)):
        if (await _snapshot(page)).get("messages"):
            break
        await page.wait_for_timeout(POLL_MS)
    await page.wait_for_timeout(SETTLE_MS)
    return await page.evaluate(MESSAGES_JS, SELECTORS)


async def _loaded(page: "Page") -> list[dict]:
    """The open conversation's bubbles, once they have hydrated (or MESSAGES_SECONDS have passed)."""
    return parse.messages(await _hydrated(page))


async def _closed(page: "Page") -> bool:
    """No lightbox open: Escape one that is, and wait CLOSE_SECONDS for it to go. False when it stays."""
    if not await page.evaluate(LIGHTBOX_JS, SELECTORS):
        return True
    await page.keyboard.press("Escape")
    for _ in range(_polls(CLOSE_SECONDS)):
        if not await page.evaluate(LIGHTBOX_JS, SELECTORS):
            return True
        await page.wait_for_timeout(POLL_MS)
    return False


async def _image(page: "Page", bubble: int, part: int) -> bytes | None:
    """One image's bytes, full size: with no lightbox open (so the one that opens is this image's), click its
    thumbnail and take the lightbox's image once it has loaded as a blob other than the thumbnail's; past
    IMAGE_SECONDS, whichever has loaded (the lightbox's, else the thumbnail's). Read before the lightbox closes. None
    when nothing loaded or a lightbox won't close. Only a blob: of the app's origin is read: that makes no request."""
    if not await _closed(page) or not await page.evaluate(OPEN_IMAGE_JS, [SELECTORS, bubble, part]):
        return None
    src = None
    for _ in range(_polls(IMAGE_SECONDS)):
        preview = await page.evaluate(PREVIEW_JS, [SELECTORS, bubble, part])
        full = ((await page.evaluate(LIGHTBOX_JS, SELECTORS)) or {}).get("src")
        src = full or preview
        if full and full != preview:
            break
        await page.wait_for_timeout(POLL_MS)
    url = await page.evaluate(BLOB_JS, src) if (src or "").startswith(BLOB_PREFIX) else None
    await _closed(page)
    head, _, body = (url or "").partition(",")
    return base64.b64decode(body) if head.endswith(";base64") and body else None


async def _save_images(page: "Page", raw: list[dict], limit: int | None, thread: str,
                       out_dir: str) -> tuple[dict[int, list[str]], int]:
    """Save each image of the last `limit` rows as out_dir/<thread>-<n><ext> (parse.image_parts' n; a file of that
    name is replaced). ({bubble index: saved paths}, how many images didn't load).
    ponytail: bubbles are found again by their index in `raw`, which holds while the app only appends (a new
    message); find them by msg-id if the app ever prepends while a read runs (it does on scrolling up, which this
    never does)."""
    saved: dict[int, list[str]] = {}
    missing = 0
    for bubble, part, n in parse.image_parts(raw, limit):
        data = await _image(page, bubble, part)
        if not data:
            missing += 1
            continue
        name = google.safe_name(f"{thread}-{n}{parse.image_ext(data)}", f"image-{n}.bin")
        saved.setdefault(bubble, []).append(str(google.save_file(out_dir, name, data).resolve()))
    return saved, missing


async def status(*, headed: bool = False) -> dict:
    """{signedIn, paired, status}: paired when the app shows its conversation list rather than its QR code (gmsg's
    status). headed is accepted for the daemon contract; data sites always run headless."""
    async with _page(POLICY) as page:
        try:
            await open_app(page)
        except AuthRequired:
            return {"signedIn": False, "paired": False, "status": "needs_pairing"}
    return {"signedIn": True, "paired": True, "status": "active"}


async def conversations(limit: int | None = None, *, headed: bool = False) -> list[dict]:
    """The conversation list as the app has loaded it (newest first), at most `limit` rows."""
    async with _page(POLICY) as page:
        await open_app(page)
        rows = await _rows(page)
    return rows[:limit] if limit else rows


async def search(query: str, pages: int | None = None, *, headed: bool = False, numbers: bool = True) -> dict:
    """{results, scanned, complete}: parse.search_deep over the deeper lookup. Messages' Start chat picker for
    `query` (its contacts, and its own search of group conversations by member), then up to `pages` (default
    DEEP_PAGES, or CONTACT_PAGES once the picker names a contact) "Load more" pages of the list, stopping once every
    contact it names has its conversation listed. scanned: the conversations looked through; complete: the whole
    list was. With numbers, the first DETAILS_MAX conversations found are opened (from the list, a read) and their
    Details read: parse.with_numbers. READ-ONLY."""
    async with _page(POLICY) as page:
        await open_app(page)
        look = await _look_up(page, query)
        people, group_rows = look["contacts"], look["groups"]
        budget = _pages(pages, query, people)
        rows, complete = await _deeper(page, await _rows(page),
                                       lambda have: parse.covered([*have, *group_rows], query, people), budget)
        read: dict[str, list[str]] = {}
        if numbers:
            rows, complete, read = await _hunt(page, rows, complete, query, people, _pages(pages))
        results = parse.search_deep(rows, query, people, group_rows)
        if numbers:
            results = parse.with_numbers(results, {**read, **await _numbers_of(page, results, read)})
    return {"results": results, "scanned": len(rows), "complete": complete}


async def _numbers_of(page: "Page", results: list[dict], known: dict | None = None) -> dict[str, list[str]]:
    """{conversation id: its people's numbers} for the first DETAILS_MAX conversation rows (1:1s first) that open
    from the page as it is (the list, or the picker's group results; never a reload, which would drop the loaded pages)."""
    read: dict[str, list[str]] = {}
    known = known or {}
    rows = [r for r in results if r["kind"] == "conversation" and r["id"] and r["id"] not in known]
    # 1:1s (and anything matched by more than a group search) first: a group result opens only from the picker,
    # which is gone once a conversation is open.
    rows.sort(key=lambda r: set(r.get("matched") or []) <= {"group"})
    for row in rows[:max(0, DETAILS_MAX - len(known))]:
        if row["id"] in read or not await page.evaluate(OPEN_JS, [SELECTORS, row["id"]]):
            continue
        try:
            await _shown(page, row["id"])
        except RuntimeError:
            continue
        members = await _participants(page)
        if members is not None:
            read[row["id"]] = _numbers(members)
    return read


async def messages(query: str, limit: int | None = None, *, image_dir: str | None = None,
                   headed: bool = False, pages: int | None = None) -> dict:
    """{conversation, messages}: the messages the app shows for the conversation `query` names (oldest first, the
    last `limit`). READ-ONLY: the guard aborts the app's mark-as-read.

    With image_dir (an existing absolute directory, `read --image-out`) the page opens with the guard's download
    permission, which lets the app fetch its encrypted attachments, and each image in those messages is saved there
    full size (_image): every row then has images (its saved paths), and the result has imagesMissing (how many
    didn't load)."""
    async with _page(POLICY, download=image_dir is not None) as page:
        await open_app(page)
        row = await _find(page, query, pages=pages)
        await _open(page, row)
        members = await _participants(page)
        number = parse.normalize_number(query)
        if number and members and len(members) == 1 and not parse.uses_number(members, number):
            raise NotFound(f"The conversation {row['name']!r} ({row['id']}) uses {members[0]['number']}, not "
                           f"{number}.", hint="That contact has more than one number: see `webcli messages search "
                                              f"{number}` (its rows show each conversation's number).")
        row = {**row, "numbers": _numbers(members), "participants": members}
        raw = await _hydrated(page)
        if image_dir is None:
            rows = parse.messages(raw)
            return {"conversation": row, "messages": rows[-limit:] if limit else rows}
        saved, missing = await _save_images(page, raw, limit, row["id"], image_dir)
    rows = parse.messages(raw, saved)
    return {"conversation": row, "messages": rows[-limit:] if limit else rows, "imagesMissing": missing}


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
    """How many sent bubbles show exactly `text` (parse.same_text: whitespace runs aside, so a line break counts)."""
    return sum(1 for row in rows if row["direction"] == "sent" and parse.same_text(row["text"], text))


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


def _refused(why: str, confirm: bool) -> RuntimeError:
    return RuntimeError(f"REFUSED{'' if confirm else ' (dry run)'}: {why} {NOTHING}")


def _expect_people(row: dict, query: str) -> list[dict]:
    """An existing conversation's members, for --expect-recipient: its name's parts; a 1:1 found by number also has
    that number."""
    people = parse.conversation_people(row)
    if len(people) == 1 and parse.is_number(query) and not people[0]["number"]:
        people[0]["number"] = parse.normalize_number(query) or ""
    return people


async def send(query: str | list[str], text: str, *, confirm: bool = False, headed: bool = False,
               new_ok: bool = False, expect: list[str] | None = None, pages: int | None = None) -> dict:
    """One text to one conversation: an existing one `query` names, or (new_ok only) the 1:1 or group conversation
    with exactly the recipients `query` lists (Start chat). Never a new conversation without new_ok; never a
    recipient that resolved ambiguously (UsageError, exit 2). expect (--expect-recipient): the recipients the caller
    expects; they must name exactly the resolved ones, dry run included (else REFUSED, exit 5).

    Without confirm, a dry run: it resolves the conversation, or the recipients (Messages' Start chat picker, typing
    only the names or numbers there), and stops; it opens no compose box and types no message. With confirm, an
    existing conversation (_send_existing):
      1. open it and wait until the page shows exactly it (its id in the URL); its title must name it (and, with
         expect, exactly those recipients);
      2-5. _type_and_send: type, check, arm the one send slot, click, verify. Never retried.
    A new one (_start): open Start chat, pick each recipient (a contact row or "Send to" with exactly the resolved
    number; in group mode the chips must then be exactly the recipients), arm the guard's one
    GET_OR_CREATE_CONVERSATION slot and click; the conversation that opens must be titled with exactly the
    recipients; then _type_and_send.
    """
    queries = [query] if isinstance(query, str) else [q for q in query]
    try:
        text = parse.message_text(text)
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    if not queries or any(not q.strip() for q in queries):
        raise UsageError("--to is empty.")
    if len(queries) > 1 and not new_ok:
        raise UsageError("Several --to make a group conversation, and webcli starts conversations only with --new-ok.",
                         hint="Add --new-ok (and --expect-recipient for each recipient to --confirm).")
    if confirm and new_ok and not expect:
        raise UsageError("A confirmed --new-ok send needs --expect-recipient for each recipient.",
                         hint="e.g. --expect-recipient 'Alex Doe' --expect-recipient +14175550100")
    expect = [e.strip() for e in expect] if expect else None
    scope = SendScope(new_ok=new_ok) if confirm else None
    async with _page(send_policy(scope), send=confirm) as page:
        await open_app(page)
        row = None
        if len(queries) == 1:
            try:
                row = await _find(page, queries[0], strict=True, pages=pages)
            except NotFound:
                if not new_ok:
                    raise
        if row is not None:
            return await _send_existing(page, row, queries[0], text, confirm, scope, expect)
        people = await _recipients(page, queries)
        out = {"dryRun": not confirm, "new": True, "group": len(people) > 1,
               "recipients": [{"query": p["query"], "name": p["name"], "number": p["number"]} for p in people],
               "to": ", ".join(parse.label(p) for p in people), "threadId": None, "text": text,
               "expectRecipients": expect, "expectRecipientsMatch": None if expect is None else
               parse.expect_matches(expect, people)}
        if expect is not None and not out["expectRecipientsMatch"]:
            raise _refused(f"--expect-recipient ({', '.join(expect)}) doesn't name exactly the recipients --to "
                           f"resolved to ({out['to']}).", confirm)
        script, arg, chips = await _stage(page, people, confirm)
        if not confirm:  # Start chat showed exactly these recipients; nothing was clicked that opens a conversation
            return {**out, "staged": True, **({"chips": chips} if out["group"] else {})}
        thread = await _create(page, scope, script, arg, people)
        result = await _type_and_send(page, thread, text, scope)
    return {**out, "threadId": thread, "created": scope.created, **result, "titleChecked": True}


async def _send_existing(page: "Page", row: dict, query: str, text: str, confirm: bool, scope: SendScope | None,
                         expect: list[str] | None) -> dict:
    """An existing conversation. When --to or an --expect-recipient is a number, the title (a contact's name) can't
    say which number the conversation uses, so its Details are read (dry run too: opening it is a read) and must
    show it: --to's number as the 1:1's one person, and --expect-recipient exactly its people, by name or number.
    Unreadable Details, another number, or one person too many refuse (exit 5)."""
    by_number = parse.is_number(query) or any(parse.is_number(e) for e in expect or [])
    out = {"dryRun": not confirm, "to": row["name"], "threadId": row["id"], "text": text}
    people = _expect_people(row, query)
    if by_number:
        await _open(page, row)
        members = await _participants(page)
        out["numbers"] = _numbers(members)
        if members is None:
            raise _refused(f"couldn't read which number conversation {row['name']!r} uses (its Details), so it "
                           "can't be checked against the number given.", confirm)
        number = parse.normalize_number(query)
        if number and not parse.uses_number(members, number):
            raise _refused(f"conversation {row['name']!r} is with {', '.join(_numbers(members)) or 'nobody'}, not "
                           f"exactly {number}.", confirm)
        people = members
    if expect is not None:
        out["new"] = False
        out["expectRecipients"], out["expectRecipientsMatch"] = expect, parse.expect_matches(expect, people)
        if not out["expectRecipientsMatch"]:
            shown = f" ({', '.join(out['numbers'])})" if by_number else ""
            raise _refused(f"--expect-recipient ({', '.join(expect)}) doesn't name exactly conversation "
                           f"{row['name']!r}{shown}.",
                           confirm)
    if not confirm:
        return out
    if not by_number:
        await _open(page, row)
    header = await page.evaluate(HEADER_JS, SELECTORS["header"])
    if header and not parse.names_match(header, row["name"]):
        raise RuntimeError(f"REFUSED: the open conversation is titled {header!r}, not {row['name']!r}. {NOTHING}")
    if header and expect is not None and not by_number and not parse.expect_matches(
            expect, _expect_people({**row, "name": header}, query)):
        raise RuntimeError(f"REFUSED: the open conversation is titled {header!r}, which isn't exactly "
                           f"{', '.join(expect)}. {NOTHING}")
    result = await _type_and_send(page, row["id"], text, scope)
    return {**out, **result, "titleChecked": bool(header)}


async def _recipients(page: "Page", queries: list[str]) -> list[dict]:
    """Each --to as one recipient (parse.recipient, from what the Start chat picker shows for it). Two --to that are
    one number: UsageError."""
    await _open_picker(page)
    people: list[dict] = []
    for query in queries:
        number = parse.normalize_number(query)
        snap, typed = await _type_number(page, number) if number else (await _type(page, query), query)
        person = parse.recipient(query, parse.picker(snap)["contacts"])
        if any(p["number"] == person["number"] for p in people):
            raise UsageError(f"--to {query!r} is a recipient already ({person['number']}).")
        people.append({**person, "typed": typed if number else None})
    await page.locator(SELECTORS["contactInput"]).first.fill("")
    return people


async def _group_mode(page: "Page") -> bool:
    """Click Start chat's "Start group conversation". LIVE (2026-10-23): the button is gone while the picker's input
    holds text and comes back ~250ms after it is emptied, so it is waited for (GROUP_BUTTON_SECONDS); clicking at
    once found none ("Start chat shows no group button")."""
    for _ in range(_polls(GROUP_BUTTON_SECONDS)):
        if await page.evaluate(CLICK_JS, SELECTORS["startGroup"]):
            await page.wait_for_timeout(SETTLE_MS)
            return True
        await page.wait_for_timeout(POLL_MS)
    return False


async def _offer(page: "Page", person: dict) -> tuple[str, int] | None:
    """Type `person`'s number into the picker (the form that found its contact first, then each other form) until
    the picker offers exactly that person (parse.pick)."""
    forms = list(dict.fromkeys([f for f in (person.get("typed"), *parse.typed_forms(person["number"])) if f]))
    for typed in forms:
        target = parse.pick(await _type(page, typed), person)
        if target is not None:
            return target
    return None


async def _stage(page: "Page", people: list[dict], confirm: bool) -> tuple[str, object, list[str]]:
    """Start chat up to the one click that opens (or creates) the conversation, without making it: a 1:1's contact
    row (or "Send to") with exactly its number; a group's "Start group conversation" mode with each recipient
    picked that way (picking adds a chip in the page; nothing is sent or created) and the chips then exactly the
    recipients, and its Next button. (the script and arg that click, the chips). A dry run stops here (the page then
    leaves the app). REFUSED (exit 5, "(dry run)" without confirm) when Start chat doesn't offer that."""
    group = len(people) > 1
    await page.locator(SELECTORS["contactInput"]).first.fill("")
    if group and not await _group_mode(page):
        raise _refused(f"Start chat shows no \"Start group conversation\" button within {GROUP_BUTTON_SECONDS}s. "
                       f"{SNIFF_HINT}", confirm)
    for done, person in enumerate(people):
        target = await _offer(page, person)
        if target is None:
            raise _refused(f"Start chat doesn't offer {parse.label(person)} ({person['number']}) exactly.", confirm)
        kind, index = target
        if not group:
            return PICK_JS, [SELECTORS, kind, index], []
        if not await page.evaluate(PICK_JS, [SELECTORS, kind, index]):
            raise _refused(f"couldn't pick {parse.label(person)} in Start chat.", confirm)
        if not await _until_chips(page, done + 1):
            raise _refused(f"picking {parse.label(person)} didn't add it as a recipient.", confirm)
    snap = await page.evaluate(PICKER_JS, SELECTORS)
    chips = snap.get("chips") or []
    if not parse.same_set(chips, people):
        raise _refused(f"the recipients picked are {', '.join(chips) or 'none'}, not exactly "
                       f"{', '.join(parse.label(p) for p in people)}.", confirm)
    if not snap.get("next"):
        raise _refused(f"Start chat shows no Next button for the group. {SNIFF_HINT}", confirm)
    return CLICK_JS, SELECTORS["next"], chips


async def _until_chips(page: "Page", count: int) -> bool:
    for _ in range(_polls(THREAD_SECONDS)):
        if len((await page.evaluate(PICKER_JS, SELECTORS)).get("chips") or []) == count:
            await page.wait_for_timeout(SETTLE_MS)
            return True
        await page.wait_for_timeout(POLL_MS)
    return False


async def _create(page: "Page", scope: SendScope, script: str, arg, people: list[dict]) -> str:
    """Arm the guard's one GET_OR_CREATE_CONVERSATION slot (CREATE_SECONDS), click (`script` with `arg`), and wait
    for a conversation to open; its title must be exactly `people`. Fails closed (RuntimeError, nothing sent) on
    anything else, such as a step asking for a group name: the conversation may exist by then (created says)."""
    scope.arm_create(CREATE_SECONDS)
    try:
        clicked = await page.evaluate(script, arg)
        thread = None
        for _ in range(_polls(CREATE_SECONDS)):
            thread = parse.thread_id((await _snapshot(page)).get("path"))
            if thread:
                break
            await page.wait_for_timeout(POLL_MS)
    finally:
        scope.disarm_create()
    maybe = " (the conversation may exist now)" if scope.created else ""
    if not clicked or not thread:
        raise RuntimeError(f"REFUSED: Start chat didn't open a conversation within {CREATE_SECONDS}s{maybe}. "
                           f"{NOTHING}{blocked()}")
    await _shown(page, thread)
    header = await page.evaluate(HEADER_JS, SELECTORS["header"])
    titled = bool(header) and parse.same_set(parse.header_parts(header) if len(people) > 1 else [header], people)
    # The title shows a contact's name even when --to (or --expect-recipient) was its number, and can't tell a
    # contact's two numbers apart: the Details' numbers can. Shown, they must be exactly the recipients' numbers.
    members = await _participants(page)
    numbered = members is not None and parse.numbers_match(members, people)
    if members is not None and not numbered:
        raise RuntimeError(f"REFUSED: the conversation that opened is with {', '.join(_numbers(members)) or 'nobody'}"
                           f", not exactly {', '.join(p['number'] for p in people)}{maybe}. {NOTHING}")
    if not titled and not numbered:
        raise RuntimeError(f"REFUSED: the conversation that opened is titled {header!r}, not exactly "
                           f"{', '.join(parse.label(p) for p in people)}{maybe}. {NOTHING}")
    return thread


async def _type_and_send(page: "Page", thread: str, text: str, scope: SendScope) -> dict:
    """In the open conversation `thread`:
      2. type the text into the compose box and wait for the send button;
      3. then, last: the URL must still be that conversation's, the compose box must read back exactly the text, and
         the page must have tried no send of its own this command (a queued message or a retry the guard blocked);
      4. arm the guard's one slot and click send: the slot passes one SEND_MESSAGE, or closes after SEND_SECONDS;
      5. wait for the thread to show one more sent bubble with the text (confirmed). Never retried.
    {sent: True, confirmed}; RuntimeError (nothing sent) when a check refuses or the click sent nothing."""
    compose = await _compose(page)
    if compose is None:
        raise RuntimeError(f"REFUSED: no compose box in conversation {thread}. {NOTHING} {SNIFF_HINT}")
    before = _sent_count(await _loaded(page), text)
    await compose.fill(text)
    button = await _send_button(page)
    if button is None:
        await _refuse(compose, f"no enabled send button within {BUTTON_SECONDS}s.")
    # Last, right before the slot opens (finding the button can take seconds): still that conversation, still
    # exactly the text, and no send of the page's own so far.
    shown = parse.thread_id((await _snapshot(page)).get("path"))
    if shown != thread:  # the compose box on screen now is another conversation's: leave it alone
        raise RuntimeError(f"REFUSED: the page moved to conversation {shown}, not {thread}. {NOTHING}")
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
    return {"sent": True, "confirmed": await _shows(page, text, before)}


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
    async with session.hold(), browser.open_page(None, mode="visible") as page:
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
