"""A scripted tab for the Google data sites' fetch tests: no browser, no network.

install(monkeypatch, page) makes browser.site_page yield `page`. goto lands on redirects.get(url, url), tells the
page's request listeners about `requests[url]`, then replays `responses[url]` to its response listeners. evaluate
answers from `answers`, keyed by a substring of the script (a callable gets the script's argument). page.request.get
answers from `files` (url -> APIResponse).
"""

from __future__ import annotations

import inspect
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from webcli.core import browser


class Response:
    def __init__(self, url: str, data, method: str = "POST") -> None:
        self.url, self.data = url, data
        self.request = SimpleNamespace(method=method, url=url)

    async def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data

    async def text(self):
        return self.data if isinstance(self.data, str) else json.dumps(self.data)


class APIResponse:
    def __init__(self, status: int = 200, body: bytes = b"", headers: dict | None = None) -> None:
        self.status, self._body, self.headers = status, body, headers or {}
        self.disposed = False

    async def body(self) -> bytes:
        return self._body

    async def dispose(self) -> None:
        self.disposed = True


class ScriptedPage:
    def __init__(self, *, redirects=None, responses=None, requests=None, answers=None, files=None, html="",
                 text="") -> None:
        self.url = "about:blank"
        self.redirects, self.responses, self.requests = redirects or {}, responses or {}, requests or {}
        self.answers, self.files, self.html, self.text = answers or {}, files or {}, html, text
        self.listeners: dict[str, list] = {"response": [], "request": []}
        self.gotos: list[str] = []
        self.fetched: list[str] = []
        self.keys: list[str] = []
        self.scrolls = 0
        self.policy = None
        self.mouse = SimpleNamespace(wheel=self._wheel)
        self.keyboard = SimpleNamespace(press=self._press)
        self.request = SimpleNamespace(get=self._get)

    def on(self, event: str, handler) -> None:
        self.listeners[event].append(handler)

    def remove_listener(self, event: str, handler) -> None:
        self.listeners[event].remove(handler)

    async def goto(self, url: str, **_options) -> None:
        self.gotos.append(url)
        self.url = self.redirects.get(url, url)
        for request_url in self.requests.get(url, []):
            for handler in list(self.listeners["request"]):
                handler(SimpleNamespace(url=request_url))
        for response in self.responses.get(url, []):
            for handler in list(self.listeners["response"]):
                result = handler(response)
                if inspect.isawaitable(result):
                    await result

    async def evaluate(self, script: str, arg=None):
        for key, value in self.answers.items():
            if key in script:
                return value(arg) if callable(value) else value
        raise AssertionError(f"unscripted evaluate: {script.strip()[:60]!r}")

    async def wait_for_timeout(self, _ms) -> None:
        pass

    async def wait_for_function(self, *_args, **_options) -> None:
        pass

    async def content(self) -> str:
        return self.html

    async def inner_text(self, _selector) -> str:
        return self.text

    async def _wheel(self, _x, _y) -> None:
        self.scrolls += 1

    async def _press(self, key: str) -> None:
        self.keys.append(key)

    async def _get(self, url: str, **_options) -> APIResponse:
        self.fetched.append(url)
        return self.files[url]


def install(monkeypatch, page: ScriptedPage) -> ScriptedPage:
    @asynccontextmanager
    async def site_page(policy, **_options):
        page.policy = policy
        yield page

    monkeypatch.setattr(browser, "site_page", site_page)
    return page
