"""The webcli 1.0 contract (Phase 0): the public adapter API, the new optional Site fields, the webcli-tool.toml
validator, and the placeholder commands the later phases fill in. Offline."""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

import webcli.adapter
from webcli.adapter import ADAPTER_API, NO_IDENTITY, Site, Strategy
from webcli.apps import webcli as webcli_app
from webcli.core import manifest_schema
from webcli.core.errors import ConfigError
from webcli.core.manifest_schema import (
    CORE_COMMANDS,
    ManifestError,
    loads,
    mismatches,
    validate,
)
from webcli.sites import BUILTIN, base, registry

DOCS = Path(__file__).resolve().parents[1] / "docs"
MINIMAL = """
name = "toyhttp"
version = "0.1.0"
adapter_api = 1
summary = "httpbin.org, read-only: one GET"
hosts = ["httpbin.org"]
maintainers = ["github:someone"]
license = "MIT"

[[commands]]
name = "get"
strategy = "public"
"""


def run(*args: str):
    return CliRunner().invoke(webcli_app.build(), list(args))


def minimal(**changes) -> dict:
    data = {"name": "toyhttp", "version": "0.1.0", "adapter_api": 1, "summary": "httpbin.org, read-only",
            "hosts": ["httpbin.org"], "maintainers": ["github:someone"], "license": "MIT",
            "commands": [{"name": "get", "strategy": "public"}]}
    data.update(changes)
    return {k: v for k, v in data.items() if v is not None}


# The adapter API.

def test_tools_import_the_adapter_api_from_one_module():
    assert ADAPTER_API == 1 and webcli.adapter.SUPPORTED_ADAPTER_APIS == (1,)
    assert (Site, Strategy, NO_IDENTITY) == (base.Site, base.Strategy, "none")
    missing = [name for name in webcli.adapter.__all__ if not hasattr(webcli.adapter, name)]
    assert missing == []


def test_registry_still_loads_every_built_in_with_the_new_fields_at_their_defaults():
    sites = registry()
    assert set(sites) == set(BUILTIN)
    for site in sites.values():
        assert (site.adapter_api, site.hosts, site.version) == (1, (), "")
        # Since 1.0.0rc2 a built-in that writes names its writing verbs (test_effects checks them against effects).
        assert site.sends_to_people <= site.writes <= set(site.verbs)
        assert not hasattr(site, "tier")
    assert (sites["loom"].writes, sites["gsc"].writes) == (frozenset(), {"submit_sitemap", "request_indexing"})


def test_describe_adds_the_api_always_and_the_rest_only_when_set():
    loom = registry()["loom"].describe()
    assert loom["adapterApi"] == 1
    assert not {"version", "hosts", "writes", "sendsToPeople"} & set(loom)

    site = Site(name="toy", kind="data", provider="toy", summary="s", cli=lambda: None, fetch="m", ops=(),
                hosts=("www.toy.example", "*.toy.example"), version="0.2.0",
                writes=frozenset({"post", "label add"}), sends_to_people=frozenset({"post"}))
    described = site.describe()
    assert {k: described[k] for k in ("adapterApi", "version", "hosts", "writes", "sendsToPeople")} == {
        "adapterApi": 1, "version": "0.2.0", "hosts": ["www.toy.example", "*.toy.example"],
        "writes": ["label add", "post"], "sendsToPeople": ["post"]}


def test_sites_json_still_lists_every_site():
    result = run("sites", "--format", "json")
    assert result.exit_code == 0
    out = json.loads(result.stdout)
    assert {s["name"] for s in out["llmcli"] + out["webcli"]} == set(BUILTIN)


# The manifest.

def test_a_minimal_manifest_is_valid():
    manifest = loads(MINIMAL)
    assert (manifest.name, manifest.version, manifest.adapter_api, manifest.identity, manifest.hosts,
            manifest.requires_webcli) == ("toyhttp", "0.1.0", 1, "toyhttp", ("httpbin.org",), "")
    assert manifest.default_identity == "toyhttp:default"
    (command,) = manifest.commands
    assert (command.name, command.strategy, command.writes, command.sends_to_people, command.requests) == \
        ("get", Strategy.PUBLIC, False, False, ())
    assert manifest.writes == manifest.sends_to_people == frozenset()


def test_the_documented_example_is_valid():
    text = (DOCS / "webcli-tool-manifest.md").read_text()
    example = re.search(r"```toml\n(.*?)```", text, re.DOTALL).group(1)
    manifest = loads(example)
    assert manifest.writes == {"label add"} and manifest.sends_to_people == frozenset()
    assert manifest.requires_webcli and manifest.default_identity == f"{manifest.identity}:default"


def test_writes_and_sends_and_no_identity():
    manifest = validate(minimal(identity="none", hosts=["*.example.com"], commands=[
        {"name": "list", "strategy": "cookie", "requests": ["GET api.example.com/items"]},
        {"name": "post", "strategy": "ui", "writes": True, "sends_to_people": True,
         "requests": ["POST www.example.com/graphql op=CreatePost (mutation)"]},
        {"name": "label add", "strategy": "cookie", "writes": True, "requests": ["PUT api.example.com/labels/1"]}]))
    assert manifest.writes == {"post", "label add"} and manifest.sends_to_people == {"post"}
    assert manifest.default_identity is None


@pytest.mark.parametrize(("changes", "problem"), [
    ({"name": None}, "name: missing"),
    ({"name": "toy-http"}, "name: must be 2-32 lowercase"),
    ({"name": "gmail"}, "is a built-in site or a webcli command"),
    ({"name": "tools"}, "is a built-in site or a webcli command"),
    ({"version": "1.0"}, "version: must be X.Y.Z"),
    ({"adapter_api": 2}, "adapter_api: this webcli supports 1"),
    ({"adapter_api": True}, "adapter_api: this webcli supports 1"),
    ({"requires_webcli": "1.0"}, "requires_webcli: must be a version range"),
    ({"summary": ""}, "summary: must be one line of text"),
    ({"identity": "loom:default"}, "identity: must be a provider name"),
    ({"hosts": []}, "hosts: must list at least one"),
    ({"hosts": ["https://httpbin.org"]}, "is not a lowercase host name"),
    ({"hosts": ["httpbin.org", "httpbin.org"]}, "'httpbin.org' is listed twice"),
    ({"maintainers": ["someone"]}, "must be github:<login>"),
    ({"homepage": "https://x"}, "unknown key 'homepage'"),
    ({"commands": []}, "commands: must list at least one"),
    ({"commands": [{"name": "get", "strategy": "rest"}]}, 'command "get": strategy: must be one of'),
    ({"commands": [{"name": "Get", "strategy": "public"}]}, 'command "Get": name: must be one or two'),
    ({"commands": [{"name": "get", "strategy": "public", "write": True}]}, "unknown key 'write'"),
    ({"commands": [{"name": "get", "strategy": "public", "writes": "yes"}]}, "writes: must be true or false"),
    ({"commands": [{"name": "get", "strategy": "public", "writes": 1}]}, "writes: must be true or false"),
    ({"commands": [{"name": "dm", "strategy": "ui", "sends_to_people": True}]}, "needs writes = true"),
    ({"commands": [{"name": "get", "strategy": "public", "requests": ["GET evil.example/x"]}]},
     "host evil.example is not in hosts"),
    ({"commands": [{"name": "get", "strategy": "public", "requests": ["httpbin.org/get"]}]},
     "must start with a method"),
    ({"commands": [{"name": "get", "strategy": "public", "requests": ["DELETE httpbin.org/anything"]}]},
     "a read command (writes = false) can't make a write request"),
    ({"commands": [{"name": "get", "strategy": "cookie", "requests": ["POST httpbin.org/graphql (mutation)"]}]},
     "a read command (writes = false) can't make a write request"),
    ({"commands": [{"name": "get", "strategy": "public"}, {"name": "get", "strategy": "public"}]},
     'command "get": listed twice'),
    # A trailing newline is not the same name.
    ({"name": "tools\n"}, "name: must be 2-32 lowercase"),
    ({"version": "0.1.0\n"}, "version: must be X.Y.Z"),
    ({"requires_webcli": ">=1.0\n"}, "requires_webcli: must be a version range"),
    ({"identity": "google\n"}, "identity: must be a provider name"),
    ({"hosts": ["httpbin.org\n"]}, "is not a lowercase host name"),
    ({"maintainers": ["github:someone\n"]}, "must be github:<login>"),
    ({"commands": [{"name": "get\n", "strategy": "public"}]}, "name: must be one or two"),
])
def test_a_manifest_that_breaks_a_rule_is_refused(changes, problem):
    with pytest.raises(ManifestError) as caught:
        validate(minimal(**changes))
    assert any(problem in p for p in caught.value.problems), caught.value.problems
    assert isinstance(caught.value, ConfigError)


def test_every_problem_is_listed_at_once():
    with pytest.raises(ManifestError) as caught:
        validate(minimal(name="Bad", version="x", license=None))
    assert [p.split(":")[0] for p in caught.value.problems] == ["license", "name", "version"]
    assert str(caught.value).startswith("webcli-tool.toml is not a valid tool manifest:\n  - license: missing")
    assert caught.value.hint.endswith("docs/webcli-tool-manifest.md")


def test_an_escaped_newline_in_toml_does_not_slip_a_reserved_name_through():
    with pytest.raises(ManifestError) as caught:
        loads(MINIMAL.replace('name = "toyhttp"', 'name = "tools\\n"'))
    assert caught.value.problems == [("name: must be 2-32 lowercase letters and digits, starting with a letter (no "
                                      "hyphens: it is also the tool's identity, <name>:default)")]


def test_bad_toml_is_a_manifest_error():
    with pytest.raises(ManifestError) as caught:
        loads('name = "toy"\nhosts = [', source="x/webcli-tool.toml")
    assert caught.value.source == "x/webcli-tool.toml" and caught.value.problems[0].startswith("not valid TOML")


def test_load_reads_a_folder_or_a_file_and_the_module_checks_files(tmp_path, capsys):
    good = tmp_path / "webcli-tool-toyhttp"
    good.mkdir()
    (good / "webcli-tool.toml").write_text(MINIMAL)
    bad = tmp_path / "bad.toml"
    bad.write_text(MINIMAL.replace('"public"', '"rest"'))
    assert manifest_schema.load(good).name == manifest_schema.load(good / "webcli-tool.toml").name == "toyhttp"
    assert manifest_schema.main([str(good)]) == 0
    assert manifest_schema.main([str(good), str(bad)]) == 7
    assert manifest_schema.main([str(tmp_path / "missing.toml")]) == 7
    err = capsys.readouterr().err
    assert "bad.toml is not a valid tool manifest" in err and "can't read it" in err


def test_mismatches_compare_the_adapter_with_its_manifest():
    manifest = validate(minimal(commands=[
        {"name": "get", "strategy": "public"},
        {"name": "post", "strategy": "public", "writes": True, "requests": ["POST httpbin.org/post"]}]))
    site = Site(name="toyhttp", kind="data", provider="toyhttp", summary="s", cli=lambda: None, fetch="m",
                ops=("get",), verbs={"get": Strategy.PUBLIC, "post": Strategy.PUBLIC}, hosts=("httpbin.org",),
                version="0.1.0", writes=frozenset({"post"}))
    assert mismatches(manifest, site) == []
    wider = dataclasses.replace(site, hosts=("httpbin.org", "evil.example"), writes=frozenset())
    assert mismatches(manifest, wider) == [
        "hosts: the adapter says ['evil.example', 'httpbin.org'], the manifest ['httpbin.org']",
        "writes: the adapter says [], the manifest ['post']"]


# The Phase 0 placeholder commands are all built now (B/C/D). Help and agent-help still list them.

def test_submit_help_works():
    result = run("submit", "--help")
    assert result.exit_code == 0
    assert "pull request" in result.stdout.lower() or "webcli-tools" in result.stdout.lower() or "submit" in result.stdout.lower()


def test_tools_without_a_subcommand_shows_its_help():
    result = run("tools")
    assert result.exit_code == 2 and "install" in result.stderr and "revoke-grant" in result.stderr


def test_reserved_names_are_exactly_the_core_commands():
    sites = {site.name for site in registry().values() if site.kind == "data"}
    assert set(webcli_app.build().commands) - sites == CORE_COMMANDS


def test_agent_help_lists_the_new_commands():
    ref = json.loads(run("agent-help", "--format", "json").stdout)
    usage = {c["name"]: c["usage"] for c in ref["commands"]}
    assert usage["new"] == "webcli new [--url URL] [--identity ID] [--capture] NAME"
    assert usage["tools install"] == "webcli tools install [OPTIONS] NAME"
    assert usage["tools list"] == "webcli tools list [OPTIONS]"
    assert {"dev", "test", "lint", "submit", "tools search", "tools enable"} <= set(usage)
