"""www.facebook.com's request policy for the core guard (fbc's guard on the shared core). Read-only, fail-closed.

read_policy(reads) is a command's policy: `reads` are the GraphQL queries that command may run (endpoints.py:
SYNC_QUERIES for sync, none for About pages). There is no sign-in mode: `webcli identity login facebook:default`
opens Chrome with no guard, as every webcli sign-in does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlsplit

from webcli.core.guard import Guard, Policy, describe as describe_url, host_matches
from webcli.sites.facebook.endpoints import APP_HOST, FB_SUFFIXES, GRAPHQL_PATHS, HOSTS, SIDE_EFFECT_GETS

if TYPE_CHECKING:
    from playwright.async_api import Request


def fb_host(host: str) -> bool:
    host = host.lower()
    return any(host == s or host.endswith("." + s) for s in FB_SUFFIXES)


def _under(path: str, prefix: str) -> bool:
    """path is prefix or below it, as whole segments: /logout and /logout/x, never /logoutx."""
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def graphql_op(headers: dict[str, str] | None, body: str | None) -> str:
    """The operation a POST /api/graphql/ runs, for the guard. ValueError when it can't be told for sure.

    The body must be a form with exactly one fb_api_req_friendly_name and one numeric doc_id, and an
    x-fb-friendly-name header, if sent, must name the same operation. JSON bodies, batches (a `queries` field), and
    repeated or disagreeing labels are refused, so the guard never has to pick one label over another.
    """
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    text = (body or "").strip()
    if not text or text.startswith(("{", "[")):
        raise ValueError("not a form body")
    pairs = parse_qsl(text, keep_blank_values=True)
    names = [v for k, v in pairs if k == "fb_api_req_friendly_name"]
    doc_ids = [v for k, v in pairs if k == "doc_id"]
    if any(k in ("queries", "batch_name") for k, _ in pairs):
        raise ValueError("batched queries")
    if len(names) != 1 or not names[0]:
        raise ValueError("no single operation name")
    if len(doc_ids) != 1 or not doc_ids[0].isdigit():
        raise ValueError("no single numeric doc_id")
    header = headers.get("x-fb-friendly-name")
    if header is not None and header != names[0]:
        raise ValueError("operation names disagree")
    return names[0]


def read_op(op: str) -> bool:
    """A query by name: ends in Query and says nothing about mutating or subscribing."""
    lowered = op.lower()
    return op.endswith("Query") and "mutation" not in lowered and "subscription" not in lowered


def request_allowed(method: str, url: str, headers: dict[str, str] | None = None, body: str | None = None,
                    *, reads: frozenset[str] = frozenset()) -> bool:
    """The guard. Fails closed: anything not listed in endpoints.py is aborted.

    - GET/HEAD: https on facebook.com, its subdomains, and its CDN (*.fbcdn.net) only, minus SIDE_EFFECT_GETS
      (sign-out) and GETs to Facebook's GraphQL paths (/api/graphql/, /webgraphql/...).
    - POST https://www.facebook.com/api/graphql/ only when its operation (graphql_op) is in `reads`, the running
      command's read-only queries, and is named like a query (read_op).
    - Every other request is aborted: friending, reactions, comments, posts, messages, pokes, follows, settings,
      "seen" markers, telemetry, every operation this site doesn't know, and every other host.

    Limits (fbc's): Facebook's server runs the doc_id; the operation name is the label Facebook's own client puts
    next to it, and the guard trusts that label. Playwright routes only the first URL of a redirect chain.
    """
    method = method.upper()
    parts = urlsplit(url)
    host, path = (parts.hostname or "").lower(), parts.path
    if parts.scheme != "https" or parts.port not in (None, 443) or "@" in parts.netloc:
        return False
    if method in ("GET", "HEAD"):
        if not any(host_matches(pattern, host) for pattern in HOSTS):
            return False
        if path in GRAPHQL_PATHS or _under(path, "/webgraphql"):
            return False
        return not any(host == h and _under(path, prefix) for h, prefix in SIDE_EFFECT_GETS)
    if method != "POST" or host != APP_HOST or path not in ("/api/graphql/", "/api/graphql"):
        return False
    try:
        op = graphql_op(headers, body)
    except ValueError:
        return False
    return op in reads and read_op(op)


def describe(method: str, url: str, headers: dict[str, str] | None = None, body: str | None = None) -> str:
    """'POST www.facebook.com/api/graphql/ op=Name': host and path only (token-looking segments redacted), never
    the query string or the body."""
    text = describe_url(method.upper(), url)
    if urlsplit(url).path in GRAPHQL_PATHS and method.upper() == "POST":
        try:
            op = graphql_op(headers, body)
        except ValueError:
            op = "<unreadable>"
        text += f" op={op if op.isidentifier() else '<redacted>'}"
    return text


def _post(request: "Request") -> tuple[dict[str, str] | None, str | None]:
    """A POST's headers and body for the policy; (None, None) for anything else. request.headers is sync and keeps
    the app's own x-fb-friendly-name."""
    if request.method.upper() != "POST":
        return None, None
    try:
        return request.headers, request.post_data
    except Exception:  # binary: no operation can be read, so it's refused
        return request.headers, None


def read_policy(reads: frozenset[str] = frozenset()) -> Policy:
    """A command's policy: page loads, plus the GraphQL queries in `reads`."""

    def allowed(request: "Request", state: Guard) -> bool:
        return request_allowed(request.method, request.url, *_post(request), reads=reads)

    return Policy("facebook", allowed, lambda request: describe(request.method, request.url, *_post(request)))


POLICY = read_policy()
