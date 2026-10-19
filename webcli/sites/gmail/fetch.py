"""Browser-side work for webcli gmail: status, the filters list, and the two writes (create_filter, delete_filter).

Everything works on Settings > Filters (UI), as google-gmail-cli did: `filters` reads the table; the writes drive the
page's own dialogs. Reads open their page with the read-only POLICY. The writes run only with confirm=True, and only
then open their page with send on and write_policy(scope): the guard lets exactly one filter write through, bound to
that command's act and its filter id or criteria. The CLI's dry runs never call them.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from webcli.core import browser
from webcli.core.errors import AuthRequired, NotFound, UsageError
from webcli.sites import google
from webcli.sites.gmail import parse
from webcli.sites.gmail.endpoints import (
    ACTION_LABELS,
    APP_URL,
    CONFIRM_BUTTONS,
    CREATE_BUTTON,
    CREATE_LINK,
    CRITERIA_LABELS,
    DIALOG_TRIES,
    FILTERS_URL,
    HAS_ATTACHMENT,
    LOAD_TRIES,
    SAVE_MS,
    SETTLE_MS,
)
from webcli.sites.gmail.guard import POLICY, WriteScope, write_policy

if TYPE_CHECKING:
    from playwright.async_api import Page

SNIFF_HINT = ("Gmail's settings page may have changed, or the guard blocked what it needs; run `python -m "
              "webcli.core.capture gmail --url https://mail.google.com/mail/u/0/#settings/filters` and compare with "
              "webcli/sites/gmail/endpoints.py.")
EMAIL_JS = r"() => { const m = (document.title || '').match(/[\w.+-]+@[\w-]+(?:\.[\w-]+)+/); return m ? m[0] : null; }"
ROWS_JS = "() => document.querySelectorAll('table.cf tr.r7 span.sA').length"
EMPTY_JS = "() => /create a new filter/i.test(document.body ? document.body.innerText : '')"
# A row's id: the delete control's DOM id ends "#<filter id>" (numeric for old filters, z...*N for new ones).
ID_JS = r"""
const filterId = (raw) => {
  raw = String(raw || '');
  let m = raw.match(/#(\d{6,})$/);
  if (m) return m[1];
  m = raw.match(/#([^\s"']+)/);
  return m ? m[1] : '';
};
"""
LIST_JS = "() => {" + ID_JS + r"""
  return Array.from(document.querySelectorAll('table.cf tr.r7')).map(r => {
    const spans = Array.from(r.querySelectorAll('span'));
    const edit = spans.find(s => /^edit$/i.test((s.innerText || '').trim()));
    const dele = spans.find(s => /^delete$/i.test((s.innerText || '').trim()));
    if (!edit || !dele) return null;
    const crit = r.querySelector('span.qW');
    const tds = Array.from(r.querySelectorAll('td'));
    let actionText = '';
    if (tds.length >= 2) {
      const clone = tds[1].cloneNode(true);
      clone.querySelectorAll('span').forEach(s => { if (/^(edit|delete)$/i.test((s.innerText || '').trim())) s.remove(); });
      actionText = (clone.innerText || '').replace(/\s+/g, ' ').trim();
    }
    return {id: filterId(dele.id) || filterId(edit.id), criteria: crit ? (crit.innerText || '').trim() : '', actionText};
  }).filter(Boolean);
}"""
# Clicks the delete control of the row whose id is exactly `wanted`; false when there is none.
CLICK_DELETE_JS = "(wanted) => {" + ID_JS + r"""
  for (const r of document.querySelectorAll('table.cf tr.r7')) {
    const dele = Array.from(r.querySelectorAll('span')).find(s => /^delete$/i.test((s.innerText || '').trim()));
    if (dele && filterId(dele.id) === wanted) { dele.click(); return true; }
  }
  return false;
}"""
CLICK_TEXT_JS = r"""
(text) => {
  const el = Array.from(document.querySelectorAll('a,button,span,div[role=button],input[type=submit]'))
    .find(e => (e.innerText || e.value || '').trim() === text);
  if (!el) return false;
  el.click();
  return true;
}
"""
HAS_LABEL_JS = r"""
(text) => Array.from(document.querySelectorAll('label, td, span, div'))
  .some(e => (e.innerText || '').trim().toLowerCase() === text.toLowerCase())
"""
# The text box on the same line as a dialog label (the create dialog's fields have no names): its center.
FIELD_JS = r"""
(text) => {
  const lab = Array.from(document.querySelectorAll('label, td, span, div'))
    .find(e => (e.innerText || '').trim().toLowerCase() === text.toLowerCase());
  if (!lab) return null;
  const ly = lab.getBoundingClientRect().y;
  const input = Array.from(document.querySelectorAll('input[type=text], textarea')).find(i => {
    const r = i.getBoundingClientRect();
    return r.width > 10 && Math.abs(r.y - ly) < 30 && r.y > 40 && r.y < 800;
  });
  if (!input) return null;
  input.focus();
  input.value = '';
  const r = input.getBoundingClientRect();
  return {x: r.x + r.width / 2, y: r.y + r.height / 2};
}
"""


async def status(*, headed: bool = False) -> dict:
    """{signedIn, email}: signed in when Gmail loads without bouncing to Google's sign-in; email from the page title
    (best effort, else null). headed is accepted for the daemon contract; data sites always run headless."""
    async with browser.site_page(POLICY) as page:
        try:
            await google.open_app(page, APP_URL)
        except AuthRequired:
            return {"signedIn": False, "email": None}
        try:
            email = await page.evaluate(EMAIL_JS)
        except Exception:
            email = None
        return {"signedIn": True, "email": email}


async def open_filters(page: "Page") -> None:
    """Load Settings > Filters and wait for its table (or the empty list's "Create a new filter" link)."""
    await google.open_app(page, FILTERS_URL)
    for _ in range(LOAD_TRIES):
        if await page.evaluate(ROWS_JS):
            return
        if await page.evaluate(EMPTY_JS):
            await page.wait_for_timeout(SETTLE_MS)  # rows can render after the heading
            return
        await page.wait_for_timeout(1_000)
    raise RuntimeError(f"Gmail's filters settings didn't load within {LOAD_TRIES}s.{google.blocked()} {SNIFF_HINT}")


async def _rows(page: "Page") -> list[dict]:
    raw = await page.evaluate(LIST_JS)
    return [parse.normalize(r) for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []


async def filters(*, headed: bool = False) -> list[dict]:
    """Every filter the settings table shows: {id, matches, do_this, from, to, subject, query, hasAttachment,
    criteria}."""
    async with browser.site_page(POLICY) as page:
        await open_filters(page)
        return await _rows(page)


async def _click(page: "Page", text: str, *, last: bool = False, dom: bool = True) -> bool:
    """Click the element whose whole text is exactly `text` (Playwright's text match first, then, with dom, a DOM
    search). Case counts: a row's lowercase "delete" link never matches "Delete"."""
    try:
        target = page.get_by_text(text, exact=True)
        await (target.last if last else target.first).click(timeout=5_000)
        return True
    except Exception:
        return dom and bool(await page.evaluate(CLICK_TEXT_JS, text))


async def create_filter(plan: dict, *, confirm: bool = False, headed: bool = False) -> dict:
    """Create a filter through the create dialogs: `plan` is parse.create_plan's {criteria, actions}. A WRITE: runs
    only with confirm=True. Returns the new filter's row; an error when it doesn't appear (nothing was created, or
    the guard blocked the write: the message names it)."""
    if not confirm:
        raise UsageError("create_filter writes: it runs only with confirm=True.")
    plan = parse.create_plan(plan.get("criteria") or {}, plan.get("actions") or [])
    scope = WriteScope("create", parse.needles(plan))
    async with browser.site_page(write_policy(scope), send=True) as page:
        await open_filters(page)
        before = {r["id"] for r in await _rows(page)}
        if not await _click(page, CREATE_LINK):
            raise RuntimeError(f"Couldn't open {CREATE_LINK!r}. {SNIFF_HINT}")
        for _ in range(DIALOG_TRIES):
            if await page.evaluate(HAS_LABEL_JS, CRITERIA_LABELS["from"]):
                break
            await page.wait_for_timeout(500)
        else:
            raise RuntimeError(f"The create-filter dialog didn't open. {SNIFF_HINT}")
        filled = []
        for key, label in CRITERIA_LABELS.items():
            value = plan["criteria"].get(key)
            box = await page.evaluate(FIELD_JS, label) if value else None
            if box:
                await page.mouse.click(box["x"], box["y"])
                await page.keyboard.type(value, delay=15)
                filled.append(key)
        if plan["criteria"].get("hasAttachment") and await _click(page, HAS_ATTACHMENT):
            filled.append("hasAttachment")
        if set(filled) != set(plan["criteria"]):
            missing = sorted(set(plan["criteria"]) - set(filled))
            raise RuntimeError(f"Couldn't fill the dialog's {', '.join(missing)} field(s); nothing was created. "
                               f"{SNIFF_HINT}")
        if not await _click(page, CREATE_BUTTON):
            raise RuntimeError(f"Couldn't advance to the filter's actions; nothing was created. {SNIFF_HINT}")
        await page.wait_for_timeout(2_000)
        for action in plan["actions"]:
            if not await _click(page, ACTION_LABELS[action]):
                raise RuntimeError(f"Couldn't tick {ACTION_LABELS[action]!r}; nothing was created. {SNIFF_HINT}")
        if not await _click(page, CREATE_BUTTON, last=True):
            raise RuntimeError(f"Couldn't press {CREATE_BUTTON!r}; nothing was created. {SNIFF_HINT}")
        await page.wait_for_timeout(SAVE_MS)
        await open_filters(page)
        created = [r for r in await _rows(page) if r["id"] not in before]
        if not created:
            raise RuntimeError(f"The new filter didn't appear in the list.{google.blocked()} {SNIFF_HINT}")
        return created[-1]


async def delete_filter(filter_id: str, *, confirm: bool = False, headed: bool = False) -> dict:
    """Delete the filter whose id is exactly `filter_id`. A WRITE: runs only with confirm=True. {deleted, filter};
    NotFound when no row has that id; an error when it is still listed afterwards."""
    if not confirm:
        raise UsageError("delete_filter writes: it runs only with confirm=True.")
    filter_id = filter_id.strip()
    if not filter_id or not re.fullmatch(r"[^\s\"']+", filter_id):
        raise UsageError(f"{filter_id!r} is not a filter id.", hint="See `webcli gmail filters list`.")
    scope = WriteScope("delete", (filter_id,))
    async with browser.site_page(write_policy(scope), send=True) as page:
        await open_filters(page)
        target = parse.find(await _rows(page), filter_id)
        if target is None:
            raise NotFound(f"No filter {filter_id}.", hint="See `webcli gmail filters list`.")
        if not await page.evaluate(CLICK_DELETE_JS, filter_id):
            raise RuntimeError(f"Couldn't press delete on filter {filter_id}. {SNIFF_HINT}")
        await page.wait_for_timeout(1_000)
        for name in CONFIRM_BUTTONS:  # Playwright's match only: a DOM fallback could reach another row
            if await _click(page, name, dom=False):
                break
        else:
            raise RuntimeError(f"The delete confirmation didn't show; nothing was deleted. {SNIFF_HINT}")
        await page.wait_for_timeout(SAVE_MS)
        await open_filters(page)
        if parse.find(await _rows(page), filter_id) is not None:
            raise RuntimeError(f"Filter {filter_id} is still listed after the delete.{google.blocked()} {SNIFF_HINT}")
        return {"deleted": filter_id, "filter": target}
