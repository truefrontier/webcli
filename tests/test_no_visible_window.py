"""Required (2): no command but `identity login` / an explicit --headed ever asks Playwright for a visible Chrome.

Each command runs end to end through the real CLI, the identity layer, the daemon client (in-process), the site's
fetch code, and core.browser, into a fake Playwright that records launch_persistent_context's arguments and stops
there. The live check that no window appears (it needs a login) is webcli/scripts/window_check.py:

    python -m webcli.scripts.window_check -- llmcli gemini status
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from click.testing import CliRunner

from webcli.apps import llmcli, webcli as webcli_app
from webcli.core import browser, daemon
from webcli.core.errors import AuthRequired
from webcli.core.identities import Identity
from webcli.sites.chatgpt import fetch as chatgpt_fetch
from webcli.sites.gemini import fetch as gemini_fetch
from webcli.sites.muse import fetch as muse_fetch
from conftest import make_profile

HIDDEN_COMMANDS = [
    ("gemini", "status"), ("gemini", "models"), ("gemini", "chats", "list"),
    ("gemini", "chats", "show", "1a2b3c4d5e6f7a8b"), ("gemini", "ask", "hi"), ("gemini", "-p", "hi"),
    ("gemini", "-p", "--image-out", "{tmp}/", "draw"), ("gemini", "ask", "--canvas", "doc"),
    ("chatgpt", "status"), ("chatgpt", "models"), ("chatgpt", "chats", "list"),
    ("chatgpt", "chats", "show", "68f3c1a2-1234-8000-9abc-0123456789ab"), ("chatgpt", "ask", "hi"),
    ("chatgpt", "-p", "hi"), ("chatgpt", "-p", "-m", "gpt-6", "--image-out", "{tmp}/", "draw"),
    ("muse", "status"), ("muse", "chats", "list"), ("muse", "chats", "show", "7c9e6679-7425-40de-944b-e07fc1f90ae7"),
    ("muse", "chats", "show", "main"), ("muse", "ask", "hi"), ("muse", "ask", "--chat", "main", "hi"),
    ("muse", "-p", "hi"),
]
IDENTITY = {"gemini": "google:default", "chatgpt": "chatgpt:default", "muse": "meta:default"}


def assert_hidden(options: dict) -> None:
    assert options["headless"] is True
    assert options["channel"] == "chrome"
    assert "--use-mock-keychain" in options["ignore_default_args"]
    assert "--enable-automation" in options["ignore_default_args"]
    assert options["service_workers"] == "block"
    assert options["viewport"] == browser.VIEWPORT
    assert "Headless" not in (options["user_agent"] or "")


@pytest.fixture
def profiles():
    return make_profile("google:default"), make_profile("chatgpt:default"), make_profile("meta:default")


@pytest.mark.parametrize("command", HIDDEN_COMMANDS, ids=" ".join)
def test_every_command_launches_hidden_chrome_only(fake_playwright, profiles, tmp_path, command):
    args = [part.replace("{tmp}", str(tmp_path / "out")) for part in command]
    result = CliRunner().invoke(llmcli.build(), args)
    assert result.exit_code == 5, result.output  # the fake stops at the launch: a site error
    assert len(fake_playwright) == 1
    assert_hidden(fake_playwright[0])
    assert fake_playwright[0]["user_data_dir"] == str(Identity(IDENTITY[command[0]]).profile)


def test_muse_models_never_opens_chrome(fake_playwright, profiles):
    result = CliRunner().invoke(llmcli.build(), ["muse", "models"])
    assert result.exit_code == 0 and fake_playwright == []


@pytest.mark.parametrize("args", [["gemini", "models", "--headed"], ["chatgpt", "models", "--headed"],
                                  ["muse", "status", "--headed"], ["muse", "ask", "--headed", "hi"]])
def test_only_an_explicit_headed_shows_a_window(fake_playwright, profiles, args):
    result = CliRunner().invoke(llmcli.build(), args)
    assert result.exit_code == 5
    options = fake_playwright[0]
    assert options["headless"] is False and options["channel"] == "chrome"
    assert {"--use-mock-keychain", "--enable-automation"} <= set(options["ignore_default_args"])
    # chatgpt's window keeps the headless viewport (a check a person clears there must hold up headless)
    assert options["viewport"] == (browser.VIEWPORT if args[0] == "chatgpt" else None)


@pytest.mark.parametrize(("args", "profile"), [
    (["identity", "login", "google:default"], "google:default"),
    (["identity", "login", "chatgpt:default"], "chatgpt:default"),
    (["identity", "login", "meta:default"], "meta:default"),
    (["gemini", "login"], "google:default"),
    (["chatgpt", "login"], "chatgpt:default"),
    (["muse", "login"], "meta:default"),
])
def test_identity_login_is_the_other_visible_path(fake_playwright, args, profile):
    result = CliRunner().invoke(llmcli.build(), args)
    assert result.exit_code == 5
    options = fake_playwright[0]
    assert options["headless"] is False and options["user_data_dir"] == str(Identity(profile).profile)
    assert {"--use-mock-keychain", "--enable-automation"} <= set(options["ignore_default_args"])
    assert options["viewport"] == (browser.VIEWPORT if profile == "chatgpt:default" else None)
    assert options["user_agent"] is None  # set per page instead: see test_chatgpt_login_window_presents_...


def test_status_and_doctor_without_a_profile_never_launch(fake_playwright):
    for app, args in ((llmcli, ["gemini", "status"]), (llmcli, ["chatgpt", "status"]), (llmcli, ["muse", "status"]),
                      (webcli_app, ["doctor"]), (webcli_app, ["identity", "status"])):
        result = CliRunner().invoke(app.build(), args)
        assert result.exit_code in (0, 4), result.output
    assert fake_playwright == []


def test_doctor_checks_sign_in_with_hidden_chrome(fake_playwright, profiles):
    result = CliRunner().invoke(webcli_app.build(), ["doctor"])
    assert result.exit_code == 0
    assert len(fake_playwright) == 3  # one sign-in check per identity: google, chatgpt, meta
    for options in fake_playwright:
        assert_hidden(options)


def test_the_daemons_chrome_is_headless(fake_playwright, profiles):
    async def launch():
        shared = daemon.Shared(Identity("chatgpt:default"))
        await shared.ensure(0)

    with pytest.raises(RuntimeError, match="launch recorded"):
        asyncio.run(launch())
    assert_hidden(fake_playwright[0])


def test_the_daemon_refuses_a_visible_chrome(profiles, monkeypatch):
    monkeypatch.setattr(browser, "shared", object())

    async def visible():
        async with browser.chrome(Identity("google:default"), mode="visible"):
            pass

    with pytest.raises(RuntimeError, match="never opens a visible Chrome"):
        asyncio.run(visible())


def test_launch_options_are_the_only_place_headless_is_decided():
    hidden = browser.launch_options(Path("/p"), "headless")
    assert_hidden({**hidden, "user_agent": hidden["user_agent"]})
    shown = browser.launch_options(Path("/p"), "visible")
    assert shown["headless"] is False and shown["viewport"] is None and shown["user_agent"] is None
    matched = browser.launch_options(Path("/p"), "visible", match_headless=True)
    assert matched["headless"] is False and matched["viewport"] == hidden["viewport"] == {"width": 1280, "height": 900}
    assert matched["user_agent"] is None
    assert browser.launch_options(Path("/p"), "visible", guarded=False)["service_workers"] == "allow"
    with pytest.raises(ValueError):
        browser.launch_options(Path("/p"), "offscreen")
    root = Path(browser.__file__).parents[1]
    for path in root.rglob("*.py"):
        text = path.read_text()
        for marker in ("headless=False", 'mode="visible")', 'mode="visible",'):
            for chunk in text.split(marker)[:-1]:  # a literal visible request: only ever inside a login function
                function = chunk.rsplit("\ndef ", 1)[-1].rsplit("\nasync def ", 1)[-1]
                assert function.startswith("login("), (path, marker, function.split("\n", 1)[0])
        if path.name != "browser.py":
            assert "launch_persistent_context(" not in text, path  # nobody launches Chrome but core.browser


def user_agent_override(context) -> dict:
    """What present_as_chrome set on the command's page (Emulation.setUserAgentOverride), exactly once."""
    [params] = [params for method, params in context.cdp if method == "Emulation.setUserAgentOverride"]
    return params


AUTO_ATTACH = ("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": False})


def test_chatgpt_login_window_presents_what_headless_commands_do(fake_chrome, monkeypatch):
    """The cf_clearance a person earns in the login window must hold up headless: both present the same user agent,
    client hints (no HeadlessChrome brand anywhere), viewport, and frame/worker override."""
    launches, contexts = fake_chrome
    monkeypatch.setattr(browser, "identity", Identity("chatgpt:default"))
    assert browser.run(chatgpt_fetch.status()) == chatgpt_fetch.signed_out()  # a headless command
    with pytest.raises(AuthRequired, match="Timed out after 0s"):
        browser.run(chatgpt_fetch.login(0))  # the login window, not waiting for a person

    (hidden, shown), (headless_page, login_window) = launches, contexts
    assert hidden["headless"] is True and shown["headless"] is False
    assert shown["viewport"] == hidden["viewport"] == {"width": 1280, "height": 900}
    override = user_agent_override(login_window)
    assert override == user_agent_override(headless_page)
    assert override["userAgent"] == ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                                     "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
    metadata = override["userAgentMetadata"]
    assert [b["brand"] for b in metadata["brands"]] == ["Google Chrome", "Chromium", "Not(A:Brand"]
    assert [b["brand"] for b in metadata["fullVersionList"]] == ["Google Chrome", "Chromium", "Not(A:Brand"]
    assert (metadata["platform"], metadata["mobile"]) == ("macOS", False)
    assert AUTO_ATTACH in login_window.cdp and AUTO_ATTACH in headless_page.cdp


def test_gemini_and_muse_sign_in_windows_stay_native(fake_chrome, monkeypatch):
    """Only chatgpt's windows match headless: Google's and Meta's sign-in windows keep Chrome's own everything."""
    launches, contexts = fake_chrome
    for ident, login in (("google:default", gemini_fetch.login), ("meta:default", muse_fetch.login)):
        monkeypatch.setattr(browser, "identity", Identity(ident))
        with pytest.raises(AuthRequired, match="Timed out after 0s"):
            browser.run(login(0))
    assert len(launches) == 2
    for options, context in zip(launches, contexts):
        assert options["headless"] is False and options["viewport"] is None and options["user_agent"] is None
        assert context.cdp == []
