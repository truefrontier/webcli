"""NotebookLM's request policies for the core guard: endpoints.py's tables. Three, one per kind of command:

  READ      every read command (status, notebooks, sources, source get, notes, artifacts, download)
  ASK       `ask`: READ plus the one chat endpoint (GenerateFreeFormStreamed)
  GENERATE  `generate --confirm`: READ plus CreateArtifact (R7cb6c)

A command gets exactly one of them for its page, so a read can never reach the chat or the create RPC.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlsplit

from webcli.core.guard import Guard, Policy
from webcli.core.redact import REDACTED, redact_url, tokenish
from webcli.sites import google
from webcli.sites.notebooklm.endpoints import APP_HOSTS, BATCH_PATH, GENERATE_RPCIDS, MEDIA_HOSTS, QUERY_PATH, \
    READ_RPCIDS, RPC_PREFIX
from webcli.sites.notebooklm.parse import freq_rpcids

if TYPE_CHECKING:
    from playwright.async_api import Request


def rpcids_of(url: str, body: str | None) -> list[str] | None:
    """The rpcids a batchexecute POST runs, or None when the URL's `rpcids=` label and the f.req body disagree (the
    server runs what f.req says; checking both means neither can smuggle in an id the other leaves out)."""
    labels = [v for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True) if k == "rpcids"]
    in_url = [r for r in (labels[0] if len(labels) == 1 else "").split(",") if r]
    in_body = freq_rpcids(body)
    if not in_url or sorted(set(in_url)) != sorted(set(in_body)):
        return None
    return in_body


def request_allowed(method: str, url: str, body: str | None = None, *, ask: bool = False,
                    generate: bool = False) -> bool:
    """The policy. Fails closed: anything not listed in endpoints.py is aborted.

    - google.allowed: GET/HEAD on NotebookLM's hosts and Google's sign-in and static hosts (no sign-out), and
      Google's cookie rotation. A GET under the RPC prefix is refused (RPCs are POSTs).
    - POST batchexecute on a NotebookLM host when its one rpcid is in READ_RPCIDS (GENERATE_RPCIDS with generate).
    - POST GenerateFreeFormStreamed only with ask.
    Everything else is aborted: notebook / source / note / artifact writes, sharing, settings, research, telemetry,
    and GetOrCreateAccount (which can create).

    Limits: Playwright routes only the first URL of a redirect chain, so a redirect Google's server chooses for an
    allowed request is followed unchecked. Checking a batchexecute body means holding it, `at` token included, in
    memory for this call; it is never stored, logged, or printed.
    """
    method = method.upper()
    parts = urlsplit(url)
    host, path = parts.hostname or "", parts.path
    on_app = host in APP_HOSTS
    if on_app and path.startswith(RPC_PREFIX):
        if method != "POST" or parts.scheme != "https" or parts.port not in (None, 443) or "@" in parts.netloc:
            return False
        if path == QUERY_PATH:
            return ask
        if path != BATCH_PATH:
            return False
        rpcids = rpcids_of(url, body)
        allowed = GENERATE_RPCIDS if generate else READ_RPCIDS
        return bool(rpcids) and len(rpcids) == 1 and rpcids[0] in allowed
    return google.allowed(method, url, APP_HOSTS)


def media_url_allowed(url: str) -> bool:
    """Whether an artifact's file may be fetched from `url` with page.request (which the route guard never sees):
    https on a MEDIA_HOSTS host, default port, no credentials in the URL. Checked before every hop."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and port is None and "@" not in parts.netloc
            and bool(MEDIA_HOSTS.fullmatch(parts.hostname or "")))


def describe(method: str, url: str, body: str | None = None) -> str:
    """METHOD host/path [rpcids=...] for error messages: no query string (it carries the session id), token-looking
    segments redacted."""
    parts = urlsplit(url)
    text = f"{method} {parts.hostname}{redact_url(parts.path)}"
    if parts.path == BATCH_PATH:
        ids = sorted({REDACTED if tokenish(r) else r for r in freq_rpcids(body)})
        text += f" rpcids={','.join(ids)}"
    return text


def _body(request: "Request") -> str | None:
    return request.post_data if urlsplit(request.url).path == BATCH_PATH else None


def _policy(name: str, *, ask: bool = False, generate: bool = False) -> Policy:
    def allowed(request: "Request", state: Guard) -> bool:
        return request_allowed(request.method, request.url, _body(request), ask=ask, generate=generate)

    def describe_request(request: "Request") -> str:
        try:
            body = _body(request)
        except Exception:
            body = None
        return describe(request.method, request.url, body)

    return Policy(name, allowed, describe_request)


READ = _policy("notebooklm")
ASK = _policy("notebooklm", ask=True)
GENERATE = _policy("notebooklm", generate=True)
POLICY = READ
