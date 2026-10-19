"""www.loom.com's request policy for the core guard. Read-only, and fail-closed.

- GET/HEAD: https on loom.com and its subdomains only (the app, its API, and the CDN the transcript file comes from),
  minus sign-out.
- GraphQL (www.loom.com/graphql), POST or GET: only when every operation in it is a query. A mutation, a
  subscription, a document that can't be read, and a persisted query sent without its text are all aborted, so the
  library and share pages can read what they show and nothing they send can change anything (a view count, a
  reaction, a comment).
- Everything else is aborted: every other POST, PUT, PATCH, DELETE, telemetry, and every other host.

Limits: GraphQL says a query doesn't write, and the guard trusts that. Playwright routes only the first URL of a
redirect chain.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from webcli.core.guard import Guard, Policy, describe, host_matches
from webcli.sites.loom.endpoints import APP_HOST, GRAPHQL_PATHS, HOSTS, SIDE_EFFECT_GETS

if TYPE_CHECKING:
    from playwright.async_api import Request

_NAME = re.compile(r"[_A-Za-z][_0-9A-Za-z]*")
READ_DEFINITIONS = frozenset({"query", "fragment", "{"})


def _string_end(doc: str, i: int) -> int:
    """Where the string starting at doc[i] ends (just past its closing quote), or -1 if it never does."""
    if doc.startswith('"""', i):
        j = i + 3
        while (j := doc.find('"""', j)) >= 0 and doc[j - 1] == "\\":
            j += 1
        return -1 if j < 0 else j + 3
    j = i + 1
    while j < len(doc) and doc[j] not in '"\n\r':
        j += 2 if doc[j] == "\\" else 1
    return j + 1 if j < len(doc) and doc[j] == '"' else -1


def definitions(doc: str) -> list[str] | None:
    """The kind of each top-level definition in a GraphQL document: "query", "mutation", "subscription", "fragment",
    or "{" (the query shorthand). None when it can't be read: an unclosed string, brace, or parenthesis. Strings and
    comments are skipped; braces inside a variable's default value (inside parentheses) don't start a definition."""
    kinds: list[str] = []
    depth = parens = i = 0
    expect = True  # at depth 0, the next word or "{" starts a definition
    while i < len(doc):
        c = doc[i]
        if c == "#":  # a comment runs to the line's end: \n, \r, or \r\n (a \r alone would hide what follows)
            ends = [j for j in (doc.find("\n", i), doc.find("\r", i)) if j >= 0]
            i = min(ends) if ends else len(doc)
            continue
        if c == '"':
            i = _string_end(doc, i)
            if i < 0:
                return None
            continue
        top = depth == 0 and parens == 0
        if c == "{":
            if top and expect:
                kinds.append("{")
            expect = expect and not top
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                return None
            expect = expect or (depth == 0 and parens == 0)
        elif c == "(":
            parens += 1
        elif c == ")":
            parens -= 1
            if parens < 0:
                return None
        elif top and expect and (word := _NAME.match(doc, i)):
            kinds.append(word.group(0))
            expect = False
            i = word.end()
            continue
        i += 1
    return kinds if depth == 0 and parens == 0 else None


def read_only_document(doc) -> bool:
    """A GraphQL document that defines at least one operation, and only queries (and fragments)."""
    kinds = definitions(doc) if isinstance(doc, str) else None
    return bool(kinds) and set(kinds) <= READ_DEFINITIONS and bool(set(kinds) - {"fragment"})


def read_only_body(body: str | None) -> bool:
    """A GraphQL POST body (one {query, ...} object, or a batch of them) where every document is read-only."""
    try:
        data = json.loads(body or "")
    except ValueError:
        return False
    items = data if isinstance(data, list) else [data]
    return bool(items) and all(isinstance(item, dict) and read_only_document(item.get("query")) for item in items)


def request_allowed(method: str, url: str, body: str | None = None) -> bool:
    """The policy (see the module doc). Fails closed."""
    method = method.upper()
    parts = urlsplit(url)
    host, path = parts.hostname or "", parts.path
    if parts.scheme != "https" or parts.port not in (None, 443) or "@" in parts.netloc:
        return False
    if not any(host_matches(pattern, host) for pattern in HOSTS):
        return False
    if path in GRAPHQL_PATHS:
        if host != APP_HOST:
            return False
        if method == "POST":
            return read_only_body(body)
        queries = parse_qs(parts.query).get("query", [])
        return method in ("GET", "HEAD") and len(queries) == 1 and read_only_document(queries[0])
    if method in ("GET", "HEAD"):
        return not any(host == h and path.startswith(prefix) for h, prefix in SIDE_EFFECT_GETS)
    return False


def _body(request: "Request") -> str | None:
    if request.method.upper() != "POST":
        return None
    try:
        return request.post_data
    except Exception:  # binary: not a GraphQL document, so refused
        return None


def _allowed(request: "Request", state: Guard) -> bool:
    return request_allowed(request.method, request.url, _body(request))


def _describe(request: "Request") -> str:
    """METHOD host/path, plus the GraphQL operation name for a blocked GraphQL POST (a name, never a value)."""
    text = describe(request.method, request.url)
    if request.method.upper() == "POST" and urlsplit(request.url).path in GRAPHQL_PATHS:
        try:
            data = json.loads(_body(request) or "")
            items = data if isinstance(data, list) else [data]
            names = [str(item.get("operationName")) for item in items if isinstance(item, dict)]
        except (ValueError, AttributeError):
            names = []
        text += f" op={','.join(n for n in names if _NAME.fullmatch(n)) or '<unreadable>'}"
    return text


POLICY = Policy("loom", _allowed, _describe)
