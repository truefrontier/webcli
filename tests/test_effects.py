"""webcli 1.0.0rc2: every command says what it can do besides read (agent-help's `effects`, effectsApi 1), built-ins
fill Site.writes from the same review, and `webcli sites` says where each site came from. docs/adapter-api-v1.md has
the contract. Offline: the library is a file:// fake (test_tools_cmd.Library)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import click
import pytest
from click.testing import CliRunner
from test_tool_trust import make_tool
from test_tools_cmd import Library, library  # noqa: F401  (library: the fixture)

from webcli import sites
from webcli.apps import llmcli as llmcli_app
from webcli.apps import webcli as webcli_app
from webcli.chromecli import cli as chromecli_cli
from webcli.core import index_client, output, revoked

KEYS = {"writes", "writeFlags", "sendsToPeople", "humanOnly", "opensWindow", "changesLocalSetup"}
WORST = {"writes": "always", "writeFlags": [], "sendsToPeople": True, "humanOnly": True, "opensWindow": True,
         "changesLocalSetup": True, "undeclared": True}  # what an undeclared command reports, spelled out
LIVE_INDEX = Path(__file__).parent / "fixtures" / "library" / "index-2026-11-06.json"  # the library's, that day


@pytest.fixture(autouse=True)
def fresh_registry(offline):
    sites.reload()
    yield
    sites.reload()


def tree(binary: str) -> click.Group:
    return {"webcli": webcli_app.build, "llmcli": llmcli_app.build, "chromecli": lambda: chromecli_cli.group}[binary]()


def run(binary: str, *args: str):
    sites.reload()
    return CliRunner().invoke(tree(binary), list(args))


def ok(binary: str, *args: str):
    result = run(binary, *args)
    assert result.exit_code == 0, result.stderr
    return json.loads(result.stdout)


def reference(binary: str, *group: str) -> dict:
    return ok(binary, *group, "agent-help", "--format", "json")


def tags(effects: dict) -> str:
    """sendsToPeople, humanOnly, opensWindow, changesLocalSetup as letters: "shwl"."""
    return "".join(t for t, key in (("s", "sendsToPeople"), ("h", "humanOnly"), ("w", "opensWindow"),
                                    ("l", "changesLocalSetup")) if effects[key])


def summary(ref: dict) -> dict:
    """name -> (writes, writeFlags, tags) for every command that isn't a plain read."""
    return {c["name"]: (c["effects"]["writes"], tuple(c["effects"]["writeFlags"]), tags(c["effects"]))
            for c in ref["commands"] if c["effects"]["writes"] != "never" or tags(c["effects"])}


# The reviewed table: every command that isn't a plain read. Anything not here is {"writes": "never"} and all false.
# A change here is a change to what webcli tells a policy wrapper; review it as one.
GOLDEN = {
    "webcli": {
        "aa config set-key": ("always", (), "hl"),
        "amazon login": ("always", (), "hwl"),
        "contacts label add": ("with_flag", ("--confirm", "--yes"), ""),
        "contacts label remove": ("with_flag", ("--confirm", "--yes"), ""),
        "gmail filters create": ("with_flag", ("--confirm", "--yes"), ""),
        "gmail filters delete": ("with_flag", ("--confirm", "--yes"), ""),
        "gsc inspect": ("with_flag", ("--request-indexing", "--yes", "--confirm"), ""),
        "gsc sitemaps": ("with_flag", ("--yes", "--confirm"), ""),
        "linkedin messages send": ("with_flag", ("--confirm", "--yes", "--send"), "s"),
        "linkedin post create": ("with_flag", ("--confirm", "--yes"), "s"),
        "messages send": ("with_flag", ("--new-ok", "--confirm", "--yes"), "s"),
        "notebooklm source add": ("with_flag", ("--confirm", "--yes"), ""),
        "notebooklm ask": ("always", (), ""),
        "notebooklm create": ("with_flag", ("--confirm", "--yes"), ""),
        **{f"notebooklm generate {kind}": ("with_flag", ("--confirm", "--yes"), "")
           for kind in ("audio", "video", "report", "quiz", "flashcards", "mind-map", "infographic", "slide-deck",
                        "data-table")},
        "perplexity login": ("always", (), "hwl"),
        "whatsapp login": ("always", (), "hwl"),
        "whatsapp send": ("with_flag", ("--confirm", "--yes"), "s"),
        "youtube playlist create": ("with_flag", ("--confirm", "--yes"), ""),
        "new": ("always", (), "hwl"),
        "dev": ("always", (), "shwl"),
        "test": ("always", (), "shwl"),
        "lint": ("always", (), "shwl"),
        **{f"tools {verb}": ("always", (), "hl")
           for verb in ("install", "update", "remove", "grant", "revoke-grant", "disable", "enable")},
        "submit": ("with_flag", ("--confirm", "--yes"), "sh"),
        "feed schedule install": ("always", (), "l"),
        "feed schedule remove": ("always", (), "l"),
        "feed push": ("always", (), ""),
        "identity login": ("always", (), "hwl"),
        "doctor": ("with_flag", ("--fix",), "l"),
        "serve": ("with_flag", ("--install-launchd", "--uninstall-launchd"), "hl"),
    },
    "llmcli": {
        **{f"{site} {verb}": ("never", (), "w") for site in ("chatgpt", "gemini", "muse")
           for verb in ("status", "chats list", "chats show")},
        "chatgpt models": ("never", (), "w"),
        "gemini models": ("never", (), "w"),
        "chatgpt ask": ("always", (), "w"),
        "gemini ask": ("always", (), "w"),
        "muse ask": ("always", (), "sw"),
        "chatgpt -p": ("always", (), ""),
        "gemini -p": ("always", (), ""),
        "muse -p": ("always", (), "s"),
        "notebooklm ask": ("always", (), ""),
        "notebooklm -p": ("always", (), ""),
        **{f"{site} login": ("always", (), "hwl") for site in ("chatgpt", "gemini", "muse", "notebooklm")},
        **{f"{site} serve": ("with_flag", ("--install-launchd", "--uninstall-launchd"), "hl")
           for site in ("chatgpt", "gemini", "muse", "notebooklm")},
        "identity login": ("always", (), "hwl"),
        "doctor": ("with_flag", ("--fix",), "l"),
        "serve": ("with_flag", ("--install-launchd", "--uninstall-launchd"), "hl"),
    },
    "chromecli": {
        "open": ("with_flag", ("--confirm", "--yes"), "w"),
        "focus": ("with_flag", ("--confirm", "--yes"), "w"),
        "close": ("with_flag", ("--confirm", "--yes"), ""),
        "navigate": ("with_flag", ("--confirm", "--yes"), ""),
        **{verb: ("with_flag", ("--confirm", "--yes"), "s") for verb in ("click", "fill", "type", "select", "press",
                                                                          "eval")},
        **{verb: ("with_flag", ("--confirm", "--yes"), "l") for verb in ("grant add", "grant add-batch",
                                                                          "grant revoke", "install-host",
                                                                          "uninstall-host")},
        "record start": ("with_flag", ("--confirm", "--yes"), "w"),
    },
}

# What a policy wrapper that predates effects hard-codes (its floor). webcli's facts must cover all of it, so a
# wrapper that unions the two never loosens: same names, same flags.
FLOOR_HUMAN_ONLY = {"identity login", "amazon login", "whatsapp login", "perplexity login", "serve",
                    "aa config set-key"}
FLOOR_WITH_DRY_RUN = {
    "contacts label add", "contacts label remove", "gmail filters create", "gmail filters delete",
    "linkedin messages send", "linkedin post create", "messages send", "whatsapp send", "notebooklm create",
    "notebooklm source add", "notebooklm generate audio", "notebooklm generate video", "notebooklm generate report",
    "notebooklm generate quiz", "notebooklm generate flashcards", "notebooklm generate mind-map",
    "notebooklm generate infographic", "notebooklm generate slide-deck", "notebooklm generate data-table",
    "youtube playlist create", "gsc sitemaps", "gsc inspect",
}
FLOOR_ALWAYS_WRITE = {"feed push", "feed schedule install", "feed schedule remove"}
FLOOR_WRITE_FLAGS = {"--confirm", "--yes", "--send", "--request-indexing", "--fix", "--new-ok"}
# The 1.0 commands the product owner made person-only (scope §2a / decision 6).
HUMAN_ONLY_1_0 = {"new", "dev", "submit", *(f"tools {v}" for v in ("install", "update", "remove", "grant",
                                                                    "revoke-grant", "enable", "disable"))}
WRITE_SPELLINGS = FLOOR_WRITE_FLAGS | {"-y", "--install-launchd", "--uninstall-launchd"}


# Every command declares; nothing silently reads as "never".

@pytest.mark.parametrize("binary", ["webcli", "llmcli", "chromecli"])
def test_every_command_of_every_binary_declares_its_effects(binary):
    root = tree(binary)
    undeclared = [" ".join(path) for path, _aliases, cmd in output.leaves(root, hidden=True)
                  if output.command_effects(cmd).get("undeclared")]
    assert undeclared == []
    # No group can turn a flag on without it being typed, so "with_flag" means typed.
    groups = [root]
    while groups:
        group = groups.pop()
        assert not output.implicit(group), group.name
        groups += [c for c in group.commands.values() if isinstance(c, click.Group)]


@pytest.mark.parametrize("args", [("webcli",), ("webcli", "gsc"), ("webcli", "feed"), ("webcli", "notebooklm"),
                                  ("llmcli",), ("llmcli", "gemini"), ("chromecli",)])
def test_agent_help_json_has_effects_api_1_and_a_full_effects_block_on_every_command(args):
    ref = reference(*args)
    assert ref["effectsApi"] == 1
    for command in ref["commands"]:
        effects, name = command["effects"], command["name"]
        assert set(effects) == KEYS, name
        assert effects["writes"] in ("never", "with_flag", "always"), name
        assert (effects["writes"] == "with_flag") == bool(effects["writeFlags"]), name
        spelled = {flag for option in command["options"] for flag in option["flags"]}
        assert set(effects["writeFlags"]) <= spelled, name
        if effects["sendsToPeople"] or effects["changesLocalSetup"]:
            assert effects["writes"] != "never", name


@pytest.mark.parametrize("binary", ["webcli", "llmcli", "chromecli"])
def test_the_reviewed_effects_table(binary):
    assert summary(reference(binary)) == GOLDEN[binary]


def test_a_sites_own_agent_help_says_the_same_as_webcli_agent_help():
    site = {f"gsc {c['name']}": c["effects"] for c in reference("webcli", "gsc")["commands"]}
    umbrella = {c["name"]: c["effects"] for c in reference("webcli")["commands"] if c["name"].startswith("gsc ")}
    assert site == umbrella


def test_effects_cover_the_old_hard_coded_floor():
    effects = {c["name"]: c["effects"] for c in reference("webcli")["commands"]}
    for name in FLOOR_HUMAN_ONLY | HUMAN_ONLY_1_0:
        assert effects[name]["humanOnly"], name
    for name in FLOOR_WITH_DRY_RUN:
        assert effects[name]["writes"] == "with_flag", name
    for name in FLOOR_ALWAYS_WRITE:
        assert effects[name]["writes"] == "always", name
    # Facts may be stricter than the floor, never looser: every command the floor counts as a writer writes here.
    assert {n for n, e in effects.items() if e["writes"] != "never"} >= FLOOR_WITH_DRY_RUN | FLOOR_ALWAYS_WRITE


@pytest.mark.parametrize("binary", ["webcli", "llmcli", "chromecli"])
def test_every_write_spelling_on_a_command_is_one_of_its_write_flags(binary):
    for command in reference(binary)["commands"]:
        effects = command["effects"]
        spelled = {flag for option in command["options"] for flag in option["flags"]} & WRITE_SPELLINGS
        if spelled:
            assert effects["writes"] != "never", command["name"]
        if effects["writes"] == "with_flag":
            assert spelled <= set(effects["writeFlags"]), command["name"]


@pytest.mark.parametrize("binary", ["webcli", "llmcli", "chromecli"])
def test_headed_capture_and_login_commands_open_a_window(binary):
    for command in reference(binary)["commands"]:
        flags = {flag for option in command["options"] for flag in option["flags"]}
        if flags & {"--headed", "--capture"}:
            assert command["effects"]["opensWindow"], command["name"]
        if command["name"].split()[-1] == "login":
            assert command["effects"]["humanOnly"] and command["effects"]["opensWindow"], command["name"]


def test_aliases_share_their_commands_effects():
    root = tree("webcli")
    walkie = root.commands["walkie"].commands["meetings"]
    assert walkie.commands["read"] is walkie.commands["transcript"]
    assert output.command_effects(walkie.commands["read"])["writes"] == "never"
    linkedin = root.commands["linkedin"].commands["messages"]
    assert output.command_effects(linkedin.commands["read"]) == output.command_effects(linkedin.commands["history"])


# What an undeclared or inconsistent declaration reports: the worst case, never "never".

def _reference_of(group: click.Group) -> dict:
    return {c["name"]: c["effects"] for c in
            output.agent_reference(group, click.Context(group, info_name="toy"), env={}, notes=[], writes=[])["commands"]}


def test_a_command_nobody_declared_claims_the_worst_not_never():
    @click.group()
    def group():
        pass

    @group.command("brand-new")
    def brand_new():
        pass

    @output.effects("with_flag", via=("confirm",))
    @group.command("no-such-option")
    def no_such_option():
        pass

    @output.effects("with_flag", via=("to",))
    @group.command("valued-option")
    @click.option("--to")
    def valued_option(to):
        pass

    @output.effects("with_flag", via=("confirm",))
    @group.command("env-confirm")
    @click.option("--confirm", is_flag=True, envvar="TOY_CONFIRM")
    def env_confirm(confirm):
        pass

    @output.effects("with_flag", via=("confirm",))
    @group.command("default-map", context_settings={"default_map": {"confirm": True}})
    @click.option("--confirm", is_flag=True)
    def default_map(confirm):
        pass

    @output.effects("with_flag", via=("confirm",), sends=True)
    @group.command("fine")
    @click.option("--confirm", "--yes", "-y", "confirm", is_flag=True)
    def fine(confirm):
        pass

    got = _reference_of(group)
    assert got["brand-new"] == got["no-such-option"] == got["valued-option"] == WORST == output.UNDECLARED
    assert got["env-confirm"]["writes"] == got["default-map"]["writes"] == "always"
    assert got["env-confirm"]["writeFlags"] == []
    assert got["fine"] == {"writes": "with_flag", "writeFlags": ["--confirm", "--yes", "-y"], "sendsToPeople": True,
                           "humanOnly": False, "opensWindow": False, "changesLocalSetup": False}


@pytest.mark.parametrize("bad", [
    lambda: output.effects("with_flag"),  # which flag?
    lambda: output.effects("never", via=("confirm",)),
    lambda: output.effects("never", sends=True),  # sending is a write
    lambda: output.effects("never", local_setup=True),
    lambda: output.effects("sometimes"),
])
def test_an_inconsistent_declaration_fails_at_import(bad):
    with pytest.raises(ValueError):
        bad()


def test_effects_go_on_a_command_not_a_group_or_function():
    with pytest.raises(TypeError):
        output.effects()(click.Group("g"))
    with pytest.raises(TypeError):
        output.effects()(lambda: None)


# Built-ins: Site.writes / sends_to_people (what `webcli sites` and the library catalog show) match the effects.

# A command whose write lands on a verb with another name (Site.verbs is keyed by fetch op on these sites).
VERB_OF = {("gsc", "inspect"): "request_indexing", ("gsc", "sitemaps"): "submit_sitemap",
           ("notebooklm", "source add"): "add_sources", ("notebooklm", "-p"): "ask",
           ("chatgpt", "-p"): "ask", ("gemini", "-p"): "ask", ("muse", "-p"): "ask"}


def _site_writes() -> tuple[dict, dict]:
    """site -> the verbs its writing commands land on (site writes only: a login changes this computer, not the
    site), and the ones that send to people."""
    writes, sends = {}, {}
    for binary in ("webcli", "llmcli"):
        for command in reference(binary)["commands"]:
            site, _, rest = command["name"].partition(" ")
            effects = command["effects"]
            if site not in sites.BUILTIN or effects["writes"] == "never" or effects["changesLocalSetup"]:
                continue
            verb = VERB_OF.get((site, rest), "generate" if rest.startswith("generate ") else rest)
            writes.setdefault(site, set()).add(verb)
            if effects["sendsToPeople"]:
                sends.setdefault(site, set()).add(verb)
    return writes, sends


def test_built_in_sites_declare_writes_and_sends_to_people_from_the_same_review():
    writes, sends = _site_writes()
    for name, site in sites.registry().items():
        assert set(site.writes) == writes.get(name, set()), name
        assert set(site.sends_to_people) == sends.get(name, set()), name
        assert set(site.writes) <= set(site.verbs), name  # what the library catalog lists, verb by verb
    assert writes["gsc"] == {"request_indexing", "submit_sitemap"}
    assert sends == {"linkedin": {"messages send", "post create"}, "messages": {"send"}, "whatsapp": {"send"},
                     "muse": {"ask"}}


def test_sites_json_shows_built_in_writes():
    data = ok("webcli", "sites", "--format", "json")
    gsc = next(r for r in data["webcli"] if r["name"] == "gsc")
    assert gsc["writes"] == ["request_indexing", "submit_sitemap"] and "sendsToPeople" not in gsc
    assert next(r for r in data["webcli"] if r["name"] == "whatsapp")["sendsToPeople"] == ["send"]
    assert "writes" not in next(r for r in data["webcli"] if r["name"] == "amazon")


# Community tools: effects come from the manifest, whatever the tool's code says.

TOY_CLI = """
import click
from webcli.adapter import add_agent_help
from webcli.core.output import effects

@click.group({group_args})
def group():
    "A toy tool."

@effects("with_flag", via=("confirm",))  # the tool's own claims: core replaces them from the manifest
@group.command("get")
def get():
    click.echo("got")

@group.group("label")
def label():
    "Labels."

@effects("never")
@label.command("add")
{confirm_option}
def add(**_kw):
    click.echo("WROTE")

@effects("never")
@group.command("sneak")
def sneak():
    click.echo("SNEAK")

add_agent_help(group, notes=[], writes=[])
"""


def toy(tools: Path, *, group_args: str = "", confirm_option: str = "", sends: bool = True) -> Path:
    folder = make_tool(tools, cli_body=textwrap.dedent(TOY_CLI.format(group_args=group_args,
                                                                      confirm_option=confirm_option)))
    if sends:
        toml = folder / "webcli-tool.toml"
        toml.write_text(toml.read_text().replace("writes = true", "writes = true\nsends_to_people = true"))
        init = folder / "webcli_tool_toyhttp" / "__init__.py"
        init.write_text(init.read_text().replace('writes=frozenset({"label add"}))',
                                                 'writes=frozenset({"label add"}), '
                                                 'sends_to_people=frozenset({"label add"}))'))
    return folder


def toy_effects() -> dict:
    return {c["name"]: c["effects"] for c in reference("webcli")["commands"] if c["name"].startswith("toyhttp ")}


@pytest.fixture
def tools(offline) -> Path:
    root = offline / "tools"
    root.mkdir(parents=True)
    return root


def test_a_community_tools_effects_come_from_its_manifest(tools):
    toy(tools)
    got = toy_effects()
    read = {"writes": "never", "writeFlags": [], "sendsToPeople": False, "humanOnly": False, "opensWindow": False,
            "changesLocalSetup": False}
    assert got["toyhttp get"] == read  # the manifest says read; the tool's own with_flag claim is dropped
    assert got["toyhttp label add"] == {**read, "writes": "with_flag", "writeFlags": ["--confirm", "--yes"],
                                        "sendsToPeople": True}  # core's gate, not the tool's "never"
    assert got["toyhttp sneak"] == WORST  # not in the manifest: the worst case, whatever it claims
    assert got["toyhttp agent-help"] == read
    dry = run("webcli", "toyhttp", "label", "add")  # and the gate holds: the tool's code never ran
    assert (dry.exit_code, json.loads(dry.stdout)) == (0, {
        "dryRun": True, "tool": "toyhttp", "command": "label add",
        "hint": "Nothing was written. Re-run with --confirm to write."})
    assert "WROTE" not in dry.output


def test_a_tool_whose_group_reads_env_vars_writes_always(tools, monkeypatch):
    toy(tools, group_args='context_settings={"auto_envvar_prefix": "TOY"}')
    assert toy_effects()["toyhttp label add"]["writes"] == "always"
    monkeypatch.setenv("TOY_LABEL_ADD_CONFIRM", "1")  # why: the env var turns --confirm on with nothing typed
    assert run("webcli", "toyhttp", "label", "add").stdout == "WROTE\n"


def test_a_tool_whose_own_confirm_isnt_a_flag_is_undeclared(tools):
    toy(tools, confirm_option='@click.option("--confirm", "confirm", default=None)')
    assert toy_effects()["toyhttp label add"] == WORST


def test_one_command_under_a_read_path_and_a_write_path_is_undeclared(tools):
    toy(tools, sends=False)
    cli = tools / "toyhttp" / "webcli_tool_toyhttp" / "cli.py"
    cli.write_text(cli.read_text() + "\ngroup.add_command(add, 'get')\n")  # `label add`'s command, also as `get`
    got = toy_effects()
    assert got["toyhttp label add"] == WORST  # neither "never" (get) nor "with_flag" (label add) holds for both


def test_a_broken_tools_placeholder_command_is_undeclared(tools):
    toy(tools)
    (tools / "toyhttp" / "webcli_tool_toyhttp" / "cli.py").write_text("raise RuntimeError('broken')\n")
    effects = {c["name"]: c["effects"] for c in reference("webcli")["commands"]}
    assert effects["toyhttp"] == WORST


# `webcli sites`: where each site came from, what vouches for it, and the tools core skipped.

def site_row(data: dict, name: str) -> dict:
    return next(r for r in data["webcli"] if r["name"] == name)


def provenance(row: dict) -> dict:
    return {k: row.get(k) for k in ("tier", "source", "verified", "commit", "sha256", "grants", "revoked",
                                    "disabled")}


def test_sites_json_says_built_ins_are_built_in():
    data = ok("webcli", "sites", "--format", "json")
    assert provenance(site_row(data, "gmail")) == {"tier": "official", "source": "builtin", "verified": None,
                                                   "commit": None, "sha256": None, "grants": [], "revoked": False,
                                                   "disabled": False}
    assert data["skipped"] == []
    assert {r["source"] for r in data["llmcli"] + data["webcli"]} == {"builtin"}


def test_an_installed_tool_is_verified_only_while_its_files_match_the_librarys_verified_pin(library, tools):
    entry = library.publish()
    library.tools["toyhttp"]["verified"] = True
    library.save()
    assert ok("webcli", "tools", "install", "toyhttp", "--yes")["installed"] == "toyhttp"
    pinned = {"tier": "community", "source": "installed", "verified": True, "commit": entry["commit"],
              "sha256": entry["sha256"], "grants": [], "revoked": False, "disabled": False}
    assert provenance(site_row(ok("webcli", "sites", "--format", "json"), "toyhttp")) == pinned

    cache = tools / "toyhttp" / "webcli_tool_toyhttp" / "__pycache__"  # what Python adds doesn't count
    cache.mkdir(exist_ok=True)
    (cache / "cli.cpython-314.pyc").write_bytes(b"bytecode")
    assert site_row(ok("webcli", "sites", "--format", "json"), "toyhttp")["verified"] is True

    ok("webcli", "tools", "grant", "toyhttp", "google:default")  # grants are core's, from grants.toml
    assert site_row(ok("webcli", "sites", "--format", "json"), "toyhttp")["grants"] == ["google:default"]

    record = tools / "toyhttp" / ".installed.json"
    pinned_record = record.read_text()
    record.write_text(pinned_record.replace('"version": "0.1.0"', '"version": "9.9.9"'))
    assert site_row(ok("webcli", "sites", "--format", "json"), "toyhttp")["verified"] is False
    record.write_text(pinned_record)

    with (tools / "toyhttp" / "webcli_tool_toyhttp" / "cli.py").open("a") as handle:
        handle.write("\n# edited after install\n")
    row = site_row(ok("webcli", "sites", "--format", "json"), "toyhttp")
    assert (row["verified"], row["sha256"]) == (False, entry["sha256"])  # the pin stays; the files don't match it


def test_verified_needs_the_librarys_word_not_the_tools(library, tools):
    library.publish()  # verified: false in the index
    ok("webcli", "tools", "install", "toyhttp", "--yes")
    assert site_row(ok("webcli", "sites", "--format", "json"), "toyhttp")["verified"] is False
    library.tools["toyhttp"]["verified"] = True
    library.save()
    ok("webcli", "tools", "search")  # refreshes the cached index
    assert site_row(ok("webcli", "sites", "--format", "json"), "toyhttp")["verified"] is True
    (tools / "index-cache.json").unlink()  # no cached index: nothing vouches for it
    assert site_row(ok("webcli", "sites", "--format", "json"), "toyhttp")["verified"] is False


def test_a_dev_tool_is_dev_and_never_verified(offline, tmp_path):
    folder = make_tool(tmp_path / "work")
    (offline / "tools").mkdir(parents=True)
    (offline / "tools" / "dev.toml").write_text(f'[toyhttp]\npath = "{folder}"\n')
    row = site_row(ok("webcli", "sites", "--format", "json"), "toyhttp")
    assert provenance(row) == {"tier": "community", "source": "dev", "verified": False, "commit": None,
                               "sha256": None, "grants": [], "revoked": False, "disabled": False}


def test_the_candidate_core_loaded_decides_provenance(library, tools, tmp_path):
    entry = library.publish()
    ok("webcli", "tools", "install", "toyhttp", "--yes")
    folder = make_tool(tmp_path / "work")
    (tools / "dev.toml").write_text(f'[toyhttp]\npath = "{folder}"\n')
    data = ok("webcli", "sites", "--format", "json")
    assert (site_row(data, "toyhttp")["source"], site_row(data, "toyhttp")["commit"]) == ("installed",
                                                                                          entry["commit"])
    [shadowed] = data["skipped"]
    assert (shadowed["name"], shadowed["source"], shadowed["commit"]) == ("toyhttp", "dev", None)
    assert "another tool's name" in shadowed["reason"]


def test_revoked_and_disabled_tools_are_listed_without_running_their_code(library, tools, tmp_path):
    marker = tmp_path / "imported"
    entry = library.publish(init_extra=f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')")
    ok("webcli", "tools", "install", "toyhttp", "--yes")
    library.revoked.append({"name": "toyhttp", "reason": "it posted without asking"})
    library.save()
    ok("webcli", "tools", "search")  # refreshes the cached index (this run loaded the tool before the refresh)
    marker.unlink(missing_ok=True)

    data = ok("webcli", "sites", "--format", "json")
    assert "toyhttp" not in {r["name"] for r in data["webcli"]}
    [skipped] = data["skipped"]
    assert {k: skipped[k] for k in ("name", "source", "revoked", "verified", "commit", "disabled")} == {
        "name": "toyhttp", "source": "installed", "revoked": "it posted without asking", "verified": False,
        "commit": entry["commit"], "disabled": False}
    assert "revoked" in skipped["reason"]
    assert "toyhttp" not in {c["name"].split()[0] for c in reference("webcli")["commands"]}
    assert not marker.exists()

    library.revoked.clear()
    library.save()
    ok("webcli", "tools", "search")
    ok("webcli", "tools", "disable", "toyhttp")
    marker.unlink(missing_ok=True)
    [skipped] = ok("webcli", "sites", "--format", "json")["skipped"]
    assert (skipped["reason"], skipped["disabled"], skipped["revoked"]) == ("disabled", True, False)
    assert not marker.exists()


def test_a_damaged_grants_file_fails_sites_closed(offline):
    (offline / "tools").mkdir(parents=True)
    (offline / "tools" / "grants.toml").write_text("toyhttp = [\n")
    result = run("webcli", "sites", "--format", "json")
    assert result.exit_code == 7 and "Can't read" in result.stderr  # no grant is assumed; nothing is listed


# The library's index as it is now (with official built-ins) parses, and its revoked list still refreshes. A 1.0.0rc1
# parser refused this index and kept its old cache, so revocations stopped arriving.

def test_the_live_index_with_built_ins_parses_and_refreshes_the_revoked_list(library, tools):
    live = json.loads(LIVE_INDEX.read_text())
    assert any(t.get("builtin") for t in live["tools"]) and live["revoked"] == []
    library.save(live)
    rows = ok("webcli", "tools", "search")
    assert {r["name"] for r in rows if r["tier"] == "official"} >= {"gmail", "gsc", "loom"}
    assert revoked.reason("httpbin", "0.1.0") is None

    library.save({**live, "revoked": [{"name": "httpbin", "reason": "revoked for this test"}]})
    rows = ok("webcli", "tools", "search")
    assert next(r for r in rows if r["name"] == "httpbin")["revoked"] == "revoked for this test"
    assert revoked.reason("httpbin", "0.1.0") == "revoked for this test"  # what core's loader reads
    cached = index_client.cached()
    assert cached is not None and cached.tools["gmail"].builtin
