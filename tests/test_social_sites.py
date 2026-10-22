"""What linkedin, facebook, loom, and messages share: how they register (each on its own identity with its own login,
none on Muse's meta:default), that no verb ever asks for a visible Chrome, that each verb exists with its
options, that secrets never reach output, errors, or logs, and that the identity daemon serves them only on their own
provider.

The headless test mirrors test_no_visible_window.py: each verb runs end to end through the real CLI, the identity
layer, the daemon client (in-process), the site's fetch code, and core.browser, into a fake Playwright that records
launch_persistent_context's arguments and stops there. Nothing is ever sent or posted: the --confirm cases stop at
the launch too.
"""

from __future__ import annotations

import importlib
import inspect
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from webcli import sites
from webcli.apps import webcli as webcli_app
from webcli.core import browser, config, daemon, identities
from webcli.core.errors import AuthRequired, ConfigError
from webcli.core.identities import Identity

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from conftest import LaunchStopped, make_profile
from test_no_visible_window import assert_hidden

SITES = {"linkedin": "linkedin", "facebook": "facebook", "loom": "loom", "messages": "messages"}
VERBS = {
    "linkedin": {"messages", "activity", "boost", "post", "feed", "posts", "status", "agent-help"},
    "facebook": {"sync", "search", "about", "enrich", "export", "status", "agent-help"},
    "loom": {"list", "info", "transcript", "status", "agent-help"},
    "messages": {"list", "threads", "read", "search", "send", "status", "agent-help"},
}
SUBVERBS = {("linkedin", "messages"): {"list", "history", "read", "send"},
            ("linkedin", "activity"): {"comments", "reactions", "posts", "export"},
            ("linkedin", "boost"): {"list", "stats"}, ("linkedin", "post"): {"create"},
            ("linkedin", "posts"): {"search", "from", "read"}}
ROWS = {"--format", "--compact", "--select", "--csv", "--quiet"}
RECORD = {"--format", "--quiet"}
WINDOW = {"--since", "--until"}
WIRING = [
    ("linkedin", "status", [], {"--expect-author"} | RECORD),
    ("linkedin", "messages list", [], {"--limit"} | ROWS),
    ("linkedin", "messages history", ["THREAD_OR_PERSON"], {"--limit"} | ROWS),
    ("linkedin", "messages read", ["THREAD_OR_PERSON"], {"--limit"} | ROWS),
    ("linkedin", "messages send", ["THREAD_OR_PERSON"],
     {"--text", "--limit", "--expect-author", "--dry-run", "--confirm", "--yes", "--send", "--quiet"}),
    ("linkedin", "activity comments", [], {"--limit", "--since"} | ROWS),
    ("linkedin", "activity reactions", [], {"--limit", "--since"} | ROWS),
    ("linkedin", "activity posts", [], {"--limit", "--since"} | ROWS),
    ("linkedin", "activity export", [], {"--limit", "--since", "--out", "--format", "--compact", "--select", "--quiet",
                                         "--csv"}),
    ("linkedin", "feed", [], {"--limit", "--since", "--ranked"} | ROWS),
    ("linkedin", "posts search", ["QUERY"], {"--limit", "--since", "--sort"} | ROWS),
    ("linkedin", "posts from", ["HANDLE_OR_URL"], {"--limit", "--since"} | ROWS),
    ("linkedin", "posts read", ["URL_OR_URN"], {"--comments"} | ROWS),
    ("linkedin", "boost list", [], WINDOW | {"--account"} | ROWS),
    ("linkedin", "boost stats", ["TARGET"], WINDOW | ROWS),
    ("linkedin", "post create", [], {"--text", "--text-file", "--image", "--expect-author", "--dry-run", "--confirm",
                                     "--yes", "--format", "--quiet"}),
    ("facebook", "status", [], RECORD),
    ("facebook", "sync", [], {"--limit"} | RECORD),
    ("facebook", "about", ["ID|URL"], {"--cached"} | RECORD),
    ("facebook", "enrich", ["ID|URL"], {"--refresh", "--no-web"} | RECORD),
    ("facebook", "search", [], {"--name", "--city", "--employer", "--keyword", "--limit", "--enrich", "--no-web"} | ROWS),
    ("facebook", "export", [], {"--format", "--out", "--quiet"}),
    ("loom", "status", [], RECORD),
    ("loom", "list", [], {"--limit", "--id-only"} | ROWS),
    ("loom", "info", ["VIDEO"], ROWS),
    ("loom", "transcript", ["VIDEO"], {"--output"} | ROWS),
    ("messages", "status", [], RECORD),
    ("messages", "list", [], {"--limit"} | ROWS),
    ("messages", "threads", [], {"--limit"} | ROWS),
    ("messages", "read", ["QUERY"], {"--limit", "--image-out"} | ROWS),
    ("messages", "search", ["QUERY"], {"--limit"} | ROWS),
    ("messages", "send", [], {"--to", "--text", "--dry-run", "--confirm", "--yes"} | RECORD),
]
VID = "0123456789abcdef0123456789abcdef"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (2).to_bytes(4, "big") + (3).to_bytes(4, "big") + b"\x08\x06"
HIDDEN_COMMANDS = [
    ("loom", "status"), ("loom", "list"), ("loom", "list", "--limit", "3"), ("loom", "info", VID),
    ("loom", "transcript", f"https://www.loom.com/share/{VID}"),
    ("facebook", "status"), ("facebook", "sync"), ("facebook", "sync", "--limit", "5"), ("facebook", "about", "jane.doe"),
    ("facebook", "enrich", "jane.doe"),
    ("linkedin", "status"), ("linkedin", "messages", "list"), ("linkedin", "messages", "history", "Boardy"),
    ("linkedin", "messages", "read", "2-abc"), ("linkedin", "activity", "comments"),
    ("linkedin", "activity", "reactions", "--since", "2026-10-01"), ("linkedin", "activity", "posts"),
    ("linkedin", "activity", "export"), ("linkedin", "boost", "list"),
    ("linkedin", "boost", "stats", "urn:li:share:7514057966338318336"),
    ("linkedin", "messages", "send", "Boardy", "--text", "hi"),
    ("linkedin", "post", "create", "--text", "hi", "--image", "{png}"),
    # --confirm too: the fake stops at the launch, so nothing is sent or posted, and a write's Chrome is hidden too
    ("linkedin", "messages", "send", "Boardy", "--text", "hi", "--confirm"),
    ("linkedin", "post", "create", "--text", "hi", "--image", "{png}", "--confirm"),
    ("messages", "status"), ("messages", "list"), ("messages", "threads", "--limit", "3"), ("messages", "read", "Alice"),
    ("messages", "search", "ali"), ("messages", "send", "--to", "Alice", "--text", "hi"),
    ("messages", "send", "--to", "Alice", "--text", "hi", "--confirm"),
]
# Synthetic values in the shapes these sites carry. None of these is real.
LI_AT = "AQEDAR0FAKEliatSessionValue0123456789abcdefXYZ"
JSESSION = "ajax:1234567890123456789"
SIGNED = ("https://cdn.loom.com/mediametadata/transcription/0123456789abcdef0123456789abcdef-1.json?Policy="
          "eyJTdGF0ZW1lbnQiOlt7IlJlc291cmNlIjoiaHR0cHM6Ly9jZG4ubG9vbS5jb20vKiJ9XX0_&Signature=FAKEsig0123456789abcdef"
          "XYZ~&Key-Pair-Id=APKAFAKE0123456789")
SECRETS = [LI_AT, JSESSION, "FAKEsig0123456789abcdefXYZ", "APKAFAKE0123456789", "NAcMfakeDTSG0123456789:12",
           "100000000000001", "FAKExsValue0123456789"]


def run(*args):
    return CliRunner().invoke(webcli_app.build(), list(args))


def leaked(result) -> list[str]:
    logs = "".join(p.read_text() for p in (config.home() / "logs").glob("*.log"))
    return [s for s in SECRETS if s in result.stdout + result.stderr + logs]


@pytest.fixture
def png(tmp_path) -> Path:
    path = tmp_path / "card.png"
    path.write_bytes(PNG)
    return path


@pytest.fixture
def profiles():
    return [make_profile(f"{provider}:default") for provider in SITES.values()]


def command(name: str, verb: str):
    found = sites.get(name).cli()
    for part in verb.split():
        found = found.commands[part]
    return found


# Registration.

@pytest.mark.parametrize("name", SITES)
def test_each_site_registers_as_a_data_site_in_both_places(name):
    entry = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]["entry-points"]
    target = f"webcli.sites.{name}:ADAPTER"
    assert entry["webcli.sites"][name] == target and sites.BUILTIN[name] == target
    site = sites.get(name)
    assert (site.kind, site.provider, site.status_op) == ("data", SITES[name], "status")
    assert site.describe()["binary"] == "webcli" and site.describe()["defaultIdentity"] == f"{SITES[name]}:default"
    fetch = importlib.import_module(site.fetch)
    for op in site.ops:  # every daemon op takes headed= (and ignores it: see the headless tests)
        assert inspect.signature(getattr(fetch, op)).parameters["headed"].default is False
    assert fetch.POLICY.site == name and fetch.APP_URL.startswith("https://")  # what `capture` needs


def test_each_brings_its_own_login_and_muse_keeps_meta():
    every = list(sites.registry().values())
    for name in SITES:
        assert sites.get(name).login is not None
        assert identities.login_site(Identity(f"{name}:default"), every).name == name
    assert identities.login_site(Identity("meta:default"), every).name == "muse"
    assert (sites.get("muse").provider, sites.get("facebook").provider) == ("meta", "facebook")
    for name in SITES:
        assert "login" not in sites.get(name).cli().commands  # sign-in is `webcli identity login`, a person's job


def test_webcli_sites_lists_all_four_under_webcli():
    listed = {s["name"]: s for s in json.loads(run("sites").stdout)["webcli"]}
    assert {name: listed[name]["provider"] for name in SITES} == SITES


@pytest.mark.parametrize("name", SITES)
def test_help_works_and_names_the_identity(name):
    result = run(name, "--help")
    assert result.exit_code == 0 and f"Identity {SITES[name]}:default" in " ".join(result.output.split())
    reference = json.loads(run(name, "agent-help", "--format", "json").stdout)
    assert reference["writes"] and reference["notes"]



@pytest.fixture(autouse=True)
def _linkedin_author():
    # The package ships no personal default author; real users configure one. Cross-site wiring tests that
    # exercise LinkedIn writes/launch/profile behavior configure kevnk so those paths run (the guard itself is
    # covered in test_linkedin.py).
    config.save({"sites": {"linkedin": {"expect_author": "kevnk"}}})
    yield


# Headless only.

@pytest.mark.parametrize("command_", HIDDEN_COMMANDS, ids=" ".join)
def test_every_verb_launches_hidden_chrome_only(fake_playwright, profiles, png, command_):
    result = run(*[part.replace("{png}", str(png)) for part in command_])
    assert result.exit_code == 5, result.output  # the fake stops at the launch: a site error
    assert len(fake_playwright) == 1
    assert_hidden(fake_playwright[0])
    assert fake_playwright[0]["user_data_dir"] == str(Identity(f"{SITES[command_[0]]}:default").profile)


@pytest.mark.parametrize("name", SITES)
def test_no_verb_offers_headed_and_headed_true_still_runs_headless(fake_playwright, monkeypatch, name):
    todo = list(sites.get(name).cli().commands.values())
    while todo:  # subgroups too
        verb = todo.pop()
        todo += getattr(verb, "commands", {}).values()
        assert not any("--headed" in p.opts for p in verb.params), verb.name
    monkeypatch.setattr(browser, "identity", Identity(f"{SITES[name]}:default"))
    fetch = importlib.import_module(sites.get(name).fetch)
    with pytest.raises(LaunchStopped):
        browser.run(fetch.status(headed=True))
    assert_hidden(fake_playwright[0])


@pytest.mark.parametrize("ident", ["linkedin:default", "loom:default", "facebook:default", "messages:default"])
def test_identity_login_is_the_only_visible_path(fake_playwright, ident):
    result = run("identity", "login", ident)
    assert result.exit_code == 5
    options = fake_playwright[0]
    assert options["headless"] is False and options["user_data_dir"] == str(Identity(ident).profile)
    assert {"--use-mock-keychain", "--enable-automation"} <= set(options["ignore_default_args"])


@pytest.mark.parametrize("args", [("loom", "list"), ("loom", "info", VID), ("loom", "transcript", VID),
                                  ("facebook", "sync"), ("facebook", "about", "jane.doe"),
                                  ("linkedin", "messages", "list"), ("linkedin", "activity", "export"),
                                  ("linkedin", "boost", "list"), ("linkedin", "messages", "send", "x", "--text", "hi"),
                                  ("messages", "list"), ("messages", "read", "Alice"), ("messages", "search", "ali"),
                                  ("messages", "send", "--to", "Alice", "--text", "hi", "--confirm")])
def test_no_profile_exits_4_without_starting_chrome(fake_playwright, args):
    result = run(*args)
    assert result.exit_code == 4 and "webcli identity login" in result.stderr
    assert fake_playwright == []


@pytest.mark.parametrize("name", SITES)
def test_status_without_a_profile_is_signed_out_without_chrome(fake_playwright, name):
    result = run(name, "status")
    assert result.exit_code == 4 and json.loads(result.stdout)["signedIn"] is False
    assert fake_playwright == []


# Command wiring.

@pytest.mark.parametrize("name", SITES)
def test_each_site_has_exactly_its_verbs(name):
    group = sites.get(name).cli()
    assert set(group.commands) == VERBS[name]
    assert {"--identity", "--lock-wait", "--version"} <= {o for p in group.params for o in p.opts}
    for (site, sub), verbs in SUBVERBS.items():
        if site == name:
            assert set(group.commands[sub].commands) == verbs


@pytest.mark.parametrize(("name", "verb", "args", "options"), WIRING, ids=lambda v: v if isinstance(v, str) else "")
def test_each_verb_has_its_arguments_and_options(name, verb, args, options):
    found = command(name, verb)
    assert [p.human_readable_name for p in found.params if p.param_type_name == "argument"] == args
    flags = {o for p in found.params if p.param_type_name == "option" for o in p.opts if o.startswith("--")}
    assert flags == options


def test_notable_defaults():
    assert {p.name: p for p in command("linkedin", "messages send").params}["limit"].default == 50
    assert {p.name: p for p in command("linkedin", "messages list").params}["limit"].default == 20
    assert {p.name: p for p in command("facebook", "search").params}["limit"].default == 50
    assert command("linkedin", "messages read") is command("linkedin", "messages history")  # an alias
    assert command("messages", "threads") is command("messages", "list")  # an alias
    transcript_format = {p.name: p for p in command("loom", "transcript").params}["fmt"]
    assert list(transcript_format.type.choices) == ["text", "csv", "json"]


@pytest.mark.parametrize("args", [("loom", "info", "not-a-video"), ("facebook", "about", "not a person!"),
                                  ("linkedin", "activity", "posts", "--since", "last tuesday"),
                                  ("linkedin", "feed", "--since", "yesterday"), ("linkedin", "posts", "search", "  "),
                                  ("linkedin", "posts", "read", "not-a-post"), ("linkedin", "posts", "from", "a b"),
                                  ("linkedin", "boost", "stats", "nope"),
                                  ("linkedin", "boost", "list", "--since", "2026-10-09", "--until", "2026-10-01"),
                                  ("linkedin", "messages", "send", "x", "--text", "   "),
                                  ("messages", "send", "--to", "x", "--text", "   "), ("messages", "read", "  "),
                                  ("linkedin", "post", "create", "--text", "hi", "--image", "/nonexistent.png")])
def test_bad_input_is_a_usage_error_before_any_chrome(fake_playwright, profiles, args):
    assert run(*args).exit_code == 2
    assert fake_playwright == []


# Redaction.

@pytest.mark.parametrize("args", [("linkedin", "messages", "list"), ("linkedin", "boost", "list"),
                                  ("linkedin", "post", "create", "--text", "hi", "--image", "{png}", "--confirm"),
                                  ("facebook", "sync"), ("facebook", "about", "jane.doe"),
                                  ("loom", "transcript", VID), ("loom", "list"), ("messages", "list"),
                                  ("messages", "send", "--to", "Alice", "--text", "hi", "--confirm")])
def test_cookies_tokens_and_signed_links_never_reach_output_errors_or_logs(monkeypatch, profiles, png, args):
    async def failing(*_args, **_kwargs):
        raise RuntimeError(f"Fetching failed at {SIGNED}; Cookie: li_at={LI_AT}; JSESSIONID=\"{JSESSION}\"; "
                           f"csrf-token: {JSESSION}; c_user=100000000000001; xs=12%3AFAKExsValue0123456789%3A2; "
                           "body fb_dtsg=NAcMfakeDTSG0123456789:12")

    monkeypatch.setattr(daemon, "call", failing)
    result = run(*[a.replace("{png}", str(png)) for a in args])
    assert result.exit_code == 5
    assert "<redacted>" in result.stderr and leaked(result) == []


@pytest.mark.parametrize("name", SITES)
def test_sign_in_needed_exits_4_with_the_identity_login_hint(monkeypatch, profiles, name):
    async def signed_out(*_args, **_kwargs):
        raise AuthRequired("The session is missing or expired.")

    monkeypatch.setattr(daemon, "call", signed_out)
    args = {"linkedin": ("messages", "list"), "facebook": ("sync",), "loom": ("list",), "messages": ("list",)}[name]
    result = run(name, *args)
    assert result.exit_code == 4 and f"webcli identity login {SITES[name]}:default" in result.stderr


# The identity daemon.

@pytest.mark.parametrize("name", SITES)
def test_the_daemon_serves_their_listed_ops_only_on_their_provider(name):
    site = sites.get(name)
    for op in site.ops:
        assert daemon.site_op(Identity(f"{SITES[name]}:default"), name, op)
    with pytest.raises(ConfigError):
        daemon.site_op(Identity("google:default"), name, "status")
    with pytest.raises(ConfigError):
        daemon.site_op(Identity(f"{SITES[name]}:default"), name, "login")  # sign-in is never a daemon op
