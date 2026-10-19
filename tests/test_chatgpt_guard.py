"""Offline tests (from cgpt) for chatgpt's request policy, Cloudflare detection, and never falling back to a
window."""

from __future__ import annotations

import asyncio
import json
import os
import stat

import pytest

from webcli.core import browser
from webcli.core.errors import Challenged
from webcli.sites.chatgpt import endpoints, parse
from webcli.sites.chatgpt.guard import request_allowed

C = "https://chatgpt.com"
CHAT = "68f3c1a2-1234-8000-9abc-0123456789ab"


def allowed(method, url, body=None, *, send=False, sign_in=False, expect=None):
    return request_allowed(method, url, body, send=send, sign_in=sign_in,
                           expect_models=frozenset(expect) if expect else None)


@pytest.mark.parametrize(("method", "url", "send", "ok"), [
    # Reads: the app, its API GETs, its CDNs, Cloudflare's check.
    ("GET", C + "/", False, True),
    ("GET", C + f"/c/{CHAT}", False, True),
    ("GET", C + "/backend-api/models?iim=false", False, True),
    ("GET", C + f"/backend-api/conversation/{CHAT}", False, True),
    ("GET", C + "/backend-api/files/download/file_00000000fake?conversation_id=x&inline=false", False, True),
    ("GET", "https://cdn.oaistatic.com/assets/app.js", False, True),
    ("GET", "https://sdmntprwestus.oaiusercontent.com/files/x?sig=y", False, True),
    ("GET", "https://challenges.cloudflare.com/turnstile/v0/api.js", False, True),
    ("POST", C + "/cdn-cgi/challenge-platform/h/b/jsd/oneshot/x", False, True),
    ("POST", "https://ab.chatgpt.com/v1/initialize?k=x", False, True),
    # Third parties and telemetry: never.
    ("GET", "https://www.google-analytics.com/collect", True, False),
    ("POST", "https://o33249.ingest.sentry.io/api/1/envelope/", True, False),
    ("POST", C + "/ces/v1/t", True, False),
    ("POST", C + "/ces/v1/rgstr?k=x", True, False),
    ("POST", "https://ab.chatgpt.com/v1/rgstr", True, False),
    # Sign-out GETs are aborted even though they are GETs.
    ("GET", C + "/auth/logout", True, False),
    ("GET", C + "/api/auth/signout", False, False),
    # The prompt and its helpers: only with send=True, https, POST.
    ("POST", C + "/backend-api/f/conversation", False, False),
    ("POST", C + "/backend-api/f/conversation", True, True),
    ("POST", "http://chatgpt.com/backend-api/f/conversation", True, False),
    ("GET", C + "/backend-api/f/conversation", True, False),  # send paths take only their listed methods
    ("POST", C + "/backend-api/f/conversation/prepare", True, True),
    ("POST", C + "/backend-api/conversation", True, True),
    ("POST", C + "/backend-api/conversation/init", False, False),
    ("POST", C + "/backend-api/conversation/init", True, True),
    ("POST", C + "/backend-api/sentinel/chat-requirements", True, True),
    ("POST", C + "/backend-api/sentinel/chat-requirements/prepare", True, True),
    ("POST", C + "/backend-api/sentinel/chat-requirements/prepare", False, False),
    ("POST", C + "/backend-api/files", True, True),
    ("POST", C + "/backend-api/files", False, False),
    ("POST", C + "/backend-api/files/file-abc123/uploaded", True, True),
    ("POST", C + "/backend-api/files/file-abc123/uploaded", False, False),
    ("POST", C + "/backend-api/files/process_upload_stream", True, True),
    ("PUT", "https://files.oaiusercontent.com/file-abc?se=x&sig=y", True, True),
    ("PUT", "https://files.oaiusercontent.com/file-abc?se=x&sig=y", False, False),
    # Account writes: never, send or not.
    ("PATCH", C + f"/backend-api/conversation/{CHAT}", True, False),
    ("DELETE", C + f"/backend-api/conversation/{CHAT}", True, False),
    ("PATCH", C + "/backend-api/conversations", True, False),
    ("POST", C + f"/backend-api/conversation/gen_title/{CHAT}", True, False),
    ("POST", C + "/backend-api/share/create", True, False),
    ("POST", C + "/backend-api/conversation/message_feedback", True, False),
    ("POST", C + "/backend-api/memories", True, False),
    ("DELETE", C + "/backend-api/memories", True, False),
    ("POST", C + "/backend-api/user_system_messages", True, False),
    ("PATCH", C + "/backend-api/settings/account_user_setting?feature=x&value=y", True, False),
    ("POST", C + "/backend-api/gizmos/snorlax/upsert", True, False),
    ("POST", C + "/backend-api/beacons/event", True, False),
    ("POST", C + "/backend-api/amphora/clear_settings_cache", True, False),
    ("POST", C + "/backend-api/tpp/default-tab-recommendation", True, False),
    ("PUT", C + "/", True, False),
])
def test_guard(method, url, send, ok):
    assert allowed(method, url, send=send) is ok


def test_every_known_write_is_blocked_in_send_mode():
    for (method, path), what in endpoints.MUTATING.items():
        url = C + path.replace("<id>", CHAT)
        assert not allowed(method, url, send=True), what


def test_a_prompt_naming_another_model_is_aborted_when_m_was_given():
    url = C + "/backend-api/f/conversation"
    body = json.dumps({"action": "next", "model": "gpt-6-thinking", "messages": []})
    assert allowed("POST", url, body, send=True, expect=["gpt-6-thinking"])
    assert not allowed("POST", url, body, send=True, expect=["gpt-6-instant", "gpt-6"])
    assert allowed("POST", url, body, send=True)  # no -m: any model
    for body in ("{}", '{"model": null}', '{"model": 6}', "not json", ""):  # fail closed: no model, no send
        assert not allowed("POST", url, body, send=True, expect=["gpt-6-instant"]), body
    assert allowed("POST", url, "{}", send=True)  # without -m the page's default is fine


def test_sign_in_mode_allows_auth_pages_and_other_hosts_but_not_sign_out():
    assert allowed("POST", "https://auth.openai.com/api/accounts/authorize/continue", sign_in=True)
    assert allowed("POST", C + "/api/auth/callback/openai", sign_in=True)
    assert not allowed("GET", C + "/api/auth/signout", sign_in=True)
    assert not allowed("POST", C + "/backend-api/f/conversation", sign_in=True)  # sign-in never sends prompts
    assert not allowed("POST", "https://auth.openai.com/api/accounts/authorize/continue")


@pytest.mark.parametrize(("state", "expected"), [
    ({"title": "Just a moment...", "url": C + "/"}, True),
    ({"title": "Just a moment…", "url": C + "/"}, True),
    ({"title": "Attention Required! | Cloudflare", "url": C + "/"}, True),
    ({"title": "chatgpt.com", "url": C + "/?__cf_chl_rt_tk=abc"}, True),
    ({"title": "ChatGPT", "url": C + "/", "form": True}, True),  # the challenge form / Turnstile widget
    ({"title": "", "url": C + "/", "iframe": True, "app": False}, True),  # a bare Turnstile frame
    ({"title": "ChatGPT", "url": C + "/", "iframe": True, "app": True}, False),  # a frame inside the app
    ({"title": "ChatGPT", "url": C + "/", "app": True}, False),
    ({"title": "Tips to use ChatGPT", "url": C + f"/c/{CHAT}"}, False),
    ({"title": "Log in - OpenAI", "url": "https://auth.openai.com/log-in"}, False),  # sign-in, not a challenge
    ({"url": C + "/"}, False),  # mid-navigation: URL alone
    ({}, False),
])
def test_cloudflare_challenge_detection(state, expected):
    assert parse.cloudflare_challenge(state) is expected


def test_user_agent_drops_headless(tmp_path):
    import plistlib
    plist = tmp_path / "Info.plist"
    plist.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "155.0.8059.80"}))
    ua = browser.user_agent(plist)
    assert "Chrome/155.0.0.0 Safari/537.36" in ua and "Headless" not in ua
    assert browser.user_agent(tmp_path / "missing.plist") is None


def test_with_app_runs_headless_once_and_never_falls_back_to_a_window(monkeypatch):
    from contextlib import asynccontextmanager

    from webcli.sites.chatgpt import fetch
    tried = []

    def fake_open(challenged_modes):
        @asynccontextmanager
        async def open_page(policy, *, mode, url, **_):
            tried.append(mode)
            if mode in challenged_modes:
                raise Challenged(mode, "chatgpt.com")
            yield f"page:{mode}"
        return open_page

    async def work(page):
        return page

    monkeypatch.setattr(browser, "open_page", fake_open(set()))
    assert asyncio.run(fetch.with_app(work)) == "page:headless"
    monkeypatch.setattr(browser, "open_page", fake_open({"headless"}))
    with pytest.raises(Challenged):
        asyncio.run(fetch.with_app(work))
    assert tried == ["headless", "headless"]  # no off-screen or visible retry
    tried.clear()
    assert asyncio.run(fetch.with_app(work, headed=True)) == "page:visible"  # only an explicit --headed
    assert tried == ["visible"]
    with pytest.raises(ValueError):
        browser.launch_options(browser.Path("/tmp/x"), "offscreen")


def test_with_app_uses_the_daemons_chrome_when_inside_serve(monkeypatch):
    from contextlib import asynccontextmanager

    from webcli.sites.chatgpt import fetch
    from webcli.sites.chatgpt.guard import POLICY
    seen = []

    class Shared:
        @asynccontextmanager
        async def page(self, policy, *, url, send, ready, match_headless):
            seen.append((policy, url, send))
            yield "daemon-page"

    async def work(page):
        return page

    monkeypatch.setattr(browser, "shared", Shared())
    assert asyncio.run(fetch.with_app(work, send=True)) == "daemon-page"
    assert seen == [(POLICY, fetch.APP_URL, True)]
    with pytest.raises(RuntimeError, match="never opens a visible Chrome"):  # --headed can't reach the daemon's Chrome
        asyncio.run(fetch.with_app(work, headed=True))
