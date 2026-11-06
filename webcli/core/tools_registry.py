"""Community tools: where they live, and how one is checked and loaded. The site registry (webcli/sites/__init__.py)
decides the order, the names, and what to skip; this module turns one tool folder into a Site, or raises.

    ~/.webcli/tools/              0700
      <name>/                     an installed tool (`webcli tools install`): its webcli-tool.toml and its package,
        webcli-tool.toml          webcli_tool_<name>/ (whose __init__ holds ADAPTER = Site(...))
        webcli_tool_<name>/
        .disabled                 present: the tool is off (`webcli tools disable`); its code is never imported
        .installed.json           what install pinned: version, commit, sha256 (`webcli sites` rechecks the files)
      dev.toml                    in-progress tools (`webcli new`): [<name>] path = "/abs/path/webcli-tool-<name>"
      grants.toml                 identity grants (core/grants.py)
      index-cache.json            the library's index.json, cached; its "revoked" list (core/revoked.py)
      revoked.json                more revocations, same shape

Folders starting with "." are skipped (an install in progress). docs/tool-trust.md has the rules in plain words.

Loading never adds a folder to sys.path: the tool's package is imported from its folder by path, so a stray json.py
next to it can't shadow anything. A tool's code runs only after its manifest is valid and it isn't disabled or
revoked; after import, its ADAPTER must match its manifest (manifest_schema.mismatches).
"""

from __future__ import annotations

import dataclasses
import functools
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Callable, NamedTuple

import click

from webcli.core import config, manifest_schema, output, revoked
from webcli.core.errors import ConfigError
from webcli.core.output import EXIT_OK, emit_json, warn
from webcli.sites.base import Site

DIR_NAME = "tools"
DEV_FILE = "dev.toml"
DISABLED = ".disabled"
INSTALLED = ".installed.json"  # what install pinned: version, commit, sha256, index, installedAt
OFF_ENV = "WEBCLI_NO_TOOLS"  # "1": load no community tools at all (a way out when one hangs at import)

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


class Candidate(NamedTuple):
    name: str
    folder: Path
    source: str  # "installed" or "dev"


def tools_dir() -> Path:
    """~/.webcli/tools (not created)."""
    return config.home() / DIR_NAME


def ensure_tools_dir() -> Path:
    return config.private_dir(config.ensure_home() / DIR_NAME)


def enabled() -> bool:
    return os.environ.get(OFF_ENV, "").strip().lower() not in ("1", "true", "yes", "on")


def is_disabled(folder: Path) -> bool:
    return (folder / DISABLED).exists()


def _dev_tools() -> list[Candidate]:
    path = tools_dir() / DEV_FILE
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        warn(f"webcli: ignoring {path}: {exc}")
        return []
    out = []
    for name, entry in sorted(data.items()):
        folder = entry.get("path") if isinstance(entry, dict) else None
        if isinstance(folder, str) and folder:
            out.append(Candidate(name, Path(folder).expanduser(), "dev"))
        else:
            warn(f"webcli: ignoring [{name}] in {path}: it needs path = \"/path/to/webcli-tool-{name}\"")
    return out


def candidates() -> list[Candidate]:
    """Every community tool to try, installed ones first (by name), then dev.toml's. Nothing imported yet."""
    if not enabled():
        return []
    root = tools_dir()
    installed = [Candidate(p.name, p, "installed") for p in sorted(root.iterdir())
                 if p.is_dir() and not p.name.startswith(".")] if root.is_dir() else []
    return installed + _dev_tools()


def installed_record(folder: Path) -> dict:
    """What `webcli tools install` pinned for the tool in `folder` ({} when there's no readable record)."""
    try:
        data = json.loads((folder / INSTALLED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def provenance(candidate: Candidate, granted: dict[str, list[str]]) -> dict:
    """Where a community tool came from and what vouches for it, for `webcli sites`: read from its manifest, install
    record, the cached library index, grants.toml, and the revoked lists; none of its code is imported.

    verified is true only for an installed tool the cached index marks verified at exactly the version, commit, and
    sha256 install pinned, whose files on disk still hash to that sha256. A dev tool is never verified."""
    name, folder = candidate.name, candidate.folder
    try:
        version = manifest_schema.load(folder).version
    except ConfigError:
        version = None
    record = installed_record(folder) if candidate.source == "installed" else {}
    why = revoked.reason(name, version)
    return {"source": candidate.source, "verified": not why and _verified(candidate, version, record),
            "commit": record.get("commit"), "sha256": record.get("sha256"), "grants": granted.get(name, []),
            "revoked": why or False, "disabled": is_disabled(folder)}


def _verified(candidate: Candidate, version: str | None, record: dict) -> bool:
    from webcli.core import index_client

    if candidate.source != "installed" or version is None:
        return False
    index = index_client.cached()
    entry = index.tools.get(candidate.name) if index else None
    if entry is None or not entry.verified or (entry.version, entry.commit, entry.sha256) != \
            (version, record.get("commit"), record.get("sha256")):
        return False
    try:
        return index_client.tree_sha256(candidate.folder, added=(INSTALLED, DISABLED)) == entry.sha256
    except (ConfigError, OSError):
        return False


def _import(name: str, folder: Path) -> object:
    """webcli_tool_<name>.ADAPTER, imported from `folder` by path. Any earlier copy (and its submodules) is dropped
    first, so a reload sees the tool's current code."""
    package = f"webcli_tool_{name}"
    init = folder / package / "__init__.py"
    if not init.is_file():
        raise ConfigError(f"{folder} has no {package}/__init__.py.")
    for loaded in [m for m in sys.modules if m == package or m.startswith(package + ".")]:
        del sys.modules[loaded]
    spec = importlib.util.spec_from_file_location(package, init, submodule_search_locations=[str(init.parent)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[package] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(package, None)
        raise
    if not hasattr(module, "ADAPTER"):
        raise ConfigError(f"{package} has no ADAPTER.")
    return module.ADAPTER


def load(candidate: Candidate) -> Site:
    """The tool's Site, checked against its manifest, with its cli wrapped (see guarded_cli). Raises on anything
    wrong; the registry turns that into a warning and a skip."""
    name, folder = candidate.name, candidate.folder
    manifest = manifest_schema.load(folder)
    if manifest.name != name:
        raise ConfigError(f"its manifest is named {manifest.name!r}, not {name!r}.")
    why = revoked.reason(name, manifest.version)
    if why:
        raise ConfigError(f"{name} {manifest.version} is revoked: {why}")
    site = _import(name, folder)
    if not isinstance(site, Site) or site.name != name:
        raise ConfigError(f"webcli_tool_{name}.ADAPTER is not a Site named {name!r}.")
    if site.kind != "data" or site.llm_cli is not None:
        raise ConfigError("a community tool is a webcli data site (kind \"data\", no llm_cli).")
    if site.fetch.split(".")[0] != f"webcli_tool_{name}":
        raise ConfigError(f"its fetch module {site.fetch!r} is outside webcli_tool_{name}.")
    problems = manifest_schema.mismatches(manifest, site)
    if problems:
        raise ConfigError("its ADAPTER doesn't match its manifest: " + "; ".join(problems))
    return dataclasses.replace(site, cli=guarded_cli(site))


def guarded_cli(site: Site) -> Callable[[], click.Command]:
    """site.cli, made safe to call while building the CLI: a tool whose commands fail to load becomes one command
    that says so (exit 7) instead of breaking every webcli command, and its write commands are gated."""

    def cli() -> click.Command:
        try:
            group = site.cli()
            if not isinstance(group, click.Command):
                raise ConfigError(f"its cli returned {type(group).__name__}, not a click command.")
            if isinstance(group, click.Group):
                for verb in site.writes:
                    _gate(group, verb, site.name)
                _declare_effects(group, site)
            else:
                output.set_effects(group, None)
            return group
        except Exception as exc:
            warn(f"webcli: the {site.name} tool's commands didn't load: {exc}")
            return _broken(site.name, exc)

    return cli


def _declare_effects(group: click.Group, site: Site) -> None:
    """Each command's effects, from the manifest (site's verbs, writes, and sends_to_people match it by now), over
    whatever the tool's code set: a write is a dry run unless --confirm (_gate), a read never writes, core's
    agent-help reads. Any other command the manifest doesn't name is UNDECLARED, the worst case."""
    for path, _aliases, command in output.leaves(group, hidden=True):
        verb = " ".join(path)
        if verb in site.writes and getattr(command, "_webcli_gated", False):
            declared = output.Effects("with_flag", via=("confirm",), sends=verb in site.sends_to_people)
        elif verb in site.verbs and verb not in site.writes:
            declared = output.Effects()
        elif verb == "agent-help" and output.command_effects(command)["writes"] == "never":
            declared = output.Effects()
        else:
            declared = None
        output.set_effects(command, declared)


def _broken(name: str, exc: Exception) -> click.Command:
    def run(args: tuple[str, ...]) -> None:
        raise ConfigError(f"The {name} tool's commands didn't load: {exc}",
                          hint=f"Fix or remove ~/.webcli/tools/{name}, or `webcli tools disable {name}`.")

    return click.Command(name, callback=run, help=f"(broken: {exc})", context_settings={"ignore_unknown_options": True},
                         params=[click.Argument(["args"], nargs=-1, type=click.UNPROCESSED)])


def _gate(group: click.Group, verb: str, tool: str) -> None:
    """Make `verb` ("label add") a dry run unless --confirm: without it core prints the dry run and the tool's own
    code for that command never runs. Best effort: it covers the command the manifest names, not code the tool runs
    elsewhere."""
    *parents, leaf = verb.split()
    parent: click.Command | None = group
    for word in parents:
        parent = parent.commands.get(word) if isinstance(parent, click.Group) else None
    command = parent.commands.get(leaf) if isinstance(parent, click.Group) else None
    if command is None or isinstance(command, click.Group) or getattr(command, "_webcli_gated", False):
        return
    own = {param.name for param in command.params}
    if "confirm" not in own:
        command.params.append(click.Option(["--confirm", "--yes", "confirm"], is_flag=True,
                                           help="Do the write (default: dry run)."))
    if "dry_run" not in own:
        command.params.append(click.Option(["--dry-run", "dry_run"], is_flag=True,
                                           help="Show what would be written and write nothing (the default)."))
    callback = command.callback

    @functools.wraps(callback)
    def gated(*args, **kwargs):
        confirm = kwargs["confirm"] if "confirm" in own else kwargs.pop("confirm", False)
        dry_run = kwargs["dry_run"] if "dry_run" in own else kwargs.pop("dry_run", False)
        if not confirm or dry_run:
            emit_json({"dryRun": True, "tool": tool, "command": verb,
                       "hint": "Nothing was written. Re-run with --confirm to write."})
            raise SystemExit(EXIT_OK)
        return callback(*args, **kwargs)

    command.callback = gated
    command._webcli_gated = True  # type: ignore[attr-defined]
