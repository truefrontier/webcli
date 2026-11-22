"""Capture: what a site's page really requests, as redacted lines. READ-ONLY: the site's own policy with send off,
so no prompt and no write can leave. The essentials of cgpt's scripts/probe.py and gweb's scripts/sniff.py.

    python -m webcli.core.capture SITE [--identity ID] [--url URL] [--wait SEC] [--headed]

Writes sites/<site>/capture/<site>-<UTC>.json (dir 0700, file 0600) and prints it. Everything in it went through
redaction: request lines are METHOD host/path plus query and POST field NAMES (never values), and blocked requests
are the guard's describe() lines. Raw bodies are never kept. A dev aid: no command or test runs it.

Guided capture (`webcli new NAME --url URL --capture`) is for a site with no adapter yet: watch() opens the URL in a
visible Chrome on the tool's identity for a person to click around in, with a guard that lets only reads out (GET,
HEAD, OPTIONS; every POST, PUT, PATCH, DELETE and WebSocket is aborted and listed). propose() turns what it saw into
the tool's hosts and candidate read commands, and fixture() scrubs the last page snapshot for tests/fixtures/. Its
record also keeps `shapes`: the keys and types (never the values) of the first-party JSON the pages read, by request
line, so an agent can write a parser without seeing the person's data (webcli/skills/webcli-tool-from-capture).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import parse_qsl, urlsplit

from webcli.core import browser, config, daemon, guard
from webcli.core.output import write_atomic
from webcli.core.redact import post_body_keys, redact_text, redact_value, tokenish

if TYPE_CHECKING:
    from playwright.async_api import Page, Request, Response

READ_METHODS = ("GET", "HEAD", "OPTIONS")
# What a scaffolded tool reads off a page, and what guided capture snapshots for its fixture: one definition, so a
# captured fixture is exactly what the tool's parse.py gets. The scaffold renders it into the tool's fetch.py.
PAGE_JS = """() => ({
  url: location.href,
  title: document.title,
  loginForm: !!document.querySelector('input[type="password"]'),
  headings: [...document.querySelectorAll('h1, h2, h3')].map(h => (h.innerText || '').trim()).filter(Boolean)
    .slice(0, 50),
  links: [...document.querySelectorAll('a[href]')].map(a => ({text: (a.innerText || '').trim(), href: a.href}))
    .filter(l => l.text && /^https?:/.test(l.href)).slice(0, 200),
})"""
FIXTURE_ITEMS = 20  # headings and links kept in a captured fixture: enough to test parsing, little personal data
# Response shapes (schema(), Shapes): bounds on what one capture reads and keeps.
SHAPE_DEPTH = 12  # deeper than this is "<deep>"
SHAPE_WIDE = 40  # an object with more keys than this is a map, so its keys are data: one "<key>"
SHAPE_VARIANTS = 3  # distinct shapes kept per request line, per list's items, per map's values
SHAPE_LINES = 40  # request lines with shapes, per capture
SHAPE_READS = 10  # responses read per request line; later repeats (a page that polls) aren't read or counted
SHAPE_BODY_BYTES = 2_000_000  # a bigger body isn't parsed. ponytail: Playwright reads a body whole, so one with
# no Content-Length is in memory before the check; SHAPE_READS, SHAPE_LINES and the semaphore bound how many
SHAPE_CHARS = 20_000  # a bigger shape is kept as {"dropped": "oversize"}
SHAPE_WAIT = 10.0  # seconds to wait for bodies still loading when the window closes
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_GRAPHQL_KIND = re.compile(r"^\s*(query|mutation|subscription)\b")
_FIELD = re.compile(r"[A-Za-z_$][\w$.:-]{0,39}")  # what a field name looks like; a title, an email, an id don't


def graphql_op(body: str | None) -> str | None:
    """"op=Name (query)" for a GraphQL POST body naming its operation, the way manifests write requests; else None.
    A persisted query (a hash, no query text) says "(unknown)": only its name is known, not whether it writes."""
    try:
        data = json.loads(body or "")
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("operationName"), str):
        return None
    kind = _GRAPHQL_KIND.match(data.get("query") or "")
    return f"op={data['operationName']} ({kind[1] if kind else 'unknown'})"


def request_line(method: str, url: str, body: str | None = None) -> str:
    """METHOD host/path ?query-keys keys=[post-field-names]: names only, every value dropped (a GraphQL body: its
    operation name and kind instead)."""
    keys = sorted({k for k, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)})
    line = guard.describe(method, url) + (f" ?{','.join(keys)}" if keys else "")
    if method.upper() != "GET" and body:
        line += f" {graphql_op(body) or f'keys={post_body_keys(body)}'}"
    return redact_text(line)


class Recorder:
    """Every request a context's pages make, as request_line()s, in order, without repeats."""

    def __init__(self) -> None:
        self.traffic: list[str] = []

    def on_request(self, request: "Request") -> None:
        try:
            body = request.post_data
        except Exception:
            body = None  # binary upload
        line = request_line(request.method, request.url, body)
        if line not in self.traffic:
            self.traffic.append(line)


def field_name(key: Any) -> bool:
    """Whether an object key reads as a field name ("title", "nextCursor", "@type") rather than data: an id, an
    email, a title with spaces, anything with four or more digits. ponytail: a short plain-word key that is data
    (a username) still passes; SHAPE_WIDE and the digit rule catch the maps that matter."""
    return isinstance(key, str) and bool(_FIELD.fullmatch(key.removeprefix("@"))) \
        and sum(c.isdigit() for c in key) < 4 and not tokenish(key)


def _variants(shapes: Any) -> list:
    out: list = []
    for item in shapes:
        if item not in out:
            out.append(item)
            if len(out) == SHAPE_VARIANTS:
                break
    return out


def schema(node: Any, depth: int = 0) -> Any:
    """A JSON value's keys and types, never its values: "str", "int", "float", "bool", "null"; a list is
    ["<list>", item shapes...]; an object whose keys are data (field_name(), SHAPE_WIDE) is {"<key>": value shape}.
    Up to SHAPE_VARIANTS distinct item or value shapes; several are ["<one of>", ...] under "<key>"."""
    if depth >= SHAPE_DEPTH:
        return "<deep>"
    if isinstance(node, dict):
        if len(node) <= SHAPE_WIDE and all(field_name(key) for key in node):
            return {key: schema(value, depth + 1) for key, value in node.items()}
        found = _variants(schema(value, depth + 1) for value in list(node.values())[:50])
        return {"<key>": found[0] if len(found) == 1 else ["<one of>", *found]}
    if isinstance(node, list):
        return ["<list>", *_variants(schema(item, depth + 1) for item in node[:50])]
    return "null" if node is None else type(node).__name__


class Shapes:
    """schema() of each first-party JSON GET a context's pages read with status 200, by request_line(): what an
    agent writes a parser against. Bodies are read as they arrive and dropped; failures are counted, never kept
    (an error's text can quote the body)."""

    def __init__(self, url: str) -> None:
        self.url, self.shapes, self.skipped = url, {}, 0
        self.reads: dict[str, int] = {}
        self.tasks: set[asyncio.Task] = set()
        self.gate = asyncio.Semaphore(4)

    def on_response(self, response: "Response") -> None:
        try:
            if response.request.method.upper() != "GET" or response.status != 200 \
                    or not first_party(self.url, urlsplit(response.url).hostname or "") \
                    or "json" not in (response.headers.get("content-type") or "").lower():
                return
            line = request_line("GET", response.url)
        except Exception:
            return
        if line not in self.reads and len(self.reads) >= SHAPE_LINES:
            self.skipped += 1
            return
        self.reads[line] = self.reads.get(line, 0) + 1
        if self.reads[line] <= SHAPE_READS:
            self.tasks.add(asyncio.get_running_loop().create_task(self._read(line, response)))

    async def _read(self, line: str, response: "Response") -> None:
        try:
            async with self.gate:
                if int(response.headers.get("content-length") or 0) > SHAPE_BODY_BYTES:
                    raise ValueError
                body = await response.body()
            if len(body) > SHAPE_BODY_BYTES:
                raise ValueError
            found = schema(json.loads(body))
        except Exception:
            self.skipped += 1
            return
        if len(json.dumps(found)) > SHAPE_CHARS:
            found = {"dropped": "oversize"}
        kept = self.shapes.setdefault(line, [])
        if found not in kept and len(kept) < SHAPE_VARIANTS:
            kept.append(found)

    async def finish(self, wait: float = SHAPE_WAIT) -> dict:
        """{shapes, shapesSkipped} once the reads still loading are done, or `wait` seconds pass (the rest are
        cancelled and counted as skipped)."""
        if self.tasks:
            _done, pending = await asyncio.wait(self.tasks, timeout=wait)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            self.skipped += len(pending)
        return {"shapes": self.shapes, "shapesSkipped": self.skipped}


def write(site: str, data: dict) -> Path:
    """sites/<site>/capture/<site>-<UTC>.json, 0600, every string redacted."""
    folder = config.private_dir(config.site_dir(site) / "capture")
    path = folder / f"{site}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    write_atomic(path, json.dumps(redact_value(data), indent=1) + "\n")
    return path


async def probe(site_name: str, url: str | None, wait: float, mode: str) -> dict:
    from webcli import sites

    site = sites.get(site_name)
    fetch = importlib.import_module(site.fetch)
    recorder = Recorder()
    async with browser.open_page(fetch.POLICY, mode=mode) as page:
        page.context.on("request", recorder.on_request)
        await page.goto(url or fetch.APP_URL, wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(wait)
        title = await page.title()
        return {"site": site.name, "identity": browser.current().id, "url": redact_text(page.url),
                "title": redact_text(title), "traffic": recorder.traffic,
                "blocked": list(dict.fromkeys(guard.state.blocked))}


def read_only_policy(name: str) -> guard.Policy:
    """Guided capture's guard: reads (GET, HEAD, OPTIONS) to any host, so the page renders and shows what it needs;
    every other method is aborted and listed in guard.state.blocked, WebSockets closed. A GET can still change
    something on a badly built site; this stops the writes a site marks as writes, not those it disguises."""
    return guard.Policy(name, lambda request, _state: request.method.upper() in READ_METHODS,
                        lambda request: guard.describe(request.method, request.url))


async def record(page: "Page", url: str, timeout: float, poll: float = 2.0, shape_wait: float = SHAPE_WAIT) -> dict:
    """Open `url` in `page` and record its requests until a person closes the window or `timeout` runs out:
    {url, traffic, blocked, snapshot, shapes, shapesSkipped}. snapshot: PAGE_JS on the last page that answered (None
    if none did). shapes: Shapes, waited for up to `shape_wait` seconds."""
    recorder, shapes, snapshot = Recorder(), Shapes(url), None
    page.context.on("request", recorder.on_request)
    page.context.on("response", shapes.on_response)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not page.is_closed() and loop.time() < deadline:
            try:
                snapshot = await page.evaluate(PAGE_JS)
            except Exception:
                pass  # between pages, or the window just closed
            await asyncio.sleep(poll)
    finally:
        page.context.remove_listener("response", shapes.on_response)
        found = await shapes.finish(shape_wait)
    return {"url": redact_text(url), "traffic": recorder.traffic,
            "blocked": list(dict.fromkeys(guard.state.blocked)), "snapshot": snapshot, **found}


async def watch(name: str, url: str, timeout: float) -> dict:
    """record() in a visible Chrome on the running identity (browser.identity), read-only. Needs a person."""
    async with browser.open_page(read_only_policy(name), mode="visible") as page:
        return await record(page, url, timeout)


def first_party(url: str, host: str) -> bool:
    """Whether `host` belongs to the site at `url`: its host, or under it (a leading www. dropped: www.example.com
    covers api.example.com). ponytail: no public-suffix list, so a sibling like api.example.com is missed when the
    URL is app.example.com; the author adds it by hand."""
    base = (urlsplit(url).hostname or "").removeprefix("www.")
    return bool(base) and (host == urlsplit(url).hostname or host == base or host.endswith("." + base))


def propose(url: str, data: dict) -> dict:
    """What a capture suggests for the tool, from record()'s result: hosts (first-party ones the page read from),
    otherHosts (third-party: not proposed), reads (first-party read request lines, each a candidate command's
    `requests` entry), and blocked (what the guard stopped: a GraphQL "(query)" there is a read the author may add
    by hand; anything else stays out of a read-only tool)."""
    seen: dict[str, None] = {}
    reads: list[str] = []
    for line in data.get("traffic", []):
        method, _, rest = line.partition(" ")
        host = rest.split("/", 1)[0]
        if not host:
            continue
        seen[host] = None
        if method in READ_METHODS and first_party(url, host) and method != "OPTIONS":
            reads.append(line)
    origin = urlsplit(url).hostname or ""
    hosts = [origin, *sorted(h for h in seen if first_party(url, h) and h != origin)]
    return {"hosts": hosts, "otherHosts": sorted(h for h in seen if not first_party(url, h)),
            "reads": reads, "blocked": list(data.get("blocked", []))}


def _scrub(text: Any) -> str:
    return _EMAIL.sub("user@example.com", redact_text(text))


def fixture(snapshot: dict | None) -> dict | None:
    """A page snapshot fit for a public fixture: tokens redacted, email addresses replaced, and only the first
    FIXTURE_ITEMS headings and links kept. Names and other page text stay: the author reads it before sharing."""
    if not isinstance(snapshot, dict):
        return None
    links = [{"text": _scrub(link.get("text")), "href": _scrub(link.get("href"))}
             for link in (snapshot.get("links") or [])[:FIXTURE_ITEMS] if isinstance(link, dict)]
    return {"url": _scrub(snapshot.get("url")), "title": _scrub(snapshot.get("title")),
            "loginForm": bool(snapshot.get("loginForm")),
            "headings": [_scrub(h) for h in (snapshot.get("headings") or [])[:FIXTURE_ITEMS]], "links": links}


def guided(name: str, url: str, timeout: float, run: Callable = browser.run) -> dict:
    """watch() with the identity's daemon paused (it holds the profile), as `identity login` does."""
    return run(daemon.paused(browser.current(), lambda: watch(name, url, timeout)))


def main(argv: list[str] | None = None) -> int:
    from webcli import sites
    from webcli.core.identities import resolve

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("site")
    parser.add_argument("--identity", default=None)
    parser.add_argument("--url", default=None)
    parser.add_argument("--wait", type=float, default=8)
    parser.add_argument("--headed", action="store_true", help="Show the window (the guard stays on, send off).")
    args = parser.parse_args(argv)
    browser.identity = resolve(sites.get(args.site), args.identity)
    mode = "visible" if args.headed else "headless"
    # The identity's daemon holds the profile while its Chrome is open: pause it, as `identity login` does.
    data = browser.run(daemon.paused(browser.identity, lambda: probe(args.site, args.url, args.wait, mode)))
    path = write(args.site, data)
    print(redact_text(json.dumps(data, indent=1)))
    print(f"capture -> {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
