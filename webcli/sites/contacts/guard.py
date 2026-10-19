"""contacts.google.com's request policy for the core guard: endpoints.py's tables, plus the one write scope.

POLICY is read-only. write_policy(scope) is the policy of `label add|remove --confirm` alone: it also passes the label
writes that scope allows, payload checked field by field, and only while the command's page holds the core guard's
send permission (browser.site_page(..., send=True)). A dry run opens its page with POLICY and send off, so the guard
aborts any write the page tries.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlsplit

from webcli.core.guard import Guard, Policy, describe
from webcli.core.redact import REDACTED, tokenish
from webcli.sites import google
from webcli.sites.contacts.endpoints import (
    APP_HOST,
    BATCH_PATH,
    HOSTS,
    MAX_KEYS,
    READ_RPCIDS,
    RPC_PREFIX,
    WRITE_RPC,
    WRITE_RPCIDS,
)
from webcli.sites.contacts.parse import create_label_payload
from webcli.sites.gemini.parse import freq_entries

if TYPE_CHECKING:
    from playwright.async_api import Request

_HEX_KEY = re.compile(r"[0-9a-f]{1,20}")


@dataclass
class WriteScope:
    """The only writes a `label add|remove --confirm` run may send.

    label_id, action, keys: membership changes (ls96Jc) may only `action` (add|remove) the selected contacts (keys:
    their hex keys) to/from this one label. create_name: the label (ouU0xd) may be created once, empty, under exactly
    this name; the guard clears it when it lets the create through.
    """

    label_id: str | None = None
    action: str | None = None
    keys: frozenset[str] = frozenset()
    create_name: str | None = None


def write_allowed(rpcid: str, payload_json: object, scope: WriteScope | None) -> bool:
    """Whether one f.req entry is a write `scope` allows. Allowing a create uses it up (scope.create_name -> None),
    so a second create, even in the same batch, is refused."""
    if scope is None or rpcid not in WRITE_RPCIDS:
        return False
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError):
        return False
    if rpcid == WRITE_RPC["create_label"]:
        if scope.create_name is None or payload != create_label_payload(scope.create_name):
            return False
        scope.create_name = None
        return True
    if not (scope.label_id and scope.action in ("add", "remove") and isinstance(payload, list) and len(payload) == 8):
        return False
    keys, people, add, remove, profiles, delegate, *flags = payload
    group = [scope.label_id]
    direction = (add, remove) == ((group, None) if scope.action == "add" else (None, group))
    return (isinstance(keys, list) and 0 < len(keys) <= MAX_KEYS
            and all(isinstance(k, str) and _HEX_KEY.fullmatch(k) and k in scope.keys for k in keys)
            and people is None and profiles is None and delegate is None and flags == [1, 0] and direction)


def is_batch(url: str) -> bool:
    parts = urlsplit(url)
    return parts.hostname == APP_HOST and parts.path == BATCH_PATH


def rpcids_of(url: str, body: str | None) -> list[str] | None:
    """The rpcids a batchexecute POST runs, or None when the URL and the f.req body disagree. Compared with counts,
    so neither can smuggle in an id, or a second copy of one, the other leaves out."""
    labels = [v for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True) if k == "rpcids"]
    in_url = [r for r in (labels[0] if len(labels) == 1 else "").split(",") if r]
    in_body = [entry[0] for entry in freq_entries(body)]
    if not in_url or Counter(in_url) != Counter(in_body):
        return None
    return in_body


def request_allowed(method: str, url: str, body: str | None = None, *, scope: WriteScope | None = None) -> bool:
    """The policy. Fails closed: anything not listed in endpoints.py is aborted.

    - google.allowed: GET/HEAD on contacts.google.com and Google's sign-in and static hosts (no sign-out), and
      Google's cookie rotation. A GET to the RPC path is refused: RPCs are POSTs.
    - POST batchexecute on contacts.google.com only when the URL's rpcids and f.req agree (counted) and every entry
      is a read in READ_RPCIDS, or, with a scope (`label add|remove --confirm` only), a label write write_allowed()
      accepts for it.
    Everything else is aborted: contact edits, deletes, stars, merges, label renames and deletes, the page's prefs
    write on load, People API calls, telemetry, every unknown rpcid.

    Limits: Playwright routes only the first URL of a redirect chain. fetch.py's own requests use redirect: 'manual',
    so they never follow one. Checking a batchexecute body means holding it, `at` token included, in memory for this
    call; it is never stored, logged, or printed.
    """
    method = method.upper()
    parts = urlsplit(url)
    if parts.hostname == APP_HOST and parts.path.startswith(RPC_PREFIX) and method in ("GET", "HEAD"):
        return False
    if google.allowed(method, url, HOSTS):
        return True
    if method != "POST" or parts.scheme != "https" or parts.port is not None or not is_batch(url):
        return False
    if not rpcids_of(url, body):
        return False
    return all(entry[0] in READ_RPCIDS or (len(entry) > 1 and write_allowed(entry[0], entry[1], scope))
               for entry in freq_entries(body))


def _body(request: "Request") -> str | None:
    return request.post_data if request.method.upper() == "POST" and is_batch(request.url) else None


def _describe(request: "Request") -> str:
    """METHOD host/path, plus rpcids=... for a batchexecute (token-looking ones redacted)."""
    text = describe(request.method, request.url)
    if is_batch(request.url):
        try:
            ids = [entry[0] for entry in freq_entries(_body(request))]
        except Exception:
            ids = []
        text += f" rpcids={','.join(sorted({REDACTED if tokenish(r) else r for r in ids})) or '<unreadable>'}"
    return text


def write_policy(scope: WriteScope | None) -> Policy:
    """The policy for one command. A scope counts only while the page holds send (state.send): only `label
    add|remove --confirm` opens its page that way."""

    def allowed(request: "Request", state: Guard) -> bool:
        return request_allowed(request.method, request.url, _body(request), scope=scope if state.send else None)

    return Policy("contacts", allowed, _describe)


POLICY = write_policy(None)
