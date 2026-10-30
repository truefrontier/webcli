"""Search Console's request policies for the core guard (endpoints.py's tables). Fail-closed; three kinds:

  READ          every read command: GET/HEAD on search.google.com (plus Google sign-in and static hosts, via
                google.allowed), and a batchexecute POST only when it is one rpcid from endpoints.RPC. RVtklb (start an
                inspection) only in index-status mode 2, never mode 1 (a live test).
  submit_policy(property, sitemap)
                `sitemaps submit --yes`: READ plus CyYwI once, and only with exactly that [property, sitemap] payload.
  REQUEST_INDEXING (request_indexing_policy())
                `inspect --request-indexing --yes`: Search Console's own URL Inspection page drives the flow, so its
                own read batches (endpoints.INSPECT_PAGE_RPCIDS, RVtklb in mode 2 or 1: the live test REQUEST
                INDEXING runs first) plus Fj3Owf once. Built fresh per command, so the budget is per command.

Everything else (adding or removing properties and users, removals, validation, settings, the page's load-time
writes such as SM7Bqb's recent-properties list) is in no policy and is aborted.

Checking a batchexecute body means holding it, `at` token and (for Fj3Owf) the page's attestation included, in
memory for this one check; it is never stored, logged, or printed. describe() names rpcids only.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlsplit

from webcli.core.guard import Guard, Policy
from webcli.core.redact import REDACTED, redact_url, tokenish
from webcli.sites import google
from webcli.sites.gsc.endpoints import BATCH_PATH, HOSTS, INSPECT_LIVE_MODE, INSPECT_MODE_INDEX, INSPECT_PAGE_RPCIDS, \
    RPC, RPC_PREFIX, WRITE_RPC
from webcli.sites.gsc.parse import freq_entries

if TYPE_CHECKING:
    from playwright.async_api import Request

READ_RPCIDS = frozenset(RPC.values())
SUBMIT = WRITE_RPC["sitemap_submit"]
REQUEST = WRITE_RPC["request_indexing"]
START = RPC["inspect_start"]


def entries_of(url: str, body: str | None) -> list[list] | None:
    """The f.req entries a batchexecute POST runs, or None when the URL's `rpcids=` label and the body disagree (the
    server runs what f.req says; checking both means neither can smuggle in an id the other leaves out)."""
    labels = [v for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True) if k == "rpcids"]
    in_url = [r for r in (labels[0] if len(labels) == 1 else "").split(",") if r]
    entries = freq_entries(body)
    if not in_url or not entries or sorted(set(in_url)) != sorted({e[0] for e in entries}):
        return None
    return entries


def payload(entry: list):
    try:
        return json.loads(entry[1]) if len(entry) > 1 and isinstance(entry[1], str) else None
    except ValueError:
        return None


def inspect_mode(entry: list) -> int | None:
    """RVtklb's mode field ([url, null, 4, MODE, prop]): 2 indexed data, 1 a live test."""
    p = payload(entry)
    return p[3] if isinstance(p, list) and len(p) == 5 and isinstance(p[3], int) else None


def _batch(method: str, url: str) -> bool | None:
    """True for a well-formed batchexecute POST on Search Console, False for anything else under the RPC prefix
    (refused), None when the URL isn't under the RPC prefix (google.allowed decides)."""
    parts = urlsplit(url)
    if (parts.hostname or "") not in HOSTS or not parts.path.startswith(RPC_PREFIX):
        return None
    return (method.upper() == "POST" and parts.path == BATCH_PATH and parts.scheme == "https"
            and parts.port in (None, 443) and "@" not in parts.netloc)


def read_allowed(method: str, url: str, body: str | None = None) -> bool:
    """READ: one rpcid per call, from endpoints.RPC; an inspection only in index-status mode."""
    batch = _batch(method, url)
    if batch is None:
        return google.allowed(method.upper(), url, HOSTS)
    if not batch:
        return False
    entries = entries_of(url, body)
    if not entries or len(entries) != 1 or entries[0][0] not in READ_RPCIDS:
        return False
    return entries[0][0] != START or inspect_mode(entries[0]) == INSPECT_MODE_INDEX


def describe(method: str, url: str, body: str | None = None) -> str:
    """METHOD host/path [rpcids=...] for messages: no query string (it carries the session id), no payloads."""
    parts = urlsplit(url)
    text = f"{method} {parts.hostname}{redact_url(parts.path)}"
    if parts.path == BATCH_PATH:
        ids = sorted({REDACTED if tokenish(e[0]) else e[0] for e in freq_entries(body)})
        text += f" rpcids={','.join(ids)}"
    return text


def _body(request: "Request") -> str | None:
    return request.post_data if urlsplit(request.url).path == BATCH_PATH else None


def _describe_request(request: "Request") -> str:
    try:
        body = _body(request)
    except Exception:
        body = None
    return describe(request.method, request.url, body)


def _policy(check) -> Policy:
    def allowed(request: "Request", state: Guard) -> bool:
        return bool(check(request.method, request.url, _body(request)))

    return Policy("gsc", allowed, _describe_request)


class Budget:
    """How many times each write rpcid may still go out on this command's page; `sent` counts what went."""

    def __init__(self, budget: dict[str, int]) -> None:
        self.left = {k: int(v) for k, v in budget.items() if k in WRITE_RPC.values() and int(v) > 0}
        self.sent: dict[str, int] = {}

    def spend(self, rpcid: str) -> bool:
        if self.left.get(rpcid, 0) <= 0:
            return False
        self.left[rpcid] -= 1
        self.sent[rpcid] = self.sent.get(rpcid, 0) + 1
        return True


def submit_check(prop: str, sitemap: str, budget: Budget):
    """READ, plus CyYwI once with exactly [prop, sitemap]."""

    def check(method: str, url: str, body: str | None) -> bool:
        if read_allowed(method, url, body):
            return True
        if not _batch(method, url):
            return False
        entries = entries_of(url, body)
        if not entries or len(entries) != 1 or entries[0][0] != SUBMIT or payload(entries[0]) != [prop, sitemap]:
            return False
        return budget.spend(SUBMIT)

    return check


def request_indexing_check(budget: Budget):
    """Search Console's own inspection page: its read batches (INSPECT_PAGE_RPCIDS and READ), RVtklb in mode 2 or 1
    (the live test), and Fj3Owf alone in its call, once."""
    page_ids = INSPECT_PAGE_RPCIDS | READ_RPCIDS

    def check(method: str, url: str, body: str | None) -> bool:
        batch = _batch(method, url)
        if batch is None:
            return google.allowed(method.upper(), url, HOSTS)
        if not batch:
            return False
        entries = entries_of(url, body)
        if not entries:
            return False
        ids = [e[0] for e in entries]
        if REQUEST in ids:
            return len(entries) == 1 and budget.spend(REQUEST)
        for e in entries:
            if e[0] not in page_ids:
                return False
            if e[0] == START and inspect_mode(e) not in (INSPECT_MODE_INDEX, INSPECT_LIVE_MODE):
                return False
        return True

    return check


def submit_policy(prop: str, sitemap: str, budget: Budget) -> Policy:
    return _policy(submit_check(prop, sitemap, budget))


def request_indexing_policy(budget: Budget) -> Policy:
    return _policy(request_indexing_check(budget))


READ = _policy(read_allowed)
POLICY = READ
