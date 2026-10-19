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
from webcli.core.identities import Identity
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
    assert shown["headless"] is False and shown["viewport"] is None
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
