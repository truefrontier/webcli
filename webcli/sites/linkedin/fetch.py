"""Browser-side work for webcli linkedin (limsg's api/activity/boost/post on the shared core): login, status,
messaging, Recent activity, Boost stats, and the two writes.

Every API call is an in-page fetch with the profile's own cookies (COOKIE). The csrf-token header is the JSESSIONID
cookie, which LinkedIn's own scripts read too; it is read inside the page, so no cookie reaches Python. Messaging
reads open the messaging page under the read-only POLICY so its own queries reveal the current queryIds (the latest
are kept in ~/.webcli/sites/linkedin/query_ids.json). Activity reads the rendered Recent activity cards (UI), scrolling
the window. Boost and both writes run on https://www.linkedin.com/robots.txt (same origin, no app JS).

The writes (`send`, `post`) run only with confirm=True, and only then open their page with send on and
write_policy(scope): the guard lets through just that command's writes. Before the first write, the signed-in author
must be the expected one (author.py), and is read again right before the last write; a mismatch writes nothing.
Writes go out once each, never retried, never following a redirect.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import quote, urlsplit

from webcli.core import browser, config, guard
from webcli.core.errors import AuthRequired, NotFound, UsageError
from webcli.core.output import write_atomic
from webcli.sites.linkedin import author as authors
from webcli.sites.linkedin import boost, parse
from webcli.sites.linkedin.endpoints import (  # APP_URL and POLICY: what `python -m webcli.core.capture` reads
    ACTIVITY_URL,
    APP_URL,
    AUTH_WALL,
    CM_BASE,
    CM_HEADERS,
    CREATE_PATH,
    CREATE_URL,
    DEFAULT_QUERY_IDS,
    KIND_FEED,
    KIND_PATH,
    LANDING_URL,
    LOGIN_URL,
    ME_ACTIVITY,
    ME_URL,
    MESSAGING_GQL,
    MESSAGING_URL,
    REGISTER_PATH,
    REGISTER_URL,
    REST_CONVERSATIONS,
    SEND_PATH,
    SEND_URL,
    SNIFF_MS,
    THREAD_URL,
    VOYAGER_HEADERS,
)
from webcli.sites.linkedin.guard import POLICY, WriteScope, write_policy

if TYPE_CHECKING:
    from playwright.async_api import Page

EXPIRED = "LinkedIn session expired or not signed in."
CM_EXPIRED = "LinkedIn session expired or Campaign Manager needs sign-in."
LIST_HINT = "See `webcli linkedin messages list --compact`."
# status -1: no JSESSIONID (signed out); 0: no response; 302: a redirect (never followed: a sign-in bounce).
GET_JS = """
async ([url, headers]) => {
  const m = document.cookie.match(/(?:^|;\\s*)JSESSIONID="?([^";]+)/);
  if (!m) return {status: -1, text: ''};
  try {
    const res = await fetch(url, {method: 'GET', headers: Object.assign({'csrf-token': m[1]}, headers),
                                  credentials: 'include', redirect: 'manual', cache: 'no-store'});
    return {status: res.type === 'opaqueredirect' ? 302 : res.status, text: await res.text()};
  } catch (e) {
    return {status: 0, text: ''};
  }
}
"""
# One write, sent once. status 0: no response, or a redirect (redirect: 'error'): the outcome is unknown.
WRITE_JS = """
async ([url, method, headers, json, b64]) => {
  const m = document.cookie.match(/(?:^|;\\s*)JSESSIONID="?([^";]+)/);
  if (!m) return {status: -1, text: '', restliId: null};
  const opts = {method, headers: Object.assign({'csrf-token': m[1]}, headers), credentials: 'include',
                redirect: 'error', cache: 'no-store'};
  if (json !== null) opts.body = JSON.stringify(json);
  if (b64 !== null) opts.body = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  try {
    const res = await fetch(url, opts);
    return {status: res.status, restliId: res.headers.get('x-restli-id'), text: await res.text()};
  } catch (e) {
    return {status: 0, text: '', restliId: null};
  }
}
"""
VANITY_JS = r"""() => {
  const hrefs = [...document.querySelectorAll('a')].map(a => a.getAttribute('href') || a.href || '');
  for (const href of hrefs) {
    const m = href.match(/\/in\/([^/?#]+)\/recent-activity\//);
    if (m && m[1] && m[1] !== 'me') return m[1];
  }
  const path = location.pathname.match(/^\/in\/([^/]+)\//);
  if (path && path[1] !== 'me') return path[1];
  return '';
}"""
CARD_JS = r"""(feedType) => {
  const cards = [...document.querySelectorAll('div[id^="expanded"][id*="FeedType_"]')]
    .filter(el => el.id.includes(feedType));
  const seen = new Set();
  const out = [];
  for (const card of cards) {
    if (seen.has(card.id)) continue;
    seen.add(card.id);
    const lines = (card.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    const links = [...card.querySelectorAll('a')].map(a => ({
      href: (a.getAttribute('href') || '').split('?')[0],
      text: (a.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 180),
    })).filter(l => l.href);
    out.push({id: card.id, lines, links});
  }
  return out;
}"""
SCROLL_JS = "() => { const el = document.scrollingElement || document.body; window.scrollTo(0, el.scrollHeight); }"


def _json(text: str | None) -> Any:
    """Parsed in Python: JSON.parse in the page loses precision on LinkedIn's big ids."""
    try:
        return json.loads(text or "null")
    except ValueError:
        return None


def auth_wall(url: str) -> bool:
    return any(token in url.lower() for token in AUTH_WALL)


async def open_li(page: "Page", url: str) -> None:
    """Load a LinkedIn page. AuthRequired when LinkedIn sends the profile to sign in or a checkpoint."""
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    if auth_wall(page.url):
        raise AuthRequired(EXPIRED)


async def voyager_get(page: "Page", url: str, headers: dict[str, str] = VOYAGER_HEADERS) -> tuple[int, Any]:
    """GET `url` in the page with its cookies: (status, parsed JSON or None)."""
    result = await page.evaluate(GET_JS, [url, headers])
    return int(result.get("status") or 0), _json(result.get("text"))


async def me(page: "Page") -> dict:
    """The signed-in author from GET /voyager/api/me: {name, publicIdentifier, profileUrn, memberUrn}. AuthRequired
    unless it pins down one profile."""
    status, data = await voyager_get(page, ME_URL)
    found = parse.parse_me(data) if status == 200 else None
    if found is None:
        raise AuthRequired(f"{EXPIRED} (GET /voyager/api/me: {'no session cookie' if status == -1 else status})")
    return found


def _check(status: int, rows: list, where: str, what: str) -> None:
    if status in (-1, 302, 401, 403):
        raise AuthRequired(f"{EXPIRED} (Voyager {what}: HTTP {status})")
    if status != 200 and not rows:
        raise RuntimeError(f"Voyager {what} failed (HTTP {status}) for {where}.")


# --- Messaging (READ-ONLY) ----------------------------------------------------------------------------------------

def _ids_path():
    return config.site_dir("linkedin") / "query_ids.json"


def cached_query_ids() -> dict[str, str]:
    """The latest queryIds the messaging page itself used, by name ({} until a messaging page has been read)."""
    try:
        saved = json.loads(_ids_path().read_text())
    except (OSError, ValueError):
        saved = {}
    return {k: v for k, v in saved.items() if isinstance(v, str)} if isinstance(saved, dict) else {}


def query_id_for(ids: dict[str, str], *names: str) -> str:
    """The first of `names` the page used, else limsg's default for the first name."""
    return next((ids[name] for name in names if ids.get(name)), DEFAULT_QUERY_IDS[names[0]])


async def sniff(page: "Page", url: str) -> dict[str, str]:
    """Load a messaging page and read the queryIds its own requests carry (URLs only); the latest are kept."""
    seen: dict[str, str] = {}

    def on_request(request) -> None:
        if qid := parse.query_id(request.url):
            seen[qid.split(".", 1)[0]] = qid

    page.on("request", on_request)
    try:
        await open_li(page, url)
        await page.wait_for_timeout(SNIFF_MS)
    finally:
        page.remove_listener("request", on_request)
    ids = cached_query_ids()
    if seen and any(ids.get(k) != v for k, v in seen.items()):
        ids.update(seen)
        write_atomic(_ids_path(), json.dumps(ids, indent=1) + "\n")
    return ids


async def _conversations(page: "Page", mailbox: str, ids: dict[str, str], limit: int) -> list[dict]:
    """The newest `limit` threads: Messaging GraphQL, else the REST list."""
    rows: list[dict] = []
    status, where = 0, ""
    for url in (f"{MESSAGING_GQL}?queryId={query_id_for(ids, 'messengerConversations')}"
                f"&variables=(mailboxUrn:{quote(mailbox, safe='')})",
                f"{REST_CONVERSATIONS}?keyVersion=LEGACY_INBOX&q=search&count={max(limit, 20)}"):
        status, data = await voyager_get(page, url)
        where = url.split("?")[0]
        if status == 200 and (rows := parse.conversations(data)):
            break
    _check(status, rows, where, "list")
    return rows[:limit]


async def conversations(limit: int = 20, *, headed: bool = False) -> list[dict]:
    """Recent DM threads {id, entityUrn, peer, preview, time, unread}, newest first. headed is accepted for the
    daemon contract; data sites always run headless."""
    async with browser.site_page(POLICY) as page:
        ids = await sniff(page, MESSAGING_URL)
        return await _conversations(page, (await me(page))["profileUrn"], ids, limit)


async def messages(target: str, limit: int = 20, *, headed: bool = False) -> list[dict]:
    """The last `limit` messages of one thread {id, time, from, text, senderUrn}, oldest first. `target`: a peer name
    (substring), a thread id (2-...), or an entityUrn. Opening the thread is read-only: the guard aborts the page's
    own mark-as-read."""
    async with browser.site_page(POLICY) as page:
        ids = await sniff(page, MESSAGING_URL)
        mailbox = (await me(page))["profileUrn"]
        found = parse.resolve_thread(await _conversations(page, mailbox, ids, max(limit, 50)), target)
        if found is None:
            raw = target.strip()
            if not raw.startswith(("urn:li:", "2-")):
                raise NotFound(f"No thread matching {target!r}.", hint=LIST_HINT)
            found = {"id": raw, "entityUrn": raw if raw.startswith("urn:li:") else "", "peer": ""}
        thread, conv = parse.normalize_conversation_urn(found.get("entityUrn") or found["id"])
        ids = await sniff(page, THREAD_URL.format(thread=quote(thread, safe="")))
        qid = query_id_for(ids, "messengerMessages", "messengerMessagesByConversation")
        rows: list[dict] = []
        status, where = 0, ""
        for variables in (f"(conversationUrn:{quote(conv, safe='')})",
                          f"(conversationUrn:{quote(conv, safe='')},count:{max(limit, 20)})",
                          f"(conversationUrn:{quote('urn:li:fs_conversation:' + thread, safe='')})"):
            status, data = await voyager_get(page, f"{MESSAGING_GQL}?queryId={qid}&variables={variables}")
            where = MESSAGING_GQL
            if status == 200 and (rows := parse.parse_messages_payload(data, self_urn=mailbox)):
                break
        if not rows:
            url = f"{REST_CONVERSATIONS}/{quote(thread, safe='')}/events?keyVersion=LEGACY_INBOX&q=events&count={max(limit, 20)}"
            status, data = await voyager_get(page, url)
            where = url.split("?")[0]
            if status == 200:
                rows = parse.parse_messages_payload(data, self_urn=mailbox)
        _check(status, rows, where, "messages")
    hint = (found.get("peer") or "").split(",")[0].strip()
    for row in rows:
        if hint and row.get("from") in ("", "peer"):
            row["from"] = hint
    return rows[-limit:]


# --- Writes -------------------------------------------------------------------------------------------------------

async def _write(page: "Page", url: str, *, method: str = "POST", json_body: Any = None, data: bytes | None = None,
                 headers: dict[str, str] | None = None) -> tuple[int, Any, str | None]:
    """One write, sent once: (status, JSON or None, x-restli-id). status 0: no response, outcome unknown."""
    hdrs = {**VOYAGER_HEADERS, "content-type": "application/json"} if json_body is not None else {}
    hdrs.update(headers or {})
    payload = base64.b64encode(data).decode() if data is not None else None
    try:
        result = await page.evaluate(WRITE_JS, [url, method, hdrs, json_body, payload])
    except Exception:  # the browser died mid-request: no response, outcome unknown
        return 0, None, None
    return int(result.get("status") or 0), _json(result.get("text")), result.get("restliId")


def _blocked(path: str) -> bool:
    """The guard aborted a request to `path` this command (so it never left Chrome)."""
    return any(path in line for line in guard.state.blocked)


async def _recheck(page: "Page", signed: dict, expect: str, what: str) -> None:
    """Re-read /me right before the last write; refuse unless it is still the same, expected account. (A session
    swap after this read and before the write would still go out as the new account.)"""
    try:
        now = await me(page)
    except Exception as exc:
        raise AuthRequired(f"REFUSED: could not re-check the signed-in account ({exc}). Nothing was {what}.") from None
    if not authors.same_account(now, signed) or not authors.matches(now, expect):
        raise AuthRequired(f"REFUSED: the signed-in account changed from {authors.who(signed)} to {authors.who(now)} "
                           f"before the write (expected {expect}). Nothing was {what}.")


async def send(target: str, text: str, expect: str, *, confirm: bool = False, search_limit: int = 50,
               headed: bool = False) -> dict:
    """A DM to one existing thread. Without confirm, a dry run: it resolves the thread and reads the author, and
    sends nothing. With confirm, it sends only when the author is `expect` (else {sent: false}, nothing sent)."""
    try:
        text = parse.load_message(text)
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    scope = WriteScope("message", text) if confirm else None
    async with browser.site_page(write_policy(scope), send=confirm) as page:
        await open_li(page, LANDING_URL)
        signed = await me(page)
        threads = await _conversations(page, signed["profileUrn"], cached_query_ids(), search_limit)
        found = parse.resolve_thread(threads, target)
        if found is None:
            raise NotFound(f"No thread matching {target!r} in the newest {search_limit}.", hint=LIST_HINT)
        out = {"dryRun": not confirm, "to": found.get("peer"), "threadId": found.get("id"),
               "entityUrn": found.get("entityUrn"), "text": text, "author": signed, "expectedAuthor": expect,
               "authorMatches": authors.matches(signed, expect)}
        if not confirm:
            return out
        if not out["authorMatches"]:  # wrong account: refused before any write
            return {**out, "sent": False}
        if not found.get("entityUrn"):
            raise RuntimeError("That thread has no conversation urn, so nothing was sent.")
        scope.armed, scope.conversation, scope.mailbox = True, found["entityUrn"], signed["profileUrn"]
        await _recheck(page, signed, expect, "sent")
        origin = str(uuid.uuid4())
        body = {"message": {"body": {"attributes": [], "text": text}, "renderContentUnions": [], "originToken": origin,
                            "conversationUrn": scope.conversation},
                "mailboxUrn": scope.mailbox, "trackingId": uuid.uuid4().hex[:16], "dedupeByClientGeneratedToken": False}
        status, _, _ = await _write(page, SEND_URL, json_body=body)
    if status == -1:
        raise AuthRequired(f"{EXPIRED} Nothing was sent.")
    if status == 0 and _blocked(SEND_PATH):
        raise RuntimeError("The guard blocked the message (it didn't match the confirmed one). Nothing was sent.")
    if 400 <= status < 500:
        raise RuntimeError(f"LinkedIn refused the message (HTTP {status}). Nothing was sent.")
    if not 200 <= status < 300:
        raise RuntimeError(f"Send got {'no response' if status == 0 else f'HTTP {status}'}. The message may have been "
                           f"sent: check `webcli linkedin messages history {found.get('id')}` before retrying.")
    return {**out, "sent": True, "status": status, "originToken": origin}


async def _publish(page: "Page", text: str, image: dict, signed: dict, expect: str, scope: WriteScope) -> dict:
    """Register -> upload -> author re-check -> create. Each write runs once; a failure stops before the next."""
    status, data, _ = await _write(page, REGISTER_URL, json_body={
        "mediaUploadType": "IMAGE_SHARING", "fileSize": image["bytes"], "filename": image["filename"]})
    if not 200 <= status < 300:
        blocked = " The guard blocked it." if status == 0 and _blocked(REGISTER_PATH) else ""
        raise RuntimeError(f"Image register failed (HTTP {status}).{blocked} Nothing was posted.")
    asset, upload_url, extra = parse.parse_register(data)
    parse.check_upload_url(upload_url)
    scope.upload_url, scope.asset = upload_url, asset
    status, _, _ = await _write(page, upload_url, method="PUT", data=image["data"],
                                headers={"content-type": image["type"], **extra})
    if not 200 <= status < 300:
        raise RuntimeError(f"Image upload failed (HTTP {status}). Nothing was posted.")
    # The create carries no author, so this is the last chance to catch a different account.
    await _recheck(page, signed, expect, "posted")
    status, data, restli_id = await _write(page, CREATE_URL, json_body=parse.share_body(text, asset))
    if status == 0 and _blocked(CREATE_PATH):
        raise RuntimeError("The guard blocked the post (it didn't match the confirmed one). Nothing was posted.")
    if 400 <= status < 500 or status == -1:
        raise RuntimeError(f"LinkedIn rejected the post (HTTP {status}). Nothing was posted.")
    if not 200 <= status < 300:
        raise RuntimeError(f"Create post got {'no response' if status == 0 else f'HTTP {status}'}. The post may be "
                           "live; check your profile before retrying.")
    urn = parse.parse_created_urn(data, restli_id)
    out: dict[str, Any] = {"urn": urn, "url": parse.post_url(urn) if urn else None, "assetUrn": asset}
    if not urn:  # 2xx means LinkedIn accepted it: reporting a failure would invite a second post
        out["warning"] = (f"LinkedIn accepted the post (HTTP {status}) but the response had no post urn. Check your "
                          "profile; do NOT re-run or it will post twice.")
    return out


async def post(text: str, image_path: str, expect: str, *, confirm: bool = False, headed: bool = False) -> dict:
    """Text + one image to the feed. Without confirm, a dry run: one read (GET /me) and the preview; nothing is
    uploaded or posted. With confirm, it publishes only when the author is `expect` (else {posted: false})."""
    try:
        text = parse.load_text(text, None)
        image = parse.load_image(image_path)
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    scope = WriteScope("post", text, size=image["bytes"], filename=image["filename"]) if confirm else None
    async with browser.site_page(write_policy(scope), send=confirm) as page:
        await open_li(page, LANDING_URL)
        signed = await me(page)
        out = {"dryRun": not confirm, "loggedIn": True, "author": signed, "expectedAuthor": expect,
               "authorMatches": authors.matches(signed, expect), "text": text, "textLength": parse.utf16_len(text),
               "image": parse.image_summary(image)}
        if not confirm:
            return out
        if not out["authorMatches"]:  # wrong account: refused before any upload or write
            return {**out, "posted": False}
        scope.armed = True
        result = await _publish(page, text, image, signed, expect, scope)
    out["image"]["assetUrn"] = result.pop("assetUrn")
    return {**out, "posted": True, **result}


# --- Recent activity (READ-ONLY) ----------------------------------------------------------------------------------

async def _vanity(page: "Page") -> str:
    """The signed-in profile's vanity name: /in/me/recent-activity/ only redirects the shell, so it is read from the
    page's links."""
    await open_li(page, ME_ACTIVITY)
    await page.wait_for_timeout(1500)
    if auth_wall(page.url):
        raise AuthRequired(EXPIRED)
    vanity = str(await page.evaluate(VANITY_JS) or "").strip()
    if not vanity or vanity == "me":
        raise RuntimeError("Could not find the signed-in profile's activity URL; LinkedIn's page may have changed.")
    return vanity


async def _scroll_for(page: "Page", feed_type: str, limit: int) -> list[dict]:
    cards = await page.evaluate(CARD_JS, feed_type)
    stable = 0
    while len(cards) < limit and stable < 2:
        before = len(cards)
        await page.mouse.wheel(0, 3200)
        await page.evaluate(SCROLL_JS)
        await page.wait_for_timeout(1200)
        cards = await page.evaluate(CARD_JS, feed_type)
        stable = stable + 1 if len(cards) <= before else 0
    return [card for card in cards if isinstance(card, dict)]


async def _collect(page: "Page", vanity: str, kind: str, *, limit: int, since: datetime | None) -> list[dict]:
    await open_li(page, ACTIVITY_URL.format(vanity=vanity, path=KIND_PATH[kind]))
    await page.wait_for_timeout(2000)
    if auth_wall(page.url):
        raise AuthRequired(EXPIRED)
    now = datetime.now(timezone.utc)
    cards = await _scroll_for(page, KIND_FEED[kind], max(limit, 40) if since else limit)
    rows = [parse.normalize_card(kind, card, now=now) for card in cards]
    rows = [r for r in rows if r.get("text") or r.get("reaction") or r.get("targetTitle") or r.get("url")]
    return parse.apply_since(rows, since)[:limit]


async def activity(kind: str, limit: int = 20, since: str | None = None, *, headed: bool = False) -> list[dict]:
    """Your recent comments, reactions, or posts (the rendered Recent activity cards), newest first."""
    if kind not in KIND_PATH:
        raise UsageError(f"Unknown activity kind {kind!r}.")
    async with browser.site_page(POLICY) as page:
        vanity = await _vanity(page)
        return await _collect(page, vanity, kind, limit=limit, since=parse.parse_since(since))


async def export(limit: int = 20, since: str | None = None, *, headed: bool = False) -> dict[str, list[dict]]:
    """{comments, reactions, posts}, each as `activity` reads it, in one page."""
    async with browser.site_page(POLICY) as page:
        vanity = await _vanity(page)
        return {kind: await _collect(page, vanity, kind, limit=limit, since=parse.parse_since(since))
                for kind in KIND_PATH}


# --- Boost (READ-ONLY: GET only, on robots.txt) -------------------------------------------------------------------

async def _cm_get(page: "Page", path: str) -> tuple[int, Any]:
    """GET {CM_BASE}{path}. Refuses anything but a relative read path (no ?action=)."""
    if not boost.read_path(path):
        raise RuntimeError(f"Refusing a non-read Campaign Manager call: {path}")
    status, data = await voyager_get(page, CM_BASE + path, CM_HEADERS)
    if status in (-1, 302, 401):  # a redirect from this API is a sign-in or checkpoint bounce
        raise AuthRequired(CM_EXPIRED)
    return status, data


async def _accounts(page: "Page") -> list[dict]:
    status, data = await _cm_get(page, "campaignManagerAccounts?q=memberBoostAccounts")
    if status == 403:
        raise AuthRequired(CM_EXPIRED)
    if status != 200:
        raise RuntimeError(f"campaignManagerAccounts HTTP {status}")
    return boost.elements(data)


async def _campaigns(page: "Page", account_id: int) -> list[dict]:
    status, data = await _cm_get(page, f"campaignManagerReportingCampaigns?accountId={account_id}"
                                       "&q=accountAndCampaignGroups")
    if status == 403:
        raise AuthRequired(CM_EXPIRED)
    if status != 200:
        raise RuntimeError(f"campaignManagerReportingCampaigns HTTP {status} (account {account_id})")
    return boost.elements(data)


async def _creatives(page: "Page", account_id: int, campaign_id: int) -> tuple[list[dict], list[str]]:
    status, data = await _cm_get(page, f"campaignManagerReportingCreatives?accountId={account_id}"
                                       f"&q=cursorBasedCriteria&campaignIds=List({campaign_id})&count=10")
    if status != 200:
        return [], [f"creatives HTTP {status}"]
    return boost.elements(data), []


async def _record(page: "Page", campaign: dict, account: dict | None, creatives: list[dict], warnings: list[str], *,
                  since: datetime | None, until: datetime | None, now: datetime) -> dict:
    window = boost.analytics_window(campaign, since=since, until=until, now=now)
    analytics = None
    if window is not None:
        analytics = await _cm_get(page, boost.analytics_path([boost._trailing_int(campaign.get("id"))], *window))
    return boost.boost_record(campaign, creatives, analytics, account=account, date_range=window, now=now,
                              warnings=warnings)


async def boosts(since: str | None = None, until: str | None = None, account: int | None = None, *,
                 headed: bool = False) -> list[dict]:
    """One record per boosted campaign across your personal Boost ad accounts, newest start first."""
    since_dt, until_dt = parse.parse_since(since), parse.parse_since(until, flag="--until")
    async with browser.site_page(POLICY) as page:
        await open_li(page, LANDING_URL)
        now = datetime.now(timezone.utc)
        accounts = await _accounts(page)
        if account is not None:
            accounts = [a for a in accounts if boost._trailing_int(a.get("id")) == account]
            if not accounts:
                raise NotFound(f"No Boost ad account {account}.")
        found = [(campaign, acct) for acct in accounts
                 for campaign in await _campaigns(page, boost._trailing_int(acct.get("id")))
                 if boost.in_list_window(campaign, since=since_dt, until=until_dt)]
        found.sort(key=lambda item: boost.latest_first(item[0]), reverse=True)
        out = []
        for campaign, acct in found:
            creatives, warnings = await _creatives(page, boost._trailing_int(acct.get("id")),
                                                   boost._trailing_int(campaign.get("id")))
            out.append(await _record(page, campaign, acct, creatives, warnings, since=since_dt, until=until_dt,
                                     now=now))
        return out


async def boost_stats(target: str, since: str | None = None, until: str | None = None, *,
                      headed: bool = False) -> dict:
    """One record for a share/activity urn (or a feed URL holding one) or a campaign id."""
    kind, value = boost.parse_target(target)
    since_dt, until_dt = parse.parse_since(since), parse.parse_since(until, flag="--until")
    async with browser.site_page(POLICY) as page:
        await open_li(page, LANDING_URL)
        now = datetime.now(timezone.utc)
        by_id = {boost._trailing_int(a.get("id")): a for a in await _accounts(page)}
        if kind == "campaign":
            status, detail = await _cm_get(page, f"campaignManagerCampaigns/{value}")
            if status in (403, 404):
                raise NotFound(f"No Boost campaign {value} (HTTP {status}).")
            if status != 200 or not isinstance(detail, dict):
                raise RuntimeError(f"campaignManagerCampaigns HTTP {status}")
            acct_id = boost._trailing_int(detail.get("accountId"))
            creatives, warnings = await _creatives(page, acct_id, value)
            return await _record(page, detail, by_id.get(acct_id), creatives, warnings, since=since_dt,
                                 until=until_dt, now=now)
        matches = []
        for acct_id, acct in by_id.items():
            for campaign in await _campaigns(page, acct_id):
                creatives, warnings = await _creatives(page, acct_id, boost._trailing_int(campaign.get("id")))
                if value in boost.content_urns(creatives):
                    matches.append((campaign, acct, creatives, warnings))
        if not matches:
            hint = (" Boost previews reference the share urn; try urn:li:share:... or the campaign id (`webcli "
                    "linkedin boost list --compact`)." if value.startswith("urn:li:activity:") else "")
            raise NotFound(f"No Boost campaign for {value}.{hint}")
        matches.sort(key=lambda item: boost.latest_first(item[0]), reverse=True)
        campaign, acct, creatives, warnings = matches[0]
        status, detail = await _cm_get(page, f"campaignManagerCampaigns/{boost._trailing_int(campaign.get('id'))}")
        if status == 200 and isinstance(detail, dict):
            campaign = {**campaign, **detail}
        else:
            warnings = warnings + [f"campaign detail HTTP {status}"]
        record = await _record(page, campaign, acct, creatives, warnings, since=since_dt, until=until_dt, now=now)
    if len(matches) > 1:
        record["otherCampaignIds"] = [boost._trailing_int(m[0].get("id")) for m in matches[1:]]
    return record


# --- Session ------------------------------------------------------------------------------------------------------

async def status(*, headed: bool = False) -> dict:
    """{signedIn, name, publicIdentifier, profileUrn, memberUrn} from GET /voyager/api/me."""
    async with browser.site_page(POLICY) as page:
        await open_li(page, LANDING_URL)
        try:
            signed = await me(page)
        except AuthRequired:
            return {"signedIn": False}
        return {"signedIn": True, **signed}


async def login(timeout: int, on_state: Callable[[str], None] = lambda _state: None) -> dict:
    """A visible Chrome on LinkedIn messaging with no guard (sign-in may cross Google, Apple, or a checkpoint); wait
    until a tab is signed in. Needs a person; only `identity login` calls it."""
    on_state("signing in")
    async with browser.open_page(None, mode="visible") as page:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await asyncio.sleep(2)
            tabs = [p for p in page.context.pages if not p.is_closed()]
            if not tabs:
                raise AuthRequired("The Chrome window closed before sign-in finished.")
            for tab in tabs:  # sign-in may finish in another tab
                if not (urlsplit(tab.url).hostname or "").endswith("linkedin.com") or auth_wall(tab.url):
                    continue
                try:
                    signed = await me(tab)
                except Exception:  # not signed in yet, or mid-navigation
                    continue
                return {"signedIn": True, "name": f"{signed['name']} ({signed['publicIdentifier']})"}
        raise AuthRequired(f"Timed out after {timeout}s waiting for LinkedIn sign-in.")
