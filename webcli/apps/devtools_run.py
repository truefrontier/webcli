"""`webcli dev`, `test`, and `lint`: run, test, and check a tool you're writing (one `webcli new` made, listed in
~/.webcli/tools/dev.toml), or any tool folder by path. webcli.py adds every command in COMMANDS."""

from __future__ import annotations

import importlib
import os

import click

from webcli import scaffold, sites
from webcli.core import daemon, output
from webcli.core.output import (
    EXIT_CONFIG,
    EXIT_NOT_FOUND,
    EXIT_OK,
    agent_output_options,
    die,
    effects,
    emit_json,
    note,
)
from webcli.scaffold import checks
from webcli.sites.base import Site


def dev_site(name: str) -> Site:
    """The tool as core loaded it from dev.toml (tier community: grants, host envelope, write gating), loaded
    fresh so it's the code on disk now. A tool core skipped doesn't run: this says why."""
    sites.reload()
    registry = sites.registry()
    reason = next((s["reason"] for s in sites.skipped() if s["name"] == name), None)
    if reason or sites.tier_of(name) != sites.COMMUNITY:
        die(f"webcli didn't load the {name} tool: {reason or 'it is not a community tool'}", EXIT_CONFIG)
    return registry[name]


@effects("always", sends=True, human=True, window=True, local_setup=True)
@click.command("dev", options_metavar="", short_help="Run a tool you're writing, as if it were installed.",
               context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False})
@click.argument("name")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def dev_cmd(name, args) -> None:
    """Run the tool NAME with ARGS, exactly as `webcli NAME ARGS` would once it's installed: `webcli dev NAME -- page
    --format json`. Its Chrome runs in this command, not the identity's daemon, so every edit shows up on the next
    run. NAME is a tool in ~/.webcli/tools/dev.toml (`webcli new` lists it there)."""
    name, _ = scaffold.find(name)
    site = dev_site(name)
    os.environ[daemon.DAEMON_ENV] = "0"  # a daemon keeps the code it started with; this run must see your edits
    try:
        group = site.cli()
        importlib.import_module(site.fetch)  # its ops' module: a mistake there is a broken tool, not a site error
    except Exception as exc:
        die(f"{name}'s commands failed to load: {type(exc).__name__}: {exc}", EXIT_CONFIG)
    group.main(list(args), prog_name=f"webcli {name}", standalone_mode=True)


def _emit(record: dict, lines: list[str], fmt: str | None) -> None:
    if output.resolve_format(fmt) == "json":
        emit_json(record)
    else:
        click.echo("\n".join(lines))


@effects("always", sends=True, human=True, window=True, local_setup=True)
@click.command("test", options_metavar="[--format F]", short_help="Run a tool's offline tests.")
@click.argument("name")
@agent_output_options(rows=False)
def test_cmd(name, fmt, quiet) -> None:
    """Run the tool NAME's tests (each test_* function in tests/test_*.py, no arguments) with the network blocked,
    as the library's CI does. Exit 0 when all pass, 7 when one fails (or the tool doesn't load), 3 when there are
    none. NAME is a tool in dev.toml, or a tool folder's path."""
    name, folder = scaffold.find(name)
    results = checks.run_tests(name, folder)
    failed = [r for r in results if r["result"] != "passed"]
    _emit({"tool": name, "folder": str(folder), "passed": len(results) - len(failed), "failed": len(failed),
           "tests": results},
          [f"{'ok  ' if r['result'] == 'passed' else 'FAIL'} {r['test']}" + (f": {r['error']}" if r["error"] else "")
           for r in results], fmt)
    if not results:
        die(f"{name} has no tests: add test_* functions to {folder / 'tests'}/test_*.py.", EXIT_NOT_FOUND)
    if failed:
        die(f"{len(failed)} of {len(results)} tests failed.", EXIT_CONFIG)
    note(f"All {len(results)} tests passed, offline.", quiet=quiet)
    raise SystemExit(EXIT_OK)


@effects("always", sends=True, human=True, window=True, local_setup=True)
@click.command("lint", options_metavar="[--format F]", short_help="Run the library's checks on a tool.")
@click.argument("name")
@agent_output_options(rows=False)
def lint_cmd(name, fmt, quiet) -> None:
    """Check the tool NAME the way the webcli-tools library's CI will: a valid manifest that matches the code
    (name, identity, hosts, commands, writes), imports from webcli.adapter only, a guard that stays on the
    manifest's hosts and, for a read-only tool, refuses PUT, PATCH, and DELETE, a --confirm on a command only if it
    is marked writes = true, offline tests present, and no secrets (fixtures: no tokens or email addresses
    either). Exit 0 with no errors (warnings are fine), 7 otherwise. NAME is a tool in dev.toml, or a tool folder's
    path."""
    name, folder = scaffold.find(name)
    findings = checks.lint(name, folder)
    errors = [f for f in findings if f["level"] == "error"]
    _emit({"tool": name, "folder": str(folder), "ok": not errors, "findings": findings},
          [f"{f['level']:7} {f['check']}: {f['message']}" for f in findings], fmt)
    if errors:
        die(f"lint: {len(errors)} error(s) in {name}.", EXIT_CONFIG, hint="Fix each error above, then lint again.")
    note(f"lint: {name} passes ({len(findings)} warning(s)).", quiet=quiet)
    raise SystemExit(EXIT_OK)


COMMANDS = [dev_cmd, test_cmd, lint_cmd]
