"""Placeholders for the webcli 1.0 commands still being built (new, dev, test, lint, tools, submit). `--help` shows
what each will do; running one exits 2 and says which phase builds it. Each phase swaps its module's placeholders
for real commands; delete this file once nothing uses it."""

from __future__ import annotations

import click

from webcli.core import output
from webcli.core.output import EXIT_USAGE


def not_built(name: str, phase: str, summary: str, usage: str = "") -> click.Command:
    """A command named `name` that takes any arguments and exits 2: "not implemented in this build (Phase X)".
    `usage` is the planned arguments, shown in --help and agent-help."""

    @click.command(name, help=f"{summary}. Not built yet: Phase {phase} of webcli 1.0 adds it.",
                   short_help=f"{summary} (not built yet).", options_metavar=usage,
                   context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
    @click.pass_context
    def command(ctx: click.Context) -> None:
        output.die(f"`{ctx.command_path}` is not implemented in this build (Phase {phase}).", EXIT_USAGE)

    return command
