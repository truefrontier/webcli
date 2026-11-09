"""webcli 1.0 Phase C: `webcli tools` against a fake webcli-tools library (a file:// index.json and tarballs on disk,
pointed at by $WEBCLI_TOOLS_INDEX_URL and $WEBCLI_TOOLS_ARCHIVE_URL). No network."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner
from test_tool_trust import make_tool

from webcli import sites
from webcli.apps import webcli as webcli_app
from webcli.core import daemon, grants, index_client
from webcli.core.errors import ConfigError
from webcli.core.identities import Identity


@pytest.fixture(autouse=True)
def fresh_registry():
    sites.reload()
    yield
    sites.reload()


class Library:
    """A fake webcli-tools repo: publish() makes a tool folder, tars it the way GitHub does (one top folder,
    <repo>-<commit>/tools/<name>/...), and lists it in index.json at that commit."""

    def __init__(self, root: Path) -> None:
        self.root, self.tools, self.revoked = root, {}, []
        root.mkdir(parents=True)
        self.save()

    def publish(self, name: str = "toyhttp", *, hosts: tuple[str, ...] = ("httpbin.org",), version: str = "0.1.0",
                requires: str = "", extra_file: tuple[str, str] | None = None, symlink: bool = False, **kw) -> dict:
        commit = hashlib.sha1(f"{name}{version}{hosts}{requires}{len(self.tools)}{extra_file}".encode()).hexdigest()
        src = make_tool(self.root / "src" / commit / "tools", name, version=version,
                        adapter_hosts=repr(tuple(hosts)), **kw)
        toml = src / "webcli-tool.toml"
        text = toml.read_text().replace('hosts = ["httpbin.org"]', f"hosts = {json.dumps(list(hosts))}")
        toml.write_text(text.replace("adapter_api = 1", f'adapter_api = 1\nrequires_webcli = "{requires}"')
                        if requires else text)
        if extra_file:
            (src / extra_file[0]).write_text(extra_file[1])
        entry = {"name": name, "version": version, "summary": "a toy tool", "commit": commit,
                 "sha256": index_client.tree_sha256(src), "verified": False}
        if symlink:
            (src / "link").symlink_to("/etc/hosts")
        with tarfile.open(self.root / f"{commit}.tar.gz", "w:gz") as tar:
            tar.add(src, arcname=f"webcli-tools-{commit}/tools/{name}")
            readme = b"not a tool"
            info = tarfile.TarInfo(f"webcli-tools-{commit}/README.md")
            info.size = len(readme)
            tar.addfile(info, io.BytesIO(readme))
        self.tools[name] = entry
        self.save()
        return entry

    def builtin(self, name: str = "loom", **extra) -> dict:
        """An official built-in's catalog entry: no commit, sha256, or path, and no tarball."""
        entry = {"name": name, "version": "1.0.0rc1", "summary": f"{name}, read-only", "tier": "official",
                 "builtin": True, "verified": True, "requires_webcli": ">=1.0.0rc1,<2", "source": "core",
                 "identity": name, "hosts": [], "commands": [{"name": "list", "strategy": "cookie", "writes": False}],
                 **extra}
        self.tools[name] = entry
        self.save()
        return entry

    def save(self, data: dict | None = None) -> None:
        index = {"tools": list(self.tools.values()), "revoked": self.revoked} if data is None else data
        (self.root / "index.json").write_text(json.dumps(index))


@pytest.fixture
def library(offline, tmp_path, monkeypatch) -> Library:
    lib = Library(tmp_path / "library")
    monkeypatch.setenv("WEBCLI_TOOLS_INDEX_URL", (lib.root / "index.json").as_uri())
    monkeypatch.setenv("WEBCLI_TOOLS_ARCHIVE_URL", lib.root.as_uri() + "/{commit}.tar.gz")
    return lib


@pytest.fixture
def tools(offline) -> Path:
    return offline / "tools"


def run(*args: str, input: str | None = None):
    sites.reload()
    return CliRunner().invoke(webcli_app.build(), list(args), input=input)


def ok(*args: str, input: str | None = None):
    result = run(*args, input=input)
    assert result.exit_code == 0, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else None


def installed_rows() -> list[dict]:
    """`webcli tools list` without the built-ins it lists after the installed tools."""
    return [r for r in ok("tools", "list") if r["status"] != "builtin"]


def leftovers(tools: Path) -> list[str]:
    return sorted(p.name for p in tools.iterdir() if p.name.startswith(".")) if tools.is_dir() else []


# The DoD path: install -> list -> grant -> revoke -> disable -> revoked in the index is refused.

def test_install_list_grant_revoke_disable_then_a_revocation_in_the_index_turns_it_off(library, tools):
    entry = library.publish()

    installed = ok("tools", "install", "toyhttp", "--yes")
    assert installed["installed"] == "toyhttp" and installed["loaded"] is True
    assert installed["commit"] == entry["commit"]
    assert installed["next"][0] == "webcli identity login toyhttp:default"
    assert json.loads((tools / "toyhttp" / ".installed.json").read_text())["sha256"] == entry["sha256"]
    assert run("toyhttp", "get").stdout == "got\n"
    assert ok("toyhttp", "label", "add")["dryRun"] is True  # the manifest's write is gated

    rows = installed_rows()
    assert [(r["name"], r["version"], r["status"], r["commit"]) for r in rows] == [
        ("toyhttp", "0.1.0", "loaded", entry["commit"][:12])]

    assert ok("tools", "grant", "toyhttp", "google:default") == {
        "tool": "toyhttp", "identity": "google:default", "granted": True, "changed": True}
    assert grants.is_granted("toyhttp", "google:default")
    assert ok("tools", "list")[0]["grants"] == "google:default"
    assert ok("tools", "revoke-grant", "toyhttp", "google:default")["changed"] is True
    assert not grants.is_granted("toyhttp", "google:default")

    assert ok("tools", "disable", "toyhttp") == {"tool": "toyhttp", "enabled": False, "changed": True}
    assert ok("tools", "list")[0]["status"] == "disabled"
    assert run("toyhttp", "get").exit_code == 2  # no such command while it's off
    assert ok("tools", "enable", "toyhttp")["loaded"] is True

    library.revoked.append({"name": "toyhttp", "reason": "it posted without asking"})
    library.save()
    assert ok("tools", "search")[0]["revoked"] == "it posted without asking"  # refreshes the cached index
    assert "it posted without asking" in ok("tools", "list")[0]["status"]
    assert sites.tier_of("toyhttp") is None

    ok("tools", "remove", "toyhttp")
    result = run("tools", "install", "toyhttp", "--yes")
    assert result.exit_code == 2 and "revoked" in result.stderr and not (tools / "toyhttp").exists()


def test_install_shows_the_manifest_and_asks(library, tools):
    library.publish(identity="google", hosts=("httpbin.org", "*.example.com"))

    result = run("tools", "install", "toyhttp", input="n\n")
    assert result.exit_code == 2 and "was not installed" in result.stderr
    assert "Reaches:     httpbin.org, *.example.com" in result.stderr
    assert "label add" in result.stderr and "WRITES" in result.stderr
    assert "webcli tools grant toyhttp google:default" in result.stderr  # not its own identity
    assert "runs as you" in result.stderr
    assert not (tools / "toyhttp").exists() and leftovers(tools) == []

    result = run("tools", "install", "toyhttp", input="")  # no answer is no
    assert result.exit_code == 2 and not (tools / "toyhttp").exists()

    result = run("tools", "install", "toyhttp", input="y\n")
    assert result.exit_code == 0 and json.loads(result.stdout)["manifest"]["identityIsOwn"] is False
    assert sites.tier_of("toyhttp") == "community"


def test_info_shows_the_real_manifest_without_installing_or_running_it(library, tools):
    marker = tools.parent / "ran"
    library.publish(init_extra=f"open({str(marker)!r}, 'w').close()")
    info = ok("tools", "info", "toyhttp")
    assert info["hosts"] == ["httpbin.org"] and info["identity"] == "toyhttp:default"
    assert {c["name"]: c["writes"] for c in info["commands"]} == {"get": False, "label add": True}
    assert info["installed"] is None and info["revoked"] is None
    assert not (tools / "toyhttp").exists() and not marker.exists() and leftovers(tools) == []
    assert run("tools", "info", "nosuchtool").exit_code == 3


def test_search_filters_and_caches_the_index(library, tools):
    library.publish("toyhttp")
    library.publish("weather")
    assert [r["name"] for r in ok("tools", "search")] == ["toyhttp", "weather"]
    assert [r["name"] for r in ok("tools", "search", "WEA")] == ["weather"]
    assert ok("tools", "search", "zzz") == []
    assert json.loads((tools / "index-cache.json").read_text())["tools"][1]["name"] == "weather"


# What install refuses.

def test_a_file_that_doesnt_match_the_index_hash_is_refused(library, tools):
    library.publish()
    library.tools["toyhttp"]["sha256"] = "0" * 64
    library.save()
    result = run("tools", "install", "toyhttp", "--yes")
    assert result.exit_code == 7 and "doesn't match the index's sha256" in result.stderr
    assert not (tools / "toyhttp").exists() and leftovers(tools) == []


def test_a_link_in_the_tool_is_refused(library, tools):
    library.publish(symlink=True)
    result = run("tools", "install", "toyhttp", "--yes")
    assert result.exit_code == 7 and "link or special file" in result.stderr
    assert not (tools / "toyhttp").exists() and leftovers(tools) == []


def test_a_manifest_that_disagrees_with_the_index_is_refused(library, tools):
    library.publish()
    library.tools["toyhttp"]["version"] = "0.9.0"
    library.save()
    result = run("tools", "install", "toyhttp", "--yes")
    assert result.exit_code == 7 and "its manifest says toyhttp 0.1.0" in result.stderr
    assert not (tools / "toyhttp").exists()


def test_a_tool_that_needs_a_newer_webcli_is_refused(library, tools):
    library.publish(requires=">=99")
    result = run("tools", "install", "toyhttp", "--yes")
    assert result.exit_code == 2 and "needs webcli >=99" in result.stderr and not (tools / "toyhttp").exists()


@pytest.mark.parametrize("name", ["google", "loom"])  # a provider; a built-in site listed as if it were community
def test_a_community_entry_with_a_built_in_name_is_refused_before_download(library, tools, name):
    library.publish(name)
    result = run("tools", "install", name, "--yes")
    assert result.exit_code == 2 and "can't replace it" in result.stderr and leftovers(tools) == []


# Official built-ins: catalogued in the index, shipped inside webcli, never downloaded.

@pytest.fixture
def no_download(monkeypatch):
    def refuse(entry, dest):
        raise AssertionError(f"downloaded {entry.name}")

    monkeypatch.setattr(index_client, "download", refuse)


def test_search_lists_built_ins_next_to_community_tools(library, tools):
    library.publish("toyhttp")
    library.builtin("loom")
    rows = {r["name"]: r for r in ok("tools", "search")}
    assert (rows["loom"]["tier"], rows["loom"]["installed"], rows["loom"]["verified"]) == ("official", "builtin", True)
    assert (rows["toyhttp"]["tier"], rows["toyhttp"]["installed"]) == ("community", "")
    assert [r["name"] for r in ok("tools", "search", "loom")] == ["loom"]
    assert "builtin" in run("tools", "search", "loom", "--format", "table").stdout


def test_info_on_a_built_in_shows_the_catalog_without_downloading(library, tools, no_download):
    library.builtin("loom", docs="https://github.com/truefrontier/webcli/blob/main/docs/social-data-sites.md")
    info = ok("tools", "info", "loom")
    assert info == {"name": "loom", "version": "1.0.0rc1", "summary": "loom, read-only", "tier": "official",
                    "builtin": True, "verified": True, "identity": "loom:default", "hosts": [],
                    "commands": [{"name": "list", "strategy": "cookie", "writes": False}],
                    "requiresWebcli": ">=1.0.0rc1,<2",
                    "docs": "https://github.com/truefrontier/webcli/blob/main/docs/social-data-sites.md",
                    "installed": "builtin", "revoked": None}
    text = run("tools", "info", "loom", "--format", "text").stdout
    assert "Built into webcli (official): nothing to install." in text and "Signs in as: loom:default" in text
    assert leftovers(tools) == []


def test_install_of_a_built_in_is_a_no_op(library, tools, no_download):
    library.builtin("loom")
    result = ok("tools", "install", "loom")
    assert (result["installed"], result["changed"], result["next"]) == ("builtin", False, ["webcli loom --help"])
    assert not (tools / "loom").exists() and leftovers(tools) == []
    assert sites.tier_of("loom") == "official"


def test_a_built_in_this_webcli_doesnt_ship_points_at_updating_webcli(library, tools, no_download):
    library.builtin("futuresite", requires_webcli=">=9.0.0,<10")
    result = run("tools", "install", "futuresite")
    assert result.exit_code == 2 and "built into webcli >=9.0.0,<10" in result.stderr
    assert "pip install --pre -U truefrontier-webcli" in result.stderr and not (tools / "futuresite").exists()
    assert ok("tools", "info", "futuresite")["installed"] is None
    assert ok("tools", "search", "futuresite")[0]["installed"] == ""


@pytest.mark.parametrize("command", [["update", "loom"], ["remove", "loom"], ["disable", "loom"], ["enable", "loom"]])
def test_managing_a_built_in_is_a_usage_error(library, tools, command):
    library.builtin("loom")
    result = run("tools", *command)
    assert result.exit_code == 2 and "loom is built into webcli; it isn't an installed tool" in result.stderr


@pytest.mark.parametrize("listed", ["builtin", "publish"])  # the catalog entry, or a community entry with its name
def test_update_never_downloads_over_a_leftover_folder_with_a_built_ins_name(library, tools, no_download, listed):
    getattr(library, listed)("loom")
    (tools / "loom").mkdir(parents=True)
    assert ok("tools", "update", "--all") == [{"name": "loom", "status": "built in"}]
    result = run("tools", "update", "loom")
    assert result.exit_code == 2 and "no tool to update" in result.stderr


def test_update_all_with_nothing_installed(library, tools):
    library.builtin("loom")
    assert ok("tools", "update", "--all") == []


def test_list_shows_installed_tools_then_the_official_data_built_ins(library, tools):
    library.publish()
    ok("tools", "install", "toyhttp", "--yes")
    rows = ok("tools", "list")
    assert rows[0]["name"] == "toyhttp"
    builtins = [r["name"] for r in rows if r["status"] == "builtin"]
    assert builtins == sorted(n for n, s in sites.registry().items()
                              if sites.tier_of(n) == "official" and s.kind == "data")
    assert "loom" in builtins and not {"chatgpt", "gemini", "muse"} & set(builtins)


@pytest.mark.parametrize("name", ["../evil", "tool-name", "Gmail"])
def test_a_name_that_isnt_a_tool_name_never_becomes_a_path(library, tools, name):
    for command in (["install", name, "--yes"], ["remove", name], ["disable", name], ["grant", name, "x:default"]):
        result = run("tools", *command)
        assert result.exit_code == 2 and "is not a tool name" in result.stderr


def test_installing_twice_points_at_update(library, tools):
    library.publish()
    ok("tools", "install", "toyhttp", "--yes")
    result = run("tools", "install", "toyhttp", "--yes")
    assert result.exit_code == 2 and "webcli tools update toyhttp" in result.stderr


# The index and the cache.

def test_offline_search_uses_the_cache_and_update_refuses(library, tools, monkeypatch):
    library.publish()
    ok("tools", "install", "toyhttp", "--yes")
    monkeypatch.setenv("WEBCLI_TOOLS_INDEX_URL", (library.root / "gone.json").as_uri())
    result = run("tools", "search")
    assert result.exit_code == 0 and json.loads(result.stdout)[0]["name"] == "toyhttp"
    assert "Using the copy cached" in result.stderr
    result = run("tools", "update", "toyhttp")
    assert result.exit_code == 7 and "Can't download" in result.stderr


BUILTIN = {"name": "loom", "version": "1.0.0rc1", "summary": "Loom", "tier": "official", "builtin": True,
           "identity": "loom", "hosts": [], "commands": [{"name": "list", "strategy": "cookie", "writes": False}]}


@pytest.mark.parametrize("index", [
    {"tools": []},  # no revoked list: a cut-off index must not clear revocations
    {"tools": [], "revoked": [{"name": "toyhttp", "versions": [1]}]},
    {"tools": [], "revoked": [{"name": "toyhttp", "versions": ["not-a-version"]}]},
    {"tools": [], "revoked": ["Toy-HTTP"]},
    {"tools": [{"name": "toyhttp", "version": "0.1.0", "commit": "main", "sha256": "0" * 64}], "revoked": []},
    {"tools": [{"name": "toy-http", "version": "0.1.0", "commit": "a" * 40, "sha256": "0" * 64}], "revoked": []},
    {"tools": [{"name": "toyhttp", "version": "0.1.0", "commit": "a" * 40, "sha256": "0" * 64,
                "path": "../../etc"}], "revoked": []},
    # a community entry that calls itself official, or says builtin without meaning it
    {"tools": [{"name": "toyhttp", "version": "0.1.0", "commit": "a" * 40, "sha256": "0" * 64,
                "tier": "official"}], "revoked": []},
    {"tools": [{"name": "toyhttp", "version": "0.1.0", "commit": "a" * 40, "sha256": "0" * 64,
                "builtin": "yes"}], "revoked": []},
    # a half-broken built-in
    {"tools": [{**BUILTIN, "commit": "a" * 40, "sha256": "0" * 64}], "revoked": []},
    {"tools": [{**BUILTIN, "tier": "community"}], "revoked": []},
    {"tools": [{k: v for k, v in BUILTIN.items() if k != "commands"}], "revoked": []},
    {"tools": [{**BUILTIN, "commands": [{"name": "list", "strategy": "cookie", "writes": "no"}]}], "revoked": []},
    {"tools": [{**BUILTIN, "identity": None}], "revoked": []},
])
def test_a_broken_index_never_replaces_the_cache(library, tools, index):
    library.revoked.append("oldtool")
    library.save()
    ok("tools", "search")
    library.save(index)
    result = run("tools", "search")
    assert result.exit_code == 0 and "not a valid webcli-tools index" in result.stderr
    assert json.loads((tools / "index-cache.json").read_text())["revoked"] == ["oldtool"]


def test_with_no_cache_a_broken_index_is_an_error(library):
    library.save({"tools": "nope", "revoked": []})
    result = run("tools", "search")
    assert result.exit_code == 7 and "not a valid webcli-tools index" in result.stderr


# Update.

def test_update_without_new_hosts_writes_or_identity_doesnt_ask(library, tools):
    library.publish()
    ok("tools", "install", "toyhttp", "--yes")
    assert ok("tools", "update", "toyhttp")["status"] == "up to date"
    entry = library.publish(version="0.2.0")
    result = ok("tools", "update", "toyhttp")
    assert (result["status"], result["from"], result["to"], result["changes"]) == ("updated", "0.1.0", "0.2.0", [])
    assert sites.registry()["toyhttp"].version == "0.2.0"
    assert json.loads((tools / "toyhttp" / ".installed.json").read_text())["commit"] == entry["commit"]
    assert leftovers(tools) == []


def test_update_that_wants_a_new_host_asks_and_a_no_keeps_the_old_version(library, tools):
    library.publish()
    ok("tools", "install", "toyhttp", "--yes")
    ok("tools", "disable", "toyhttp")
    library.publish(version="0.2.0", hosts=("httpbin.org", "evil.example.com"))

    result = run("tools", "update", "toyhttp", input="n\n")
    assert result.exit_code == 2 and "new hosts: evil.example.com" in result.stderr
    assert ok("tools", "list")[0]["version"] == "0.1.0" and leftovers(tools) == []

    result = ok("tools", "update", "--all", "--yes")
    assert [(r["name"], r["status"], r["changes"]) for r in result] == [
        ("toyhttp", "updated", ["new hosts: evil.example.com"])]
    row = ok("tools", "list")[0]
    assert (row["version"], row["status"]) == ("0.2.0", "disabled")  # still off after the update


def test_update_names_new_writes_and_a_new_identity(library, tools):
    library.publish()
    ok("tools", "install", "toyhttp", "--yes")
    library.publish(version="0.2.0", identity="google")
    result = run("tools", "update", "--all", input="n\n")
    assert result.exit_code == 0 and json.loads(result.stdout)[0]["status"] == "declined"
    assert "signs in as google:default instead of toyhttp:default" in result.stderr


def test_update_needs_a_name_or_all(library):
    assert run("tools", "update").exit_code == 2
    assert run("tools", "update", "toyhttp", "--all").exit_code == 2
    assert run("tools", "update", "toyhttp").exit_code == 3


def test_a_named_update_that_cant_happen_exits_nonzero_but_all_reports_it(library, tools):
    library.publish()
    ok("tools", "install", "toyhttp", "--yes")
    library.revoked.append("toyhttp")
    library.save()
    result = run("tools", "update", "toyhttp")
    assert result.exit_code == 2 and "toyhttp is revoked" in result.stderr
    assert ok("tools", "update", "--all")[0]["status"] == "revoked"
    library.revoked.clear()
    library.tools.clear()
    library.save()
    assert run("tools", "update", "toyhttp").exit_code == 3
    assert ok("tools", "update", "--all") == [{"name": "toyhttp", "status": "not in library"}]


def test_an_interrupted_update_swap_puts_the_old_version_back(library, tools, monkeypatch):
    library.publish()
    ok("tools", "install", "toyhttp", "--yes")
    library.publish(version="0.2.0")
    real = Path.rename

    def interrupted(self, target):
        if self.name.startswith(".stage-"):
            raise KeyboardInterrupt
        return real(self, target)

    monkeypatch.setattr(Path, "rename", interrupted)
    assert run("tools", "update", "toyhttp").exit_code != 0
    monkeypatch.setattr(Path, "rename", real)
    assert ok("tools", "list")[0]["version"] == "0.1.0" and leftovers(tools) == []


# Remove, grants.

def test_remove_deletes_the_tool_and_its_grants(library, tools):
    library.publish()
    ok("tools", "install", "toyhttp", "--yes")
    ok("tools", "grant", "toyhttp", "google:default")
    assert ok("tools", "remove", "toyhttp") == {"removed": "toyhttp", "grantsRevoked": ["google:default"]}
    assert not (tools / "toyhttp").exists() and grants.list_grants() == {}
    assert installed_rows() == []
    assert run("tools", "remove", "toyhttp").exit_code == 3


def test_install_drops_grants_left_under_its_name(library, tools):
    grants.grant("toyhttp", "google:default")  # from a dev tool of that name, or a remove that couldn't edit grants
    library.publish(identity="google")
    result = run("tools", "install", "toyhttp", "--yes")
    assert result.exit_code == 0, result.stderr
    assert "it can't use your google sign-in until you run `webcli tools grant" in result.stderr
    assert json.loads(result.stdout)["grantsDropped"] == ["google:default"]
    assert "dropped" in result.stderr and grants.list_grants() == {}
    with pytest.raises(ConfigError, match="only if you grant it"):  # the install prompt said so
        daemon.site_op(Identity("google:default"), "toyhttp", "get")


def test_an_install_that_cant_drop_the_old_grants_installs_nothing(library, tools, monkeypatch):
    # The grants go before the folder goes in place, so a grants.toml that can't be written never leaves the new
    # tool installed with the old tool's grants.
    grants.grant("toyhttp", "google:default")
    library.publish(identity="google")

    def full_disk(_grants):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(grants, "_save", full_disk)
    result = run("tools", "install", "toyhttp", "--yes")
    assert not (tools / "toyhttp").exists() and leftovers(tools) == []
    assert grants.list_grants() == {"toyhttp": ["google:default"]}  # untouched, and no tool by that name to use them
    assert result.exit_code == 7 and "Can't drop the grants left under the name toyhttp" in result.stderr


def test_grant_needs_an_installed_tool_and_its_own_identity_needs_none(library, tools):
    assert run("tools", "grant", "toyhttp", "google:default").exit_code == 3
    library.publish()
    ok("tools", "install", "toyhttp", "--yes")
    assert ok("tools", "grant", "toyhttp", "toyhttp:work")["changed"] is False
    assert grants.list_grants() == {}
    assert run("tools", "grant", "toyhttp", "not an identity").exit_code == 7


def test_the_sha256_command_matches_what_install_checks(library, tools, capsys):
    entry = library.publish()
    folder = library.root / "src" / entry["commit"] / "tools" / "toyhttp"
    assert index_client.main(["sha256", str(folder)]) == 0
    assert capsys.readouterr().out == f"{entry['sha256']}  {folder}\n"
