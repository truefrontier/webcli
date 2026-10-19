"""Browser-side work for webcli contacts: status, labels, list, other, search, get, and the one write, label
membership (label_change). Everything else is READ-ONLY.

Every op loads contacts.google.com in a fresh headless tab, then calls the app's own batchexecute RPCs
(endpoints.RPC) with fetch() inside that page (credentials: 'include'). The page's cookies, its `at` token, and its
session id stay in the page: BATCH_JS reads them there and hands Python only the answer. Nothing here clicks, types,
or submits anything.

label_change is a dry run unless confirm=True. Only confirm=True opens its page with send on and the write policy
(guard.write_policy) holding a WriteScope; a dry run's page has neither, so the guard would abort a write.
"""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING, Any, Callable

from webcli.core import browser
from webcli.core.errors import AuthRequired, NotFound, RateLimited, UsageError
from webcli.sites import google
from webcli.sites.contacts import parse
from webcli.sites.contacts.endpoints import (
    APP_URL,
    BATCH_PAUSE,
    GET_CHUNK,
    MAX_PAGES,
    MEMBERSHIP_BATCH,
    RPC,
    SYSTEM_GROUP_NAMES,
    TOKEN_SECONDS,
    WRITE_RPC,
)
from webcli.sites.contacts.guard import POLICY, WriteScope, write_policy

if TYPE_CHECKING:
    from playwright.async_api import Page

CHANGED_HINT = ("Google Contacts may have changed; run `python -m webcli.core.capture contacts` and compare with "
                "webcli/sites/contacts/endpoints.py and parse.py.")

# Runs inside the page. Tokens never leave it; Python gets {status, body}.
BATCH_JS = """
async ({rpcid, payload}) => {
  const wiz = window.WIZ_global_data || {};
  if (!wiz.SNlM0e) return {status: 0, body: ''};
  const params = new URLSearchParams({rpcids: rpcid, 'source-path': location.pathname,
    hl: document.documentElement.lang || 'en', _reqid: String(100000 + Math.floor(Math.random() * 900000)), rt: 'c'});
  if (wiz.cfb2h) params.set('bl', wiz.cfb2h);
  if (wiz.FdrFJe) params.set('f.sid', wiz.FdrFJe);
  const body = new URLSearchParams({'f.req': JSON.stringify([[[rpcid, JSON.stringify(payload), null, 'generic']]]),
    at: wiz.SNlM0e});
  try {
    const res = await fetch('/_/ContactsUi/data/batchexecute?' + params, {method: 'POST', credentials: 'include',
      redirect: 'manual', signal: AbortSignal.timeout(90000),
      headers: {'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8', 'X-Same-Domain': '1'}, body});
    return {status: res.type === 'opaqueredirect' ? 0 : res.status, body: await res.text()};
  } catch (e) {
    return {status: -1, body: String(e && e.name || e)};
  }
}
"""
HAS_TOKEN_JS = "() => !!(window.WIZ_global_data && window.WIZ_global_data.SNlM0e)"
BUILD_JS = "() => (window.WIZ_global_data || {}).cfb2h || null"  # the app's build label: public, not a secret


class SiteError(RuntimeError):
    """Google answered something webcli can't use: a changed format, an HTTP error, a timeout (exit 5)."""


def signed_out() -> dict:
    return {"signedIn": False, "email": None, "contactsApi": None}


async def _wait_token(page: "Page") -> bool:
    try:
        await page.wait_for_function(HAS_TOKEN_JS, timeout=TOKEN_SECONDS * 1000)
        return True
    except Exception:
        return False


async def signed_in_page(page: "Page") -> None:
    await google.open_app(page, APP_URL)
    if not await _wait_token(page):
        raise AuthRequired("Not signed in to Google Contacts (the page has no session token).")


async def rpc(page: "Page", rpcid: str, payload: Any) -> Any:
    """One RPC from inside the page; its decoded payload.

    HTTP 0 (a redirect: sign-in), 401, 403, or a permission code -> AuthRequired. HTTP 429 or RESOURCE_EXHAUSTED ->
    RateLimited. Anything else that isn't a clean answer -> SiteError.
    """
    res = await page.evaluate(BATCH_JS, {"rpcid": rpcid, "payload": payload})
    status = res["status"]
    if status in (0, 401, 403):
        raise AuthRequired(f"Google refused {rpcid} (HTTP {status}); the session is missing or expired.")
    if status == 429:
        raise RateLimited(f"Google rate-limited {rpcid} (HTTP 429).", sent=False)
    if status == -1:
        raise SiteError(f"{rpcid} request failed in the page ({res['body'][:80]}).{google.blocked()}")
    if not 200 <= status < 300:
        raise SiteError(f"{rpcid} failed: HTTP {status}. {CHANGED_HINT}")
    try:
        return parse.rpc_payload(res["body"], rpcid)
    except parse.RpcError as exc:
        if exc.code in parse.AUTH_CODES:
            raise AuthRequired(f"Google refused {rpcid} (code {exc.code}); the session is missing or expired.") from None
        if exc.code in parse.RATE_CODES:
            raise RateLimited(f"Google rate-limited {rpcid} (RESOURCE_EXHAUSTED).", sent=False) from None
        raise SiteError(f"{exc} {CHANGED_HINT}") from None
    except ValueError:
        raise SiteError(f"{rpcid} answered something that isn't batchexecute JSON. {CHANGED_HINT}") from None


async def _build(page: "Page") -> str:
    build = await page.evaluate(BUILD_JS)
    if not build:
        raise SiteError(f"The page has no build label (WIZ_global_data.cfb2h). {CHANGED_HINT}")
    return build


async def _groups(page: "Page") -> list[dict]:
    return parse.groups(await rpc(page, RPC["labels"], [[[1, 3, await _build(page)]]]))


async def status(*, headed: bool = False) -> dict:
    """{signedIn, email, contactsApi: ok|error}: signed in when the page has an `at` token (Google serves none to a
    signed-out visitor); contactsApi "ok" when the labels RPC answers (else contactsApiError says why). headed is
    accepted for the daemon contract; data sites always run headless."""
    async with browser.site_page(POLICY) as page:
        try:
            await signed_in_page(page)
        except AuthRequired:
            return signed_out()
        try:
            email = await page.evaluate(google.EMAIL_JS)
        except Exception:
            email = None
        out = {"signedIn": True, "email": email, "contactsApi": "ok"}
        try:
            await _groups(page)
        except AuthRequired:
            return signed_out()
        except (SiteError, RateLimited) as exc:
            out.update(contactsApi="error", contactsApiError=str(exc))
        return out


async def labels(*, headed: bool = False) -> list[dict]:
    async with browser.site_page(POLICY) as page:
        await signed_in_page(page)
        return await _groups(page)


def find_label(wanted: str, group_list: list[dict]) -> dict | None:
    """A label by exact id, else by name (case-insensitive, whitespace trimmed and collapsed). None when nothing
    matches; UsageError (exit 2) when two or more labels share the name."""
    for g in group_list:
        if g["id"] == wanted.strip():
            return g
    hits = [g for g in group_list if parse.label_key(g["name"]) == parse.label_key(wanted)]
    if len(hits) > 1:
        raise UsageError(f"{wanted!r} matches {len(hits)} labels; pass the label id instead.",
                         hint="Candidates: " + ", ".join(f"{g['id']} ({g['name']})" for g in hits))
    return hits[0] if hits else None


def resolve_label(wanted: str, group_list: list[dict]) -> dict:
    found = find_label(wanted, group_list)
    if found is None:
        raise NotFound(f"No label {wanted!r}.", hint="See `webcli contacts labels`.")
    return found


async def paginate(page: "Page", rpcid: str, first: list, next_payload: Callable[[str], list],
                   unpack: Callable[[Any], tuple[list, str | None]], *, limit: int | None,
                   keep: Callable[[list], bool] = lambda row: True) -> list[list]:
    """Rows from every page of a list RPC, following page tokens; stops once `limit` rows are kept."""
    rows: list[list] = []
    seen_ids: set[str] = set()
    seen_tokens: set[str] = set()
    payload = first
    for _ in range(MAX_PAGES):
        batch, token = unpack(await rpc(page, rpcid, payload))
        for r in batch:
            rid = r[0] if isinstance(r[0], str) else None
            if rid in seen_ids or not keep(r):
                continue
            seen_ids.add(rid)
            rows.append(r)
            if limit and len(rows) >= limit:
                return rows
        if not token:
            return rows
        if token in seen_tokens:
            raise SiteError(f"{rpcid} handed back a page token it already used. {CHANGED_HINT}")
        seen_tokens.add(token)
        payload = next_payload(token)
    raise SiteError(f"{rpcid} kept paging past {MAX_PAGES} pages. {CHANGED_HINT}")


async def _my_contacts(page: "Page", *, limit: int | None = None, label_id: str | None = None) -> list[list]:
    keep = ((lambda r: label_id == "myContacts" or label_id in parse.contact(r)["labelIds"]) if label_id
            else (lambda r: True))
    return await paginate(page, RPC["contacts"], [None, None, None, 1, 0], lambda t: [t, None, None, 1, 0],
                          parse.contacts_page, limit=limit, keep=keep)


async def contacts(limit: int | None = None, label: str | None = None, *, headed: bool = False) -> dict:
    """{contacts: [contact, ...], label: {...} | None}: My Contacts, optionally one label's members."""
    async with browser.site_page(POLICY) as page:
        await signed_in_page(page)
        group_list = await _groups(page)
        names = parse.group_names(group_list)
        chosen = resolve_label(label, group_list) if label else None
        rows = await _my_contacts(page, limit=limit, label_id=chosen["id"] if chosen else None)
        return {"contacts": [parse.contact(r, names, source="contacts") for r in rows], "label": chosen}


async def other(limit: int | None = None, *, headed: bool = False) -> list[dict]:
    """The "Other contacts" list: people you've emailed who aren't saved contacts."""
    async with browser.site_page(POLICY) as page:
        await signed_in_page(page)
        rows = await paginate(page, RPC["other"], [None, None, 1, 0], lambda t: [t, None, 1, 0], parse.other_page,
                              limit=limit)
        return [parse.contact(r, source="other") for r in rows]


async def _search(page: "Page", query: str, names: dict[str, str]) -> list[dict]:
    payload = await rpc(page, RPC["search"], [[[1, 3, await _build(page)]], query, 1, 1, 1, 1])
    try:
        mine, others = parse.search_results(payload)
    except ValueError:
        raise SiteError(f"The search answer has an unexpected shape. {CHANGED_HINT}") from None
    return ([parse.contact(r, names, source="contacts") for r in mine]
            + [parse.contact(r, names, source="other") for r in others])


async def search(query: str, limit: int | None = None, *, headed: bool = False) -> list[dict]:
    """The app's own search (names, emails, phones, and more): My Contacts first, then Other contacts."""
    async with browser.site_page(POLICY) as page:
        await signed_in_page(page)
        hits = await _search(page, query, parse.group_names(await _groups(page)))
        return hits[:limit] if limit else hits


async def _get_rows(page: "Page", ids: list[str]) -> dict[str, list]:
    """{id: row} for the ids Google has data for (rptSGc, in chunks)."""
    found: dict[str, list] = {}
    for i in range(0, len(ids), GET_CHUNK):
        chunk = ids[i:i + GET_CHUNK]
        for r in parse.at(await rpc(page, RPC["get"], [chunk, None, 1, 0]), 0) or []:
            if isinstance(r, list) and r and r[0] in chunk and not parse.is_empty(r):
                found[r[0]] = r
    return found


async def get(ref: str, *, headed: bool = False) -> dict:
    """One contact in full, by id (rptSGc), or by an email address or phone number (search, then exact match)."""
    kind, value = parse.lookup_key(ref)
    async with browser.site_page(POLICY) as page:
        await signed_in_page(page)
        names = parse.group_names(await _groups(page))
        if kind == "id":
            row = (await _get_rows(page, [value])).get(value)
            if row is None:
                raise NotFound(f"No contact {value}.", hint="Ids come from `webcli contacts list`, `search`, or `other`.")
            return parse.contact(row, names)
        hits = [c for c in await _search(page, value, names) if parse.matches(c, kind, value)]
        if not hits:
            raise NotFound(f"No contact has the {kind} {value!r}.", hint=f"Try: webcli contacts search {value!r}")
        found = hits[0]
        return {**found, "otherMatches": [c["id"] for c in hits[1:]]} if len(hits) > 1 else found


# `label add|remove`: the only writes.

async def _select(page: "Page", names: dict[str, str], group_list: list[dict], *, from_label: str | None,
                  query: str | None, ids: list[str] | None) -> tuple[list[dict], list[dict], int]:
    """(matched My Contacts, failures [{id, error}], Other contacts skipped) for exactly one selector."""
    if from_label is not None:
        source = resolve_label(from_label, group_list)
        rows = await _my_contacts(page, label_id=source["id"])
        return [parse.contact(r, names, source="contacts") for r in rows], [], 0
    if query is not None:
        found = await _search(page, query, names)
        mine = [c for c in found if c["source"] == "contacts"]
        return mine, [], len(found) - len(mine)
    wanted = list(dict.fromkeys(ids or []))
    rows = await _get_rows(page, wanted)
    matched, failures = [], []
    for cid in wanted:
        if cid not in rows:
            failures.append({"id": cid, "error": "not found"})
            continue
        c = parse.contact(rows[cid], names)
        if c["source"] != "contacts":
            failures.append({"id": cid, "error": "an Other contact (not saved in My Contacts); labels need a saved "
                                                 "contact"})
        else:
            matched.append(c)
    return matched, failures, 0


async def _created_label(page: "Page", scope: WriteScope, name: str) -> dict:
    """Create the label (ouU0xd), then read it back by name: the create answer's shape isn't relied on."""
    scope.create_name = name  # the guard lets exactly one create under this name through, then clears it
    try:
        await rpc(page, WRITE_RPC["create_label"], parse.create_label_payload(name))
    finally:
        scope.create_name = None
    for _ in range(5):
        found = [g for g in await _groups(page) if parse.label_key(g["name"]) == parse.label_key(name)]
        if len(found) == 1:
            return found[0]
        await asyncio.sleep(1)
    raise SiteError(f"Created label {name!r} but couldn't read it back. {CHANGED_HINT}")


def _stop_code(exc: BaseException) -> int:
    return 6 if isinstance(exc, RateLimited) else 4 if isinstance(exc, AuthRequired) else 5


async def label_change(action: str, target: str, *, from_label: str | None = None, query: str | None = None,
                       ids: list[str] | None = None, confirm: bool = False, create_missing: bool = False,
                       headed: bool = False) -> dict:
    """Add contacts to a label, or remove them from it. A dry run unless confirm=True.

    Exactly one of from_label / query / ids picks the contacts (My Contacts only: Other contacts can't hold labels).
    Contacts already in the wanted state are skipped, so a rerun is safe. Changes go out MEMBERSHIP_BATCH contacts per
    call with a random BATCH_PAUSE between calls, then are read back: "accepted" counts the changes Google
    acknowledged and "changed" the ones that read back in the new state. A rate limit or lost session mid-run (or a
    read-back that fails) sets "stopped": {code, error}, with the result so far.
    """
    if action not in ("add", "remove"):
        raise UsageError(f"action must be add or remove, not {action!r}")
    if sum(x is not None for x in (from_label, query, ids)) != 1:
        raise UsageError("Pass exactly one selector: from_label, query, or ids.")
    scope = WriteScope() if confirm else None
    async with browser.site_page(write_policy(scope) if confirm else POLICY, send=confirm) as page:
        await signed_in_page(page)
        group_list = await _groups(page)
        names = parse.group_names(group_list)
        label = find_label(target, group_list)
        system_keys = {parse.label_key(x) for pair in SYSTEM_GROUP_NAMES.items() for x in pair}
        if label is None and parse.label_key(target) in system_keys:
            label = {"id": target, "name": target, "system": True}  # Starred: a system group GKlgB doesn't list
        if label and label["system"]:
            raise UsageError(f"{label['name']!r} is a system group; webcli contacts only changes your own labels.")
        if label is None and (action == "remove" or not create_missing):
            hint = "See `webcli contacts labels`." + ("" if action == "remove" else
                                                       " Or add --create-missing to create it.")
            raise NotFound(f"No label {target!r}.", hint=hint)
        matched, failures, skipped_other = await _select(page, names, group_list, from_label=from_label, query=query,
                                                         ids=ids)
        if not matched:
            raise NotFound("No saved contacts matched the selector."
                           + (f" ({len(failures)} ids failed.)" if failures else ""),
                           hint="Check the selector with `webcli contacts list --label`, `search`, or `get`.")
        want_in = action == "add"
        has = (lambda c: label["id"] in c["labelIds"]) if label else (lambda c: False)
        todo = [c for c in matched if has(c) != want_in]
        name = label["name"] if label else parse.clean_label_name(target)
        result = {
            "label": name, "labelId": label["id"] if label else None, "action": action,
            "matched": len(matched), "wouldChange": len(todo), "changed": 0,
            ("alreadyLabeled" if want_in else "alreadyAbsent"): len(matched) - len(todo),
            "failed": len(failures), "failures": failures, "dryRun": not confirm,
            "createLabel": label is None, "skippedOtherContacts": skipped_other,
            "contacts": [{"id": c["id"], "name": c["name"], "change": has(c) != want_in} for c in matched],
        }
        if not confirm or not todo:
            return result
        sent: list[str] = []
        try:
            if label is None:
                label = await _created_label(page, scope, name)
                result.update(label=label["name"], labelId=label["id"])
            scope.label_id, scope.action = label["id"], action
            scope.keys = frozenset(parse.contact_key(c["id"]) for c in todo)
            for i in range(0, len(todo), MEMBERSHIP_BATCH):
                if i:
                    await asyncio.sleep(random.uniform(*BATCH_PAUSE))
                batch = [c["id"] for c in todo[i:i + MEMBERSHIP_BATCH]]
                try:
                    await rpc(page, WRITE_RPC["membership"], parse.membership_payload(batch, label["id"], action))
                    sent += batch
                except SiteError as exc:
                    failures += [{"id": cid, "error": str(exc)} for cid in batch]
        except (RateLimited, AuthRequired) as exc:
            result["stopped"] = {"code": _stop_code(exc), "error": str(exc)}
        result["accepted"] = len(sent)
        if sent:  # read them back: `changed` counts only what Google now shows in the wanted state
            try:
                rows = await _get_rows(page, sent)
            except (RateLimited, AuthRequired, SiteError) as exc:
                result.setdefault("stopped", {"code": _stop_code(exc), "error": f"Google accepted {len(sent)} "
                                                                                 f"changes, but reading them back "
                                                                                 f"failed: {exc}"})
            else:
                for cid in sent:
                    row = rows.get(cid)
                    if row is not None and (label["id"] in parse.contact(row)["labelIds"]) == want_in:
                        result["changed"] += 1
                    else:
                        failures.append({"id": cid, "error": "Google accepted the change but the contact reads "
                                                             "back unchanged"})
        result.update(failed=len(failures), failures=failures)
        return result
