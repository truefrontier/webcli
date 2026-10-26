"""What keep, photos, youtube, recorder, contacts, gmail, and notebooklm share:
how they register, that no verb ever asks for a visible Chrome,
that each verb exists with its options, that secrets never reach output, errors, or logs, that the identity daemon
serves them only on a google identity, and the shared download and save helpers.

The headless test mirrors test_no_visible_window.py: each verb runs end to end through the real CLI, the identity
layer, the daemon client (in-process), the site's fetch code, and core.browser, into a fake Playwright that records
launch_persistent_context's arguments and stops there.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from webcli import sites
from webcli.apps import webcli as webcli_app
from webcli.core import browser, config, daemon, identities
from webcli.core.errors import AuthRequired, ConfigError
from webcli.core.identities import Identity
from webcli.sites import google
from webcli.sites.photos.guard import POLICY as PHOTOS, download_url_allowed

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from conftest import LaunchStopped, make_profile
from google_fakes import APIResponse, ScriptedPage
from test_no_visible_window import assert_hidden

SITES = ("keep", "photos", "youtube", "recorder", "contacts", "gmail", "notebooklm")
VERBS = {
    "keep": {"list", "get", "info", "search", "status", "agent-help"},
    "photos": {"list", "search", "info", "albums", "download", "status", "agent-help"},
    "youtube": {"history", "status", "playlist", "subs", "uploads", "home", "agent-help"},
    "recorder": {"list", "info", "search", "transcript", "download", "sync", "status", "agent-help"},
    "contacts": {"list", "get", "search", "other", "labels", "export", "status", "label", "agent-help"},
    "gmail": {"filters", "status", "agent-help"},
    "notebooklm": {"status", "notebooks", "sources", "source", "notes", "artifacts", "ask", "generate", "download",
                   "create", "agent-help"},
}
SUBVERBS = {("contacts", "label"): {"add", "remove"}, ("gmail", "filters"): {"list", "create", "delete"},
            ("youtube", "playlist"): {"create"}, ("notebooklm", "source"): {"get", "add"},
            ("notebooklm", "generate"): {"kinds", "audio", "video", "report", "quiz", "flashcards", "mind-map",
                                         "infographic", "slide-deck", "data-table"}}
ROWS = {"--format", "--compact", "--select", "--csv", "--quiet"}
RECORD = {"--format", "--quiet"}
WRITE = {"--dry-run", "--confirm", "--yes"} | RECORD
SELECTORS = {"--from-label", "--query", "--ids"}
ADD_SOURCES = {"--file", "--url", "--urls-file", "--text", "--title", "--wait", "--timeout", "--interval",
               "--json"} | WRITE
GENERATE = {"--source", "--language", "--instructions", "--wait", "--timeout", "--interval", "--json"} | WRITE
# (site, verb, arguments, options)
WIRING = [
    ("keep", "list", [], {"--limit", "--archived", "--id-only"} | ROWS),
    ("keep", "search", ["QUERY"], {"--limit", "--archived", "--id-only"} | ROWS),
    ("keep", "info", ["NOTE_ID"], ROWS),
    ("keep", "get", ["NOTE_ID"], {"--output", "--quiet"}),
    ("keep", "status", [], RECORD),
    ("photos", "list", [], {"--album", "--limit", "--id-only"} | ROWS),
    ("photos", "search", ["QUERY"], {"--limit", "--id-only"} | ROWS),
    ("photos", "info", ["PHOTO"], ROWS),
    ("photos", "albums", [], {"--id-only"} | ROWS),
    ("photos", "download", ["PHOTO"], {"--output"} | RECORD),
    ("photos", "status", [], RECORD),
    ("youtube", "history", [], {"--limit", "--since", "--source"} | ROWS),
    ("youtube", "status", [], RECORD),
    ("youtube", "subs", [], {"--json"} | ROWS),
    ("youtube", "uploads", [], {"--since", "--limit", "--include-shorts", "--json"} | ROWS),
    ("youtube", "home", [], {"--limit", "--include-shorts", "--json"} | ROWS),
    ("recorder", "list", [], {"--id-only"} | ROWS),
    ("recorder", "info", ["RECORDING_ID"], ROWS),
    ("recorder", "search", ["QUERY"], {"--id-only"} | ROWS),
    ("recorder", "transcript", ["RECORDING_ID"], {"--output"} | RECORD),
    ("recorder", "download", ["RECORDING_ID"], {"--output"} | RECORD),
    ("recorder", "sync", [], {"--output"} | RECORD),
    ("recorder", "status", [], RECORD),
    ("contacts", "list", [], {"--limit", "--label"} | ROWS),
    ("contacts", "other", [], {"--limit"} | ROWS),
    ("contacts", "search", ["QUERY"], {"--limit"} | ROWS),
    ("contacts", "get", ["ID|EMAIL|PHONE"], RECORD),
    ("contacts", "labels", [], ROWS),
    ("contacts", "export", [], {"--format", "--out", "--label", "--limit", "--quiet"}),
    ("contacts", "status", [], RECORD),
    ("contacts", "label add", ["LABEL"], SELECTORS | {"--create-missing"} | WRITE),
    ("contacts", "label remove", ["LABEL"], SELECTORS | WRITE),
    ("gmail", "filters list", [], {"--limit"} | ROWS),
    ("gmail", "filters create", [], {"--from", "--to", "--subject", "--query", "--negated-query", "--has-attachment",
                                     "--skip-inbox", "--mark-as-read", "--star", "--delete-it", "--never-spam"} | WRITE),
    ("gmail", "filters delete", ["FILTER_ID"], WRITE),
    ("gmail", "status", [], RECORD),
    ("notebooklm", "status", ["[ARTIFACT]"], {"--notebook", "--json"} | RECORD),
    ("notebooklm", "notebooks", [], {"--json"} | ROWS),
    ("notebooklm", "sources", ["NOTEBOOK"], {"--json"} | ROWS),
    ("notebooklm", "source get", ["SOURCE"], {"--notebook", "--guide", "--output", "--json"} | RECORD),
    ("notebooklm", "source add", ["NOTEBOOK"], ADD_SOURCES),
    ("notebooklm", "create", ["TITLE"], ADD_SOURCES),
    ("notebooklm", "notes", ["NOTEBOOK"], {"--json"} | ROWS),
    ("notebooklm", "artifacts", ["NOTEBOOK"], {"--json"} | ROWS),
    ("notebooklm", "ask", ["NOTEBOOK", "QUESTION"], {"--source", "--json"} | RECORD),
    ("notebooklm", "download", ["ARTIFACT"], {"--output", "--notebook", "--json"} | RECORD),
    ("notebooklm", "generate audio", ["NOTEBOOK"], {"--style", "--length"} | GENERATE),
    ("notebooklm", "generate video", ["NOTEBOOK"], {"--style", "--visual", "--visual-prompt"} | GENERATE),
    ("notebooklm", "generate report", ["NOTEBOOK"], {"--style"} | GENERATE),
    ("notebooklm", "generate quiz", ["NOTEBOOK"], {"--quantity", "--difficulty"} | GENERATE),
    ("notebooklm", "generate flashcards", ["NOTEBOOK"], {"--quantity", "--difficulty"} | GENERATE),
    ("notebooklm", "generate mind-map", ["NOTEBOOK"], GENERATE),
    ("notebooklm", "generate infographic", ["NOTEBOOK"], {"--orientation", "--detail", "--visual"} | GENERATE),
    ("notebooklm", "generate slide-deck", ["NOTEBOOK"], {"--style", "--length"} | GENERATE),
    ("notebooklm", "generate data-table", ["NOTEBOOK"], GENERATE),
]
HIDDEN_COMMANDS = [
    ("keep", "list"), ("keep", "search", "picnic"), ("keep", "info", "abc"), ("keep", "get", "abc"),
    ("keep", "status"),
    ("photos", "list"), ("photos", "list", "--album", "AF1QipAlbum"), ("photos", "search", "dogs"),
    ("photos", "info", "AF1QipPhoto"), ("photos", "albums"), ("photos", "download", "AF1QipPhoto", "-o", "{tmp}"),
    ("photos", "status"),
    ("youtube", "history"), ("youtube", "history", "--source", "youtube", "--since", "2026-10-01"),
    ("youtube", "history", "--source", "myactivity"), ("youtube", "status"),
    ("recorder", "list"), ("recorder", "info", "r1"), ("recorder", "search", "standup"),
    ("recorder", "transcript", "r1", "-o", "{tmp}"), ("recorder", "download", "r1", "-o", "{tmp}"),
    ("recorder", "sync", "-o", "{tmp}"), ("recorder", "status"),
    ("contacts", "list"), ("contacts", "list", "--label", "Newsletter"), ("contacts", "other"),
    ("contacts", "search", "ada"), ("contacts", "get", "c123"), ("contacts", "get", "ada@example.com"),
    ("contacts", "labels"), ("contacts", "export"), ("contacts", "status"),
    ("contacts", "label", "add", "Newsletter", "--ids", "c123"),
    ("contacts", "label", "remove", "Newsletter", "--from-label", "Old"),
    # --confirm too: the fake stops at the launch, so nothing is written, and a write's Chrome is hidden as well
    ("contacts", "label", "add", "Newsletter", "--query", "ada", "--confirm"),
    ("gmail", "status"), ("gmail", "filters", "list"), ("gmail", "filters", "delete", "1234567890123"),
    ("gmail", "filters", "delete", "1234567890123", "--confirm"),
    ("gmail", "filters", "create", "--from", "a@example.com", "--star", "--confirm"),
    ("notebooklm", "status"), ("notebooklm", "notebooks"), ("notebooklm", "sources", "nb1"),
    ("notebooklm", "source", "get", "s1"), ("notebooklm", "notes", "nb1"), ("notebooklm", "artifacts", "nb1"),
    ("notebooklm", "ask", "nb1", "What is it?"), ("notebooklm", "download", "a1", "-o", "{tmp}/"),
    ("notebooklm", "generate", "audio", "nb1"), ("notebooklm", "generate", "report", "nb1", "--confirm"),
]
# Synthetic values in the shapes these sites carry. None of these is real.
PSID = "g.a000testPSIDvalue0123456789abcdefFAKE0076"
SAPISIDHASH = "1760450400_0123456789abcdef0123456789abcdef01234567"
SIGNED = "https://photos.fife.usercontent.google.com/pw/AF1QipFAKEsignedMediaKey0123456789abcdefXYZ=d"
SECRETS = [PSID, SAPISIDHASH, "AF1QipFAKEsignedMediaKey0123456789abcdefXYZ"]


def run(*args):
    return CliRunner().invoke(webcli_app.build(), list(args))


def leaked(result) -> list[str]:
    logs = "".join(p.read_text() for p in (config.home() / "logs").glob("*.log"))
    return [s for s in SECRETS if s in result.stdout + result.stderr + logs]


# Registration.

@pytest.mark.parametrize("name", SITES)
def test_each_site_registers_as_a_google_data_site_in_both_places(name):
    entry = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]["entry-points"]
    target = f"webcli.sites.{name}:ADAPTER"
    assert entry["webcli.sites"][name] == target and sites.BUILTIN[name] == target
    site = sites.get(name)
    assert (site.kind, site.provider, site.status_op, site.login) == ("data", "google", "status", None)
    assert site.describe()["binary"] == "webcli" and site.describe()["defaultIdentity"] == "google:default"
    assert "status" in site.ops
    fetch = importlib.import_module(site.fetch)
    for op in site.ops:  # every daemon op takes headed= (and ignores it: see the headless tests)
        assert inspect.signature(getattr(fetch, op)).parameters["headed"].default is False
    assert fetch.POLICY.site == name and fetch.APP_URL.startswith("https://")  # what `capture` needs


def test_they_sign_in_through_gemini_and_add_no_login_of_their_own():
    assert identities.login_site(Identity("google:default"), list(sites.registry().values())).name == "gemini"
    for name in SITES:
        assert "login" not in sites.get(name).cli().commands


def test_webcli_sites_lists_them_under_webcli():
    import json

    result = run("sites")
    listed = {s["name"]: s for s in json.loads(result.stdout)["webcli"]}
    assert set(SITES) <= set(listed)
    assert all(listed[name]["provider"] == "google" for name in SITES)


# Headless only.

@pytest.mark.parametrize("command", HIDDEN_COMMANDS, ids=" ".join)
def test_every_verb_launches_hidden_chrome_only(fake_playwright, tmp_path, command):
    make_profile("google:default")
    result = run(*[part.replace("{tmp}", str(tmp_path / "out")) for part in command])
    assert result.exit_code == 5, result.output  # the fake stops at the launch: a site error
    assert len(fake_playwright) == 1
    assert_hidden(fake_playwright[0])
    assert fake_playwright[0]["user_data_dir"] == str(Identity("google:default").profile)


@pytest.mark.parametrize("name", SITES)
def test_no_verb_offers_headed_and_headed_true_still_runs_headless(fake_playwright, monkeypatch, name):
    todo = list(sites.get(name).cli().commands.values())
    while todo:  # subgroups too: contacts label, gmail filters
        verb = todo.pop()
        todo += getattr(verb, "commands", {}).values()
        assert not any("--headed" in p.opts for p in verb.params), verb.name
    monkeypatch.setattr(browser, "identity", Identity("google:default"))
    fetch = importlib.import_module(sites.get(name).fetch)
    with pytest.raises(LaunchStopped):
        browser.run(fetch.status(headed=True))
    assert_hidden(fake_playwright[0])


# Command wiring.

@pytest.mark.parametrize("name", SITES)
def test_each_site_has_exactly_its_verbs(name):
    group = sites.get(name).cli()
    assert set(group.commands) == VERBS[name]
    assert {"--identity", "--lock-wait", "--version"} <= {o for p in group.params for o in p.opts}
    for (site, sub), verbs in SUBVERBS.items():
        if site == name:
            assert set(group.commands[sub].commands) == verbs


def command(name: str, verb: str):
    found = sites.get(name).cli()
    for part in verb.split():
        found = found.commands[part]
    return found


def test_recorder_ships_no_mcp_and_no_skill():
    commands = sites.get("recorder").cli().commands
    assert "mcp" not in commands and "skill" not in commands
    root = Path(__file__).parents[1]
    assert "recorder-mcp" not in (root / "pyproject.toml").read_text()
    for path in (root / "webcli" / "sites" / "recorder").glob("*.py"):
        assert "fastmcp" not in path.read_text() and "mcp_server" not in path.read_text()


@pytest.mark.parametrize(("name", "verb", "args", "options"), WIRING, ids=lambda v: v if isinstance(v, str) else "")
def test_each_verb_has_its_arguments_and_options(name, verb, args, options):
    found = command(name, verb)
    assert [p.human_readable_name for p in found.params if p.param_type_name == "argument"] == args
    flags = {o for p in found.params if p.param_type_name == "option" for o in p.opts if o.startswith("--")}
    assert flags == options


def test_notable_defaults_and_choices():
    group = {name: sites.get(name).cli() for name in SITES}
    history = {p.name: p for p in group["youtube"].commands["history"].params}
    assert list(history["source"].type.choices) == ["auto", "myactivity", "youtube"]
    assert history["source"].default == "auto" and history["limit"].default is None
    sync = {p.name: p for p in group["recorder"].commands["sync"].params}
    assert sync["out"].default == "./recorder-backup" and "-o" in sync["out"].opts
    assert {p.name: p for p in group["photos"].commands["list"].params}["limit"].default == 50
    assert {p.name: p for p in group["keep"].commands["search"].params}["limit"].default == 50
    info_format = {p.name: p for p in group["keep"].commands["info"].params}["fmt"]
    assert "text" in info_format.type.choices
    for name in SITES:
        result = run(name, "--help")
        assert result.exit_code == 0 and "Identity google:default" in " ".join(result.output.split())


def test_bad_ids_are_usage_errors_before_any_chrome(fake_playwright):
    assert run("photos", "info", "not-a-key").exit_code == 2
    assert run("photos", "list", "--album", "https://example.com/nothing").exit_code == 2
    assert run("youtube", "history", "--since", "last tuesday").exit_code == 2
    assert fake_playwright == []


# Redaction.

@pytest.mark.parametrize("args", [("photos", "download", "AF1QipX", "-o", "{tmp}"), ("keep", "list"),
                                  ("youtube", "history"), ("recorder", "sync", "-o", "{tmp}"), ("contacts", "list"),
                                  ("contacts", "label", "add", "L", "--ids", "c1", "--confirm"),
                                  ("gmail", "filters", "list"), ("gmail", "filters", "delete", "123", "--confirm"),
                                  ("notebooklm", "ask", "nb1", "Q?"), ("notebooklm", "download", "a1", "-o", "{tmp}/"),
                                  ("notebooklm", "generate", "audio", "nb1", "--confirm")])
def test_cookies_tokens_and_signed_links_never_reach_output_errors_or_logs(monkeypatch, tmp_path, args):
    async def failing(*_args, **_kwargs):
        raise RuntimeError(f"Fetching failed at {SIGNED}; Cookie: __Secure-1PSID={PSID}; "
                           f"Authorization: SAPISIDHASH {SAPISIDHASH}")

    monkeypatch.setattr(daemon, "call", failing)
    result = run(*[a.replace("{tmp}", str(tmp_path)) for a in args])
    assert result.exit_code == 5
    assert "<redacted>" in result.stderr and leaked(result) == []


def test_sign_in_needed_exits_4_with_the_identity_login_hint(monkeypatch):
    async def signed_out(*_args, **_kwargs):
        raise AuthRequired("keep.google.com sent Chrome to accounts.google.com.")

    monkeypatch.setattr(daemon, "call", signed_out)
    result = run("keep", "list")
    assert result.exit_code == 4 and "webcli identity login google:default" in result.stderr


def test_blocked_request_descriptions_hide_signed_links():
    class Request:
        method, url, post_data = "GET", SIGNED, None

    assert PHOTOS.describe(Request()) == "GET photos.fife.usercontent.google.com/<redacted>"


# The identity daemon.

def test_the_google_daemon_serves_their_listed_ops_only():
    for name in SITES:
        site = sites.get(name)
        for op in site.ops:
            assert daemon.site_op(Identity("google:default"), name, op)
        with pytest.raises(ConfigError):
            daemon.site_op(Identity("chatgpt:default"), name, "status")
        with pytest.raises(ConfigError):
            daemon.site_op(Identity("google:default"), name, "delete")


# Shared helpers.

@pytest.mark.parametrize(("method", "url", "allowed"), [
    ("GET", "https://www.gstatic.com/x.js", True),
    ("GET", "https://lh3.googleusercontent.com/a/avatar", True),
    ("GET", "https://accounts.google.com/CheckCookie", True),
    ("POST", "https://accounts.google.com/RotateCookies", True),
    ("GET", "https://accounts.google.com/Logout", False),
    ("GET", "https://www.google.com/accounts/Logout?continue=x", False),
    ("GET", "http://www.gstatic.com/x.js", False),
    ("GET", "https://www.gstatic.com:8443/x.js", False),
    ("GET", "https://user@www.gstatic.com/x.js", False),
    ("GET", "https://www.google-analytics.com/collect", False),
    ("GET", "https://gstatic.com.evil.example/x.js", False),
    ("POST", "https://play.google.com/log?format=json", False),
    ("POST", "https://www.gstatic.com/x.js", False),
    ("DELETE", "https://accounts.google.com/", False),
])
def test_the_shared_google_rules(method, url, allowed):
    assert google.allowed(method, url, ()) is allowed


def test_fetch_file_checks_every_hop_before_fetching_it():
    first = "https://photos.fife.usercontent.google.com/pw/abc=d"
    hop = "https://video-downloads.googleusercontent.com/v/1"
    page = ScriptedPage(files={first: APIResponse(302, headers={"location": hop}),
                               hop: APIResponse(200, b"original", {"content-type": "video/mp4"})})
    status, headers, body = asyncio.run(google.fetch_file(page, first, download_url_allowed, what="the original"))
    assert (status, body, page.fetched) == (200, b"original", [first, hop])
    assert all(response.disposed for response in page.files.values())  # no body left in the daemon's memory

    away = ScriptedPage(files={first: APIResponse(302, headers={"location": "https://evil.example/x"})})
    with pytest.raises(RuntimeError, match=r"Refused to fetch the original from GET evil\.example/x"):
        asyncio.run(google.fetch_file(away, first, download_url_allowed, what="the original"))
    assert away.fetched == [first]  # the off-list hop was never requested

    loop = ScriptedPage(files={first: APIResponse(302, headers={"location": first})})
    with pytest.raises(RuntimeError, match="redirected more than 5 times"):
        asyncio.run(google.fetch_file(loop, first, download_url_allowed, what="the original"))


def test_save_file_is_private_atomic_and_never_follows_a_symlink(tmp_path):
    target = tmp_path / "elsewhere.txt"
    target.write_text("keep me")
    (tmp_path / "photo.jpg").symlink_to(target)
    path = google.save_file(tmp_path, "photo.jpg", b"jpeg bytes")
    assert path.read_bytes() == b"jpeg bytes" and not path.is_symlink() and target.read_text() == "keep me"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["elsewhere.txt", "photo.jpg"]  # no .part left behind
    for bad in ("../escape.jpg", "..", ""):
        with pytest.raises(ValueError):
            google.save_file(tmp_path, bad, b"x")


@pytest.mark.parametrize(("text", "expected"), [
    ("Team standup 10/19", "Team standup 10_19"), ("../../etc/passwd", ".._.._etc_passwd"), ("..", "fallback"),
    ("", "fallback"), (None, "fallback"), ("Café über", "Café über"),
])
def test_safe_name(text, expected):
    assert google.safe_name(text, "fallback") == expected
