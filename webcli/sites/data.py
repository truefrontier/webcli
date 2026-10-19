"""What every browser-backed `webcli <site>` group shares: the group itself (--identity, --lock-wait), running the
site's fetch ops in the identity daemon's hidden Chrome with their errors mapped to exit codes, and agent-help.

Data verbs are headless only: no --headed. Sign-in is the provider's (`webcli identity login <identity>`, a person's
job); a data site declares login=None and borrows it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from webcli import __version__
from webcli.core import browser, daemon, output
from webcli.core.identities import Identity, resolve
from webcli.core.output import EXIT_USAGE, die
from webcli.sites.base import Site
from webcli.sites.llm import env_help


def identity() -> Identity:
    return browser.current()


def login_hint() -> str:
    return f"a person runs: webcli identity login {identity().id}"


def _wait_arg(ctx: click.Context, param: click.Parameter, value: float) -> float:
    try:
        return browser.parse_wait(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from None


def site_group(site: Site, help_text: str) -> click.Group:
    """`webcli <site>`: resolves the identity (exit 7 when it can't) and the lock wait before any command runs."""

    @click.group(cls=output.AgentGroup, name=site.name, help=help_text)
    @click.version_option(__version__, prog_name=f"webcli {site.name}")
    @click.option("--identity", "identity_id", metavar="PROVIDER:ACCOUNT", default=None,
                  help=f"Run as this identity (default {site.provider}:default; env WEBCLI_IDENTITY).")
    @click.option("--lock-wait", type=float, default=30.0, envvar=browser.LOCK_WAIT_ENV, show_default=True,
                  callback=_wait_arg,
                  help=f"Seconds to wait when another command holds the identity's Chrome (env "
                       f"{browser.LOCK_WAIT_ENV}).")
    def group(identity_id: str | None, lock_wait: float) -> None:
        output.env_format()  # a bad format env is a usage error before any command does work
        browser.lock_wait = lock_wait
        browser.identity = resolve(site, identity_id)

    return group


def call(site: Site, op: str, *args, **kwargs) -> Any:
    """<site>.fetch.<op> in the identity daemon's hidden Chrome (started on demand), or in a hidden Chrome of this
    command's own when no daemon can run. Errors exit with their code (4 sign-in, 3 not found, 5 site...)."""
    try:
        return browser.run(daemon.call(identity(), site.name, op, *args, headed=False, **kwargs))
    except SystemExit:
        raise
    except Exception as exc:
        output.fail(exc, site=site.name, login=login_hint(), chats=f"webcli {site.name}")


def out_dir(value: str) -> str:
    """An output directory as an absolute path, created now: the daemon (cwd ~/.webcli) writes the files."""
    path = Path(value).expanduser().resolve()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        die(f"Can't create {path}: {exc}", EXIT_USAGE)
    return str(path)


def emit_record(title: str, data: dict, fmt: str | None) -> None:
    if output.resolve_format(fmt) == "json":
        output.emit_json(data)
    else:
        output.kv_table(title, data, output.resolve_format(fmt))


def add_agent_help(group: click.Group, *, notes: list[str], writes: list[str]) -> None:
    group.add_command(output.agent_help_command(group, version=__version__, env=env_help, notes=notes, writes=writes))
