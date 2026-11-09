"""webcli 1.0 Phase A: loading community tools safely. Built-ins stay official and unshadowed; a broken, revoked, or
disabled tool is skipped without breaking the CLI; a community tool uses only its own identity unless granted another;
its pages reach only its manifest's hosts; its write commands are dry runs without --confirm. Offline."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from webcli import sites
from webcli.apps import webcli as webcli_app
from webcli.core import daemon, grants, guard, identities, revoked, tools_registry
from webcli.core.errors import ConfigError
from webcli.core.identities import Identity


@pytest.fixture(autouse=True)
def fresh_registry():
    sites.reload()
    yield
    sites.reload()


def make_tool(root: Path, name: str = "toyhttp", *, identity: str | None = None, init_extra: str = "",
              manifest_name: str | None = None, adapter_hosts: str = '("httpbin.org",)', cli_body: str | None = None,
              version: str = "0.1.0") -> Path:
    """A minimal valid tool folder: `get` reads, `label add` writes."""
    identity = identity or name
    folder = root / name
    package = folder / f"webcli_tool_{name}"
    package.mkdir(parents=True)
    (folder / "webcli-tool.toml").write_text(textwrap.dedent(f"""
        name = "{manifest_name or name}"
        version = "{version}"
        adapter_api = 1
        summary = "a toy tool"
        identity = "{identity}"
        hosts = ["httpbin.org"]
        maintainers = ["github:someone"]
        license = "MIT"

        [[commands]]
        name = "get"
        strategy = "cookie"

        [[commands]]
        name = "label add"
        strategy = "cookie"
        writes = true
        """))
    (package / "__init__.py").write_text(textwrap.dedent(f"""
        from webcli.adapter import Site, Strategy
        {init_extra}

        def _cli():
            from webcli_tool_{name}.cli import group
            return group

        ADAPTER = Site(name="{name}", kind="data", provider="{identity}", summary="a toy tool", cli=_cli,
                       fetch="webcli_tool_{name}.fetch", ops=("get",),
                       verbs={{"get": Strategy.COOKIE, "label add": Strategy.COOKIE}}, version="{version}",
                       hosts={adapter_hosts}, writes=frozenset({{"label add"}}))
        """))
    (package / "fetch.py").write_text(textwrap.dedent("""
        from webcli.core import guard

        async def get(**_kwargs):
            return {"envelope": guard.envelope.get()}
        """))
    (package / "cli.py").write_text(cli_body or textwrap.dedent("""
        import click

        @click.group()
        def group():
            "A toy tool."

        @group.command("get")
        def get():
            click.echo("got")

        @group.group("label")
        def label():
            "Labels."

        @label.command("add")
        def add():
            click.echo("WROTE")
        """))
    return folder


@pytest.fixture
def tools(offline) -> Path:
    root = offline / "tools"
    root.mkdir(parents=True)
    return root


def run(*args: str):
    return CliRunner().invoke(webcli_app.build(), list(args))


def skipped_reasons() -> dict[str, str]:
    return {s["name"]: s["reason"] for s in sites.skipped()}


# Built-ins are untouched.

def test_built_ins_load_as_official_with_no_tools_installed():
    assert set(sites.registry()) == set(sites.BUILTIN)
    assert {sites.tier_of(name) for name in sites.registry()} == {"official"}
    assert sites.skipped() == [] and sites.hosts_limit("gmail") is None


def test_an_installed_tool_loads_as_community_after_the_built_ins(tools):
    make_tool(tools)
    assert list(sites.registry())[-1] == "toyhttp" and sites.tier_of("toyhttp") == "community"
    assert sites.hosts_limit("toyhttp") == ("httpbin.org",)
    rows = json.loads(run("sites").stdout)["webcli"]
    assert {r["name"]: r["tier"] for r in rows}["toyhttp"] == "community"
    assert {r["name"]: r["tier"] for r in rows}["gmail"] == "official"
    assert run("toyhttp", "get").stdout == "got\n"


def test_a_dev_tool_from_dev_toml_loads(tools, tmp_path):
    folder = make_tool(tmp_path / "work", "devtoy")
    (tools / "dev.toml").write_text(f'[devtoy]\npath = "{folder}"\n')
    assert sites.tier_of("devtoy") == "community"


def test_no_tools_env_turns_every_community_tool_off(tools, monkeypatch):
    make_tool(tools)
    monkeypatch.setenv("WEBCLI_NO_TOOLS", "1")
    assert set(sites.registry()) == set(sites.BUILTIN)


# Shadowing.

@pytest.mark.parametrize("name", ["gmail", "google", "meta", "tools", "none"])
def test_a_tool_cant_shadow_a_built_in_site_provider_or_command_and_its_code_never_runs(tools, capsys, name):
    marker = tools.parent / "ran"
    make_tool(tools, name, init_extra=f"open({str(marker)!r}, 'w').close()")
    registry = sites.registry()
    assert sites.tier_of(name) != "community"
    if name == "gmail":
        assert registry["gmail"].fetch == "webcli.sites.gmail.fetch"
    assert "can't replace it" in skipped_reasons()[name]
    assert f"skipped the {name} tool" in capsys.readouterr().err
    assert not marker.exists()


def test_a_dev_tool_cant_take_an_installed_tools_name(tools, tmp_path):
    make_tool(tools)
    other = make_tool(tmp_path / "work", "toyhttp")
    (tools / "dev.toml").write_text(f'[toyhttp]\npath = "{other}"\n')
    assert sites.tier_of("toyhttp") == "community"
    assert sites.registry()["toyhttp"] and "another tool's name" in skipped_reasons()["toyhttp"]


def test_another_packages_entry_point_is_not_loaded(monkeypatch, capsys):
    foreign = SimpleNamespace(name="gmail", value="evil:ADAPTER", dist=SimpleNamespace(name="evil"))
    monkeypatch.setattr(sites, "entry_points", lambda group: [foreign])
    assert sites.registry()["gmail"].fetch == "webcli.sites.gmail.fetch"
    assert "entry point" in skipped_reasons()["gmail"] and "skipped the gmail tool" in capsys.readouterr().err


# Broken tools are skipped, not fatal.

@pytest.mark.parametrize("breakage", ["raises", "exits", "mismatch", "misnamed", "foreign fetch"])
def test_a_broken_tool_is_skipped_and_the_rest_still_load(tools, capsys, breakage):
    make_tool(tools, "goodtool")
    if breakage == "raises":
        make_tool(tools, "badtool", init_extra="raise RuntimeError('boom')")
    elif breakage == "exits":
        make_tool(tools, "badtool", init_extra="raise SystemExit(3)")
    elif breakage == "mismatch":
        make_tool(tools, "badtool", adapter_hosts='("httpbin.org", "evil.example.com")')
    elif breakage == "misnamed":
        make_tool(tools, "badtool", manifest_name="othertool")
    else:
        folder = make_tool(tools, "badtool")
        init = folder / "webcli_tool_badtool" / "__init__.py"
        init.write_text(init.read_text().replace('fetch="webcli_tool_badtool.fetch"', 'fetch="webcli.sites.gmail.fetch"'))
    assert "badtool" not in sites.registry() and sites.tier_of("goodtool") == "community"
    assert "badtool" in skipped_reasons() and "skipped the badtool tool" in capsys.readouterr().err
    result = run("sites")
    assert result.exit_code == 0 and "goodtool" in result.stdout


def test_a_folder_without_a_valid_manifest_is_skipped(tools):
    (tools / "junk").mkdir()
    (tools / ".installing").mkdir()  # in progress: ignored
    assert "manifest" in skipped_reasons()["junk"] or "webcli-tool.toml" in skipped_reasons()["junk"]
    assert ".installing" not in skipped_reasons()


def test_a_tool_whose_commands_fail_to_load_doesnt_break_the_cli(tools):
    make_tool(tools, cli_body="raise ImportError('missing dependency')\n")
    assert run("sites").exit_code == 0
    result = run("toyhttp", "get")
    assert result.exit_code == 7 and "didn't load" in result.stderr


def test_a_disabled_tool_is_skipped_quietly_and_never_imported(tools, capsys):
    marker = tools.parent / "ran"
    folder = make_tool(tools, init_extra=f"open({str(marker)!r}, 'w').close()")
    (folder / tools_registry.DISABLED).touch()
    assert "toyhttp" not in sites.registry() and skipped_reasons()["toyhttp"] == "disabled"
    assert capsys.readouterr().err == "" and not marker.exists()


def test_doctor_reports_community_and_skipped_tools(tools):
    make_tool(tools)
    make_tool(tools, "gmail")
    report = json.loads(run("doctor", "--no-browser").stdout)
    assert report["tools"]["community"] == ["toyhttp"]
    assert [s["name"] for s in report["tools"]["skipped"]] == ["gmail"]


def test_doctor_says_how_old_the_revoked_list_is(tools):
    """Revocations arrive only when a `webcli tools` command refreshes index-cache.json, so doctor says how old it is,
    and calls it stale once a community tool is installed and the list is missing or over a week old."""
    def revocations():
        return json.loads(run("doctor", "--no-browser").stdout)["tools"]["revocations"]

    assert revocations() == {"cache": str(tools / "index-cache.json"), "ageDays": None, "stale": False, "hint": None}
    make_tool(tools)
    sites.reload()  # the first doctor run cached a registry with no tools
    assert revocations()["stale"] is True and "webcli tools search" in revocations()["hint"]
    cache = tools / "index-cache.json"
    cache.write_text(json.dumps({"tools": [], "revoked": []}))
    assert revocations()["ageDays"] == 0.0 and revocations()["stale"] is False
    eight_days_ago = cache.stat().st_mtime - 8 * 86400
    os.utime(cache, (eight_days_ago, eight_days_ago))
    assert revocations()["ageDays"] == 8.0 and revocations()["stale"] is True


# Revoked tools.

def test_a_revoked_tool_is_not_loaded_and_its_code_never_runs(tools, capsys):
    marker = tools.parent / "ran"
    make_tool(tools, init_extra=f"open({str(marker)!r}, 'w').close()")
    (tools / "index-cache.json").write_text(json.dumps(
        {"tools": [], "revoked": [{"name": "toyhttp", "reason": "it posted without asking"}]}))
    assert "toyhttp" not in sites.registry() and not marker.exists()
    assert "it posted without asking" in skipped_reasons()["toyhttp"] and "revoked" in capsys.readouterr().err


def test_revocation_by_version(tools):
    (tools / "revoked.json").write_text(json.dumps([{"name": "toyhttp", "versions": ["0.0.9"]}, "othertool"]))
    make_tool(tools)
    assert sites.tier_of("toyhttp") == "community"
    assert revoked.reason("toyhttp", "0.0.9") and not revoked.is_revoked("toyhttp", "0.1.0")
    assert not revoked.is_revoked("toyhttp") and revoked.is_revoked("othertool", "1.2.3")


def test_an_unreadable_revoked_list_fails_closed(tools):
    make_tool(tools)
    (tools / "index-cache.json").write_text("{not json")
    assert "toyhttp" not in sites.registry() and "can't be read" in skipped_reasons()["toyhttp"]


def test_the_daemon_refuses_a_tool_revoked_after_it_loaded(tools):
    make_tool(tools)
    assert sites.tier_of("toyhttp") == "community"
    (tools / "revoked.json").write_text(json.dumps({"revoked": ["toyhttp"]}))
    with pytest.raises(ConfigError, match="revoked"):
        daemon.site_op(Identity("toyhttp:default"), "toyhttp", "get")


# Identities and grants.

def test_a_community_tool_runs_on_its_own_identity_held_to_its_hosts(tools):
    make_tool(tools)
    op = daemon.site_op(Identity("toyhttp:default"), "toyhttp", "get")
    assert asyncio.run(op()) == {"envelope": ("httpbin.org",)}
    assert guard.envelope.get() is None  # only while the op runs


def test_a_community_tool_needs_a_grant_to_use_an_existing_identity(tools):
    make_tool(tools, identity="google")
    with pytest.raises(ConfigError) as refused:
        daemon.site_op(Identity("google:default"), "toyhttp", "get")
    assert refused.value.hint == "To let it run with that session: webcli tools grant toyhttp google:default"
    assert grants.grant("toyhttp", "google:default") is True
    assert asyncio.run(daemon.site_op(Identity("google:default"), "toyhttp", "get")()) == \
        {"envelope": ("httpbin.org",)}
    with pytest.raises(ConfigError):  # a grant is for one identity
        daemon.site_op(Identity("google:work"), "toyhttp", "get")
    assert grants.revoke_grant("toyhttp", "google:default") is True
    with pytest.raises(ConfigError, match="only if you grant it"):
        daemon.site_op(Identity("google:default"), "toyhttp", "get")


def test_official_sites_need_no_grant_and_get_no_envelope():
    op = daemon.site_op(Identity("google:default"), "gmail", "status")
    assert op.__module__ == "webcli.sites.gmail.fetch"


def test_grants_file_is_private_and_round_trips(offline):
    assert grants.list_grants() == {} and not grants.is_granted("toyhttp", "google:default")
    assert grants.grant("toyhttp", "google:default") and not grants.grant("toyhttp", "google:default")
    grants.grant("toyhttp", "meta:default")
    grants.grant("other", "google:work")
    assert grants.list_grants() == {"other": ["google:work"], "toyhttp": ["google:default", "meta:default"]}
    assert stat.S_IMODE(os.stat(grants.path()).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(grants.path().parent).st_mode) == 0o700
    assert grants.revoke_grant("toyhttp") and grants.list_grants() == {"other": ["google:work"]}
    assert not grants.revoke_grant("toyhttp", "google:default")
    with pytest.raises(ConfigError):
        grants.grant("toyhttp", "google")  # not provider:account
    with pytest.raises(ConfigError):
        grants.grant("Bad-Name", "google:default")


def test_a_damaged_grants_file_grants_nothing(offline):
    grants.path().parent.mkdir(parents=True)
    grants.path().write_text('toyhttp = "google:default"\n')
    with pytest.raises(ConfigError):
        grants.is_granted("toyhttp", "google:default")


def test_identity_login_never_uses_a_community_tools_sign_in_for_a_shared_provider(tools):
    make_tool(tools, "aaatool", identity="google", init_extra="async def _login(*a): return {}")
    init = tools / "aaatool" / "webcli_tool_aaatool" / "__init__.py"
    init.write_text(init.read_text().replace('summary="a toy tool",', 'summary="a toy tool", login=_login,'))
    make_tool(tools, "owntool", init_extra="async def _login(*a): return {}")
    init = tools / "owntool" / "webcli_tool_owntool" / "__init__.py"
    init.write_text(init.read_text().replace('summary="a toy tool",', 'summary="a toy tool", login=_login,'))
    every = list(sites.registry().values())
    assert sites.registry()["aaatool"].login is not None
    assert identities.login_site(Identity("google:default"), every).name == "gemini"
    assert identities.login_site(Identity("owntool:default"), every).name == "owntool"
    with pytest.raises(ConfigError):  # a tool only signs in its own identity
        identities.login_site(Identity("google:default"), [s for s in every if s.name == "aaatool"])


# The host envelope.

def _request(url: str, method: str = "GET"):
    return SimpleNamespace(url=url, method=method)


def test_the_envelope_blocks_hosts_outside_the_manifest_whatever_the_policy_says():
    allow_all = guard.Policy("toyhttp", lambda request, state: True, lambda request: request.url)
    with guard.held_to(("httpbin.org", "*.cdn.example.com")):
        guard.state.begin(allow_all)
    assert guard.check(_request("https://httpbin.org/get"), own_page=True)
    assert guard.check(_request("https://a.cdn.example.com/x.js"), own_page=True)
    assert not guard.check(_request("https://evil.example.com/steal"), own_page=True)
    assert not guard.check(_request("https://httpbin.org.evil.com/"), own_page=True)
    assert guard.state.blocked == ["https://evil.example.com/steal", "https://httpbin.org.evil.com/"]
    guard.state.end()
    assert guard.state.hosts is None


def test_an_empty_envelope_allows_nothing_and_no_envelope_leaves_the_policy_alone():
    allow_all = guard.Policy("x", lambda request, state: True, lambda request: request.url,
                             sockets=lambda url: True)
    with guard.held_to(()):
        guard.state.begin(allow_all)
    assert not guard.check(_request("https://httpbin.org/get"), own_page=True)
    assert not guard.socket_allowed("wss://httpbin.org/ws", own_page=True)
    guard.state.begin(allow_all)
    assert guard.check(_request("https://anything.example.com/"), own_page=True)
    assert guard.socket_allowed("wss://anything.example.com/ws", own_page=True)


# Write gating.

def test_a_community_write_command_is_a_dry_run_without_confirm(tools):
    make_tool(tools)
    result = run("toyhttp", "label", "add")
    assert result.exit_code == 0 and "WROTE" not in result.stdout
    assert json.loads(result.stdout) == {"dryRun": True, "tool": "toyhttp", "command": "label add",
                                         "hint": "Nothing was written. Re-run with --confirm to write."}
    assert "WROTE" not in run("toyhttp", "label", "add", "--confirm", "--dry-run").stdout
    assert run("toyhttp", "label", "add", "--confirm").stdout == "WROTE\n"
    assert run("toyhttp", "label", "add", "--yes").stdout == "WROTE\n"
    assert run("toyhttp", "get").stdout == "got\n"  # reads are untouched


def test_a_tools_own_confirm_option_still_reaches_it(tools):
    make_tool(tools, cli_body=textwrap.dedent("""
        import click
        from webcli.adapter import confirm_write, write_options

        @click.group()
        def group():
            "A toy tool."

        @group.command("get")
        def get():
            click.echo("got")

        @group.group("label")
        def label():
            "Labels."

        @label.command("add")
        @write_options
        def add(confirm, dry_run):
            if confirm_write(confirm, {"label": "x"}, dry_run=dry_run):
                click.echo("WROTE")
        """))
    assert json.loads(run("toyhttp", "label", "add").stdout)["tool"] == "toyhttp"
    assert run("toyhttp", "label", "add", "--confirm").stdout == "WROTE\n"
