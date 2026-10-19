"""Capture: what a site's page really requests, as redacted lines. READ-ONLY: the site's own policy with send off,
so no prompt and no write can leave. The essentials of cgpt's scripts/probe.py and gweb's scripts/sniff.py.

    python -m webcli.core.capture SITE [--identity ID] [--url URL] [--wait SEC] [--headed]

Writes sites/<site>/capture/<site>-<UTC>.json (dir 0700, file 0600) and prints it. Everything in it went through
redaction: request lines are METHOD host/path plus query and POST field NAMES (never values), and blocked requests
are the guard's describe() lines. Raw bodies are never kept. A dev aid: no command or test runs it.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlsplit

from webcli.core import browser, config, daemon, guard
from webcli.core.output import write_atomic
from webcli.core.redact import post_body_keys, redact_text, redact_value

if TYPE_CHECKING:
    from playwright.async_api import Request


def request_line(method: str, url: str, body: str | None = None) -> str:
    """METHOD host/path ?query-keys keys=[post-field-names]: names only, every value dropped."""
    keys = sorted({k for k, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)})
    line = guard.describe(method, url) + (f" ?{','.join(keys)}" if keys else "")
    if method.upper() != "GET" and body:
        line += f" keys={post_body_keys(body)}"
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
