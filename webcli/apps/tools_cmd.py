"""`webcli tools`: find, install, and manage community tools from the webcli-tools library. webcli.py adds every
command in COMMANDS.

Install and update download a tool at the commit the library's index.json pins, check its files against the index's
sha256 (core/index_client.py), check its manifest, show what it will reach, sign in as, and write, and ask before
putting it in ~/.webcli/tools/<name>/. Core loads it from there (core/tools_registry.py) and holds it to its manifest
(docs/tool-trust.md). The other commands manage those folders and grants.toml.

The index also lists webcli's official built-in data sites (loom, gmail, ...). They ship inside webcli: search and info
show them, install says there's nothing to install, and the commands that manage installed tools refuse them.

    ~/.webcli/tools/<name>/.installed.json   what install pinned: version, commit, sha256, index, installedAt
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import click

from webcli import __version__, sites
from webcli.core import grants, index_client, manifest_schema, revoked, tools_registry
from webcli.core.errors import ConfigError
from webcli.core.identities import Identity
from webcli.core.index_client import Entry
from webcli.core.manifest_schema import _NAME, Manifest
from webcli.core.output import (
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_USAGE,
    agent_output_options,
    die,
    effects,
    emit_json,
    emit_rows,
    note,
    resolve_format,
    warn,
)
from webcli.sites.base import NO_IDENTITY

RECORD = tools_registry.INSTALLED
UPDATE_WEBCLI = "pip install --pre -U truefrontier-webcli"
WARNING = ("A community tool is Python code that runs as you, next to your signed-in sessions. webcli holds it to the "
           "hosts, identity, and writes above, but that doesn't make a stranger's code safe: read it first.")


def _folder(name: str) -> Path:
    """~/.webcli/tools/<name>, once `name` is known to be a tool name (never a path)."""
    if not _NAME.fullmatch(name):
        die(f"{name!r} is not a tool name.", EXIT_USAGE, hint="2-32 lowercase letters and digits, no hyphens.")
    return tools_registry.tools_dir() / name


def _installed(name: str) -> Path:
    folder = _folder(name)
    if not folder.is_dir() and _ships(name):
        die(f"{name} is built into webcli; it isn't an installed tool.", EXIT_USAGE,
            hint=f"It comes with webcli and updates with it: {UPDATE_WEBCLI}.")
    if not folder.is_dir():
        die(f"No installed tool named {name!r}.", EXIT_NOT_FOUND, hint="`webcli tools list` shows what's installed.")
    return folder


def _installed_names() -> list[str]:
    root = tools_registry.tools_dir()
    return [p.name for p in sorted(root.iterdir()) if p.is_dir() and not p.name.startswith(".")] \
        if root.is_dir() else []


def _record(folder: Path) -> dict:
    return tools_registry.installed_record(folder)


def _manifest(folder: Path) -> Manifest | None:
    try:
        return manifest_schema.load(folder)
    except ConfigError:
        return None


def _entry(index: index_client.Index, name: str) -> Entry:
    entry = index.tools.get(name)
    if entry is None:
        die(f"The webcli-tools library has no tool named {name!r}.", EXIT_NOT_FOUND,
            hint="`webcli tools search` lists them.")
    if name in manifest_schema._reserved():
        die(f"{name!r} is a webcli built-in or command; a tool can't replace it.", EXIT_USAGE)
    why = revoked.reason(name, entry.version)
    if why:
        die(f"{name} {entry.version} is revoked: {why}", EXIT_USAGE, hint="It won't be installed or run.")
    return entry


def _ships(name: str) -> bool:
    """This webcli has `name` built in."""
    return sites.tier_of(name) == sites.OFFICIAL


def _builtin_view(entry: Entry) -> tuple[dict, str]:
    """What `tools info` shows for an official built-in: the library's catalog entry. Nothing is downloaded."""
    identity = None if entry.identity == NO_IDENTITY else f"{entry.identity}:default"
    data = {"name": entry.name, "version": entry.version, "summary": entry.summary, "tier": sites.OFFICIAL,
            "builtin": True, "verified": entry.verified, "identity": identity, "hosts": list(entry.hosts),
            "commands": [{"name": n, "strategy": s, "writes": w} for n, s, w in entry.commands],
            "requiresWebcli": entry.requires_webcli or None, "docs": entry.docs or None,
            "installed": "builtin" if _ships(entry.name) else None}
    lines = [f"{entry.name} {entry.version}: {entry.summary}",
             "  Built into webcli (official): nothing to install." if _ships(entry.name) else
             f"  Built into webcli {entry.requires_webcli or 'releases other than this one'}; this is webcli "
             f"{__version__}. Update webcli to get it: {UPDATE_WEBCLI}",
             f"  Signs in as: {identity or 'nothing (it never signs in)'}"]
    if entry.docs:
        lines.append(f"  Docs:        {entry.docs}")
    lines.append("  Commands:")
    lines += [f"    {n:<20} {s}{', WRITES' if w else ''}" for n, s, w in entry.commands]
    return data, "\n".join(lines)


def _check_requires(manifest: Manifest) -> None:
    """Exit 2 unless this webcli is in the tool's requires_webcli range."""
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import Version

    if not manifest.requires_webcli:
        return
    try:
        ok = SpecifierSet(manifest.requires_webcli).contains(Version(__version__), prereleases=True)
    except InvalidSpecifier:
        ok = False
    if not ok:
        die(f"{manifest.name} {manifest.version} needs webcli {manifest.requires_webcli}; this is webcli "
            f"{__version__}.", EXIT_USAGE,
            hint="Update webcli (pip install --pre -U truefrontier-webcli), or wait for the tool to catch up.")


@contextmanager
def _staged(entry: Entry) -> Iterator[tuple[Path, Manifest]]:
    """The tool downloaded, hash-checked, and its manifest checked, in a dot folder core's loader skips. The folder
    is removed on the way out unless the caller moved it into place."""
    stage = Path(tempfile.mkdtemp(prefix=f".stage-{entry.name}-", dir=tools_registry.ensure_tools_dir()))
    try:
        index_client.download(entry, stage)
        manifest = manifest_schema.load(stage)
        if (manifest.name, manifest.version) != (entry.name, entry.version):
            raise index_client.LibraryError(
                f"The library's index says {entry.name} {entry.version}, but its manifest says {manifest.name} "
                f"{manifest.version}. Not installing it.")
        yield stage, manifest
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _identity_line(manifest: Manifest) -> str:
    if manifest.identity == NO_IDENTITY:
        return "nothing (it never signs in)"
    if manifest.identity == manifest.name:
        return f"{manifest.default_identity}, its own (a new sign-in, none of your existing ones)"
    return (f"{manifest.default_identity}, which is NOT its own: it can't use your {manifest.identity} sign-in until "
            f"you run `webcli tools grant {manifest.name} {manifest.default_identity}`")


def _describe(manifest: Manifest, entry: Entry | None, record: dict | None = None) -> dict:
    """What a person agrees to: the manifest, and where the code comes from."""
    record = record or {}
    return {
        "name": manifest.name, "version": manifest.version, "summary": manifest.summary,
        "identity": manifest.default_identity, "identityIsOwn": manifest.identity in (manifest.name, NO_IDENTITY),
        "hosts": list(manifest.hosts),
        "commands": [{"name": c.name, "strategy": c.strategy.value, "writes": c.writes,
                      "sendsToPeople": c.sends_to_people} for c in manifest.commands],
        "maintainers": list(manifest.maintainers), "license": manifest.license,
        "requiresWebcli": manifest.requires_webcli or None,
        "commit": entry.commit if entry else record.get("commit"),
        "sha256": entry.sha256 if entry else record.get("sha256"),
        "verified": entry.verified if entry else False,
        "source": index_client.source(entry) if entry else None,
    }


def _text(manifest: Manifest, entry: Entry | None) -> str:
    lines = [f"{manifest.name} {manifest.version}: {manifest.summary}"]
    if entry:
        lines.append(f"  Code:        {index_client.source(entry)} (sha256 checked)")
    lines += [f"  Maintainers: {', '.join(manifest.maintainers)}; license {manifest.license}; "
              + ("verified by the library" if entry and entry.verified else "community, not verified"),
              f"  Signs in as: {_identity_line(manifest)}",
              f"  Reaches:     {', '.join(manifest.hosts)}",
              "  Commands:"]
    for c in manifest.commands:
        what = "read only" if not c.writes else \
            ("WRITES and SENDS TO PEOPLE" if c.sends_to_people else "WRITES") + " (a dry run unless --confirm)"
        lines.append(f"    {c.name:<20} {what}")
    return "\n".join(lines + [WARNING])


def _ask(question: str) -> bool:
    try:
        return click.confirm(question, default=False, err=True)
    except click.Abort:  # no answer (stdin closed): no
        return False


def _write_record(stage: Path, entry: Entry, index: index_client.Index) -> None:
    (stage / RECORD).write_text(json.dumps({
        "name": entry.name, "version": entry.version, "commit": entry.commit, "sha256": entry.sha256,
        "index": index.url, "installedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, indent=1) + "\n")


def _state(name: str) -> dict:
    """Whether core loads the tool now, and why not."""
    if not tools_registry.enabled():
        return {"loaded": False, "reason": f"${tools_registry.OFF_ENV} is set: no community tool loads"}
    if sites.tier_of(name) == sites.COMMUNITY:
        return {"loaded": True}
    reasons = [s["reason"] for s in sites.skipped() if s["name"] == name]
    return {"loaded": False, "reason": reasons[0] if reasons else "not loaded"}


def _next_steps(manifest: Manifest) -> list[str]:
    steps = [f"webcli {manifest.name} --help"]
    if manifest.identity == manifest.name:
        steps.insert(0, f"webcli identity login {manifest.default_identity}")
    elif manifest.identity != NO_IDENTITY:
        steps.insert(0, f"webcli tools grant {manifest.name} {manifest.default_identity}   (only if you trust it "
                        f"with your {manifest.identity} sign-in)")
    return steps


def _changes(old: Manifest | None, new: Manifest) -> list[str]:
    """What an update asks for that the installed version didn't: hosts, writing or sending commands, identity."""
    if old is None:
        return ["the installed manifest can't be read, so nothing can be compared"]
    out = []
    hosts = sorted(set(new.hosts) - set(old.hosts))
    if hosts:
        out.append(f"new hosts: {', '.join(hosts)}")
    for attr, what in (("writes", "commands that write"), ("sends_to_people", "commands that send to people")):
        added = sorted(getattr(new, attr) - getattr(old, attr))
        if added:
            out.append(f"new {what}: {', '.join(added)}")
    if new.identity != old.identity:
        out.append(f"signs in as {new.default_identity or 'nothing'} instead of {old.default_identity or 'nothing'}")
    return out


@click.group("tools")
def group() -> None:
    """Find, install, and manage community tools from the webcli-tools library. A tool installs at the commit the
    library pins, after its files match the library's sha256 and you've seen its hosts, identity, and writes."""


@effects("never")
@group.command("search")
@click.argument("query", required=False, default="")
@agent_output_options()
def search_cmd(query: str, fmt, compact, select, as_csv, quiet) -> None:
    """Search the library by name and summary (no QUERY: list every tool). webcli's official built-ins are listed
    too, marked installed "builtin"."""
    index = index_client.fetch()
    installed = set(_installed_names())
    rows = []
    for entry in index.tools.values():
        if query.lower() in f"{entry.name} {entry.summary}".lower():
            record = _record(tools_registry.tools_dir() / entry.name) if entry.name in installed else {}
            if entry.builtin:
                state = "builtin" if _ships(entry.name) else ""
            else:
                state = (record.get("version") or "yes") if entry.name in installed else ""
            rows.append({"name": entry.name, "version": entry.version, "summary": entry.summary,
                         "tier": sites.OFFICIAL if entry.builtin else sites.COMMUNITY, "verified": entry.verified,
                         "installed": state, "revoked": revoked.reason(entry.name, entry.version) or ""})
    emit_rows(rows, title="webcli-tools library",
              fields=["name", "version", "summary", "tier", "verified", "installed", "revoked"],
              compact_fields=("name", "version"), columns=["name", "version", "installed", "summary"],
              noun="tools", fmt=fmt, compact=compact, select=select, quiet=quiet, as_csv=as_csv)
    raise SystemExit(EXIT_OK)


@effects("never")
@group.command("info")
@click.argument("name")
@agent_output_options(formats=("text", "json"), rows=False)
def info_cmd(name: str, fmt, quiet) -> None:
    """Show what a tool reaches, signs in as, and writes: the library's current version (downloaded and checked,
    never run), or the installed one if the library no longer lists it."""
    folder = _folder(name)
    entry = index_client.fetch().tools.get(name)
    if entry and entry.builtin:
        data, text = _builtin_view(entry)
    elif entry:
        with _staged(entry) as (_stage, manifest):
            data, text = _describe(manifest, entry), _text(manifest, entry)
    elif folder.is_dir() and (manifest := _manifest(folder)):
        data, text = _describe(manifest, None, _record(folder)), _text(manifest, None)
        text += "\n(Installed; the library no longer lists it.)"
    else:
        die(f"No tool named {name!r} in the library or installed.", EXIT_NOT_FOUND,
            hint="`webcli tools search` lists the library.")
    if "installed" not in data:
        data["installed"] = (_record(folder).get("version") or "yes") if folder.is_dir() else None
    data["revoked"] = revoked.reason(name, data["version"])
    if resolve_format(fmt, human="text") == "json":
        emit_json(data)
    else:
        click.echo(text + (f"\nRevoked: {data['revoked']}" if data["revoked"] else ""))
    raise SystemExit(EXIT_OK)


@effects("always", human=True, local_setup=True)
@group.command("install")
@click.argument("name")
@click.option("--yes", "-y", "yes", is_flag=True, help="Install without asking (the manifest is still shown).")
def install_cmd(name: str, yes: bool) -> None:
    """Install a tool from the library: shows its manifest (hosts, identity, writes) and asks first. An official
    built-in is already part of webcli: nothing to install, exit 0."""
    folder = _folder(name)
    if folder.exists():
        die(f"{name} is already installed.", EXIT_USAGE,
            hint=f"`webcli tools update {name}` gets the library's version; `webcli tools remove {name}` uninstalls.")
    index = index_client.fetch()
    listed = index.tools.get(name)
    if listed is not None and listed.builtin:
        if not _ships(name):
            die(f"{name} is built into webcli {listed.requires_webcli or 'releases other than this one'}; this is "
                f"webcli {__version__}.", EXIT_USAGE, hint=f"Update webcli to get it: {UPDATE_WEBCLI}.")
        emit_json({"tool": name, "installed": "builtin", "changed": False, "version": __version__,
                   "note": f"{name} is built into webcli {__version__}; there's nothing to install.",
                   "next": [f"webcli {name} --help"]})
        raise SystemExit(EXIT_OK)
    entry = _entry(index, name)
    # Grants are kept by name. Nothing by this name is installed, so any left over (a dev tool's, or a remove that
    # couldn't edit grants.toml) were for other code: this tool starts with none, as the manifest text says.
    stale = grants.list_grants().get(name, [])
    with _staged(entry) as (stage, manifest):
        _check_requires(manifest)
        click.echo(_text(manifest, entry), err=True)
        if not yes and not _ask(f"Install {name} {manifest.version}?"):
            die(f"{name} was not installed.", EXIT_USAGE, hint="Pass --yes to install without asking.")
        _write_record(stage, entry, index)
        if stale:  # before the folder goes in place: if grants.toml can't be written, nothing is installed
            try:
                grants.revoke_grant(name)
            except OSError as exc:
                raise ConfigError(f"Can't drop the grants left under the name {name} ({exc}); not installing it.",
                                  hint=f"Check {grants.path()}.") from None
        stage.rename(folder)  # fails, rather than replacing, if a non-empty folder appeared meanwhile
    if stale:
        warn(f"webcli: dropped the grants left under the name {name} ({', '.join(stale)}): they were for an earlier "
             f"tool. Grant again only if you trust this one.")
    sites.reload()
    emit_json({"installed": name, "version": manifest.version, "commit": entry.commit, "path": str(folder),
               **_state(name), "manifest": _describe(manifest, entry), "next": _next_steps(manifest),
               **({"grantsDropped": stale} if stale else {})})
    raise SystemExit(EXIT_OK)


def _update(index: index_client.Index, name: str, yes: bool) -> dict:
    folder = tools_registry.tools_dir() / name
    old, record = _manifest(folder), _record(folder)
    entry = index.tools.get(name)
    if entry is None:
        return {"name": name, "status": "not in library"}
    # A leftover folder under a built-in's name, or a name webcli keeps: core never loads it.
    if entry.builtin or name in manifest_schema._reserved():
        return {"name": name, "status": "built in"}
    why = revoked.reason(name, entry.version)
    if why:
        return {"name": name, "status": "revoked", "reason": why}
    if (record.get("commit"), record.get("sha256")) == (entry.commit, entry.sha256):
        return {"name": name, "status": "up to date", "version": entry.version}
    with _staged(entry) as (stage, new):
        _check_requires(new)
        changes = _changes(old, new)
        if changes:
            click.echo(_text(new, entry) + "\nThis update asks for more than the installed version:\n"
                       + "\n".join(f"  - {c}" for c in changes), err=True)
            if not yes and not _ask(f"Update {name} to {new.version}?"):
                return {"name": name, "status": "declined", "changes": changes}
        _write_record(stage, entry, index)
        if tools_registry.is_disabled(folder):
            (stage / tools_registry.DISABLED).touch()
        # ponytail: no lock against a second `webcli tools` changing the same tool at once; add one if that happens.
        aside = folder.with_name(f".old-{name}-{stage.name.rsplit('-', 1)[-1]}")
        folder.rename(aside)
        try:
            stage.rename(folder)
        except BaseException:  # Ctrl-C too: never leave the tool only under a dot name
            aside.rename(folder)
            raise
        shutil.rmtree(aside, ignore_errors=True)
    out = {"name": name, "status": "updated", "from": old.version if old else record.get("version"),
           "to": new.version, "commit": entry.commit, "changes": changes}
    diff = index_client.compare(record.get("commit"), entry)
    if diff:
        out["diff"] = diff
    return out


@effects("always", human=True, local_setup=True)
@group.command("update")
@click.argument("name", required=False)
@click.option("--all", "all_", is_flag=True, help="Update every installed tool; reports each one's status, exit 0.")
@click.option("--yes", "-y", "yes", is_flag=True,
              help="Don't ask, even when an update wants new hosts, writes, or identity.")
def update_cmd(name: str | None, all_: bool, yes: bool) -> None:
    """Update installed tools to the library's pinned version. Asks first when one wants new hosts, writing
    commands, or another identity; otherwise updates without asking."""
    if bool(name) == all_:
        die("Name one tool, or pass --all.", EXIT_USAGE)
    names = _installed_names() if all_ else [_installed(name).name]
    index = index_client.fetch(cached_ok=False)
    results = [_update(index, n, yes) for n in names]
    sites.reload()
    status = None if all_ else results[0]["status"]
    if not all_ and status == "declined":
        die(f"{name} was not updated.", EXIT_USAGE, hint="Pass --yes to update without asking.")
    if not all_ and status == "revoked":
        die(f"{name} is revoked: {results[0]['reason']}", EXIT_USAGE, hint=f"`webcli tools remove {name}`.")
    if not all_ and status == "built in":
        die(f"{name} is built into webcli; there's no tool to update.", EXIT_USAGE,
            hint=f"`webcli tools remove {name}` deletes the leftover folder; webcli updates with {UPDATE_WEBCLI}.")
    if not all_ and status == "not in library":
        die(f"The webcli-tools library no longer lists {name}; nothing to update.", EXIT_NOT_FOUND)
    emit_json(results if all_ else results[0])
    raise SystemExit(EXIT_OK)


@effects("never")
@group.command("list")
@agent_output_options()
def list_cmd(fmt, compact, select, as_csv, quiet) -> None:
    """Installed tools: version, pinned commit, grants, and whether core loads each one. Then webcli's official
    built-in data sites, status "builtin"."""
    granted = grants.list_grants()
    rows = []
    for name in _installed_names():
        folder = tools_registry.tools_dir() / name
        record, manifest = _record(folder), _manifest(folder)
        state = {"loaded": False, "reason": "disabled"} if tools_registry.is_disabled(folder) else _state(name)
        rows.append({"name": name, "version": manifest.version if manifest else record.get("version"),
                     "status": "loaded" if state["loaded"] else state["reason"],
                     "commit": (record.get("commit") or "")[:12], "grants": ", ".join(granted.get(name, [])),
                     "summary": manifest.summary if manifest else ""})
    rows += [{"name": name, "version": __version__, "status": "builtin", "commit": "", "grants": "",
              "summary": site.summary} for name, site in sites.registry().items()
             if _ships(name) and site.kind == "data"]
    emit_rows(rows, title="Installed tools and built-ins",
              fields=["name", "version", "status", "commit", "grants", "summary"],
              compact_fields=("name", "version", "status"), columns=["name", "version", "status", "grants"],
              noun="tools", fmt=fmt, compact=compact, select=select, quiet=quiet, as_csv=as_csv)
    raise SystemExit(EXIT_OK)


@effects("always", human=True, local_setup=True)
@group.command("remove")
@click.argument("name")
def remove_cmd(name: str) -> None:
    """Uninstall a tool and take back its identity grants. Its own sign-in (<name>:default), if any, stays."""
    folder = _installed(name)
    shutil.rmtree(folder)
    sites.reload()
    try:
        dropped = grants.list_grants().get(name, [])
        grants.revoke_grant(name)
    except ConfigError as exc:
        dropped = []
        warn(f"webcli: {name} is removed, but its grants weren't: {exc}")
    emit_json({"removed": name, "grantsRevoked": dropped})
    raise SystemExit(EXIT_OK)


@effects("always", human=True, local_setup=True)
@group.command("grant")
@click.argument("name")
@click.argument("identity")
def grant_cmd(name: str, identity: str) -> None:
    """Let a tool use one of your existing identities (google:default). A tool may always use its own
    (<name>:<account>); anything else needs this."""
    folder = _folder(name)
    if not folder.is_dir() and sites.tier_of(name) != sites.COMMUNITY:
        die(f"No community tool named {name!r}.", EXIT_NOT_FOUND, hint="`webcli tools list` shows what's installed.")
    ident = Identity(identity.strip())
    if ident.provider == name:
        emit_json({"tool": name, "identity": ident.id, "granted": True, "changed": False,
                   "note": "A tool may always use its own identities; no grant needed."})
        raise SystemExit(EXIT_OK)
    changed = grants.grant(name, ident.id)
    manifest = _manifest(folder) if folder.is_dir() else None
    hosts = ", ".join(manifest.hosts) if manifest else "its manifest's hosts"
    note(f"{name} can now use {ident.id}, your signed-in {ident.provider} session. Its pages still reach only {hosts}. "
         f"Take it back with `webcli tools revoke-grant {name} {ident.id}`.", quiet=False)
    emit_json({"tool": name, "identity": ident.id, "granted": True, "changed": changed})
    raise SystemExit(EXIT_OK)


@effects("always", human=True, local_setup=True)
@group.command("revoke-grant")
@click.argument("name")
@click.argument("identity")
def revoke_grant_cmd(name: str, identity: str) -> None:
    """Take back a tool's grant of one of your identities."""
    _folder(name)
    ident = Identity(identity.strip())
    emit_json({"tool": name, "identity": ident.id, "granted": False, "changed": grants.revoke_grant(name, ident.id)})
    raise SystemExit(EXIT_OK)


def _toggle(name: str, on: bool) -> None:
    marker = _installed(name) / tools_registry.DISABLED
    changed = marker.exists() == on
    if on:
        marker.unlink(missing_ok=True)
    else:
        marker.touch()
    sites.reload()
    emit_json({"tool": name, "enabled": on, "changed": changed, **(_state(name) if on else {})})
    raise SystemExit(EXIT_OK)


@effects("always", human=True, local_setup=True)
@group.command("disable")
@click.argument("name")
def disable_cmd(name: str) -> None:
    """Turn an installed tool off: core won't import its code until you enable it."""
    _toggle(name, False)


@effects("always", human=True, local_setup=True)
@group.command("enable")
@click.argument("name")
def enable_cmd(name: str) -> None:
    """Turn a disabled tool back on."""
    _toggle(name, True)


COMMANDS = [group]
