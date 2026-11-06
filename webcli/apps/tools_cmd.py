"""`webcli tools`: find, install, and manage tools from the webcli-tools library. Placeholders until Phase C of
webcli 1.0 builds them. webcli.py adds every command in COMMANDS."""

from __future__ import annotations

import click

from webcli.apps.stubs import not_built


@click.group("tools")
def group() -> None:
    """Find, install, and manage tools from the webcli-tools library (not built yet)."""


for _name, _summary, _usage in (
    ("search", "Search the library", "[QUERY]"),
    ("info", "Show what a tool reads, writes, and signs in as", "NAME"),
    ("install", "Install a tool from the library, after showing its manifest", "NAME [--yes]"),
    ("list", "List installed tools", ""),
    ("update", "Update installed tools, asking first if one wants new hosts, writes, or identities",
     "NAME | --all"),
    ("remove", "Uninstall a tool", "NAME"),
    ("grant", "Let a tool use one of your existing identities", "NAME IDENTITY"),
    ("revoke-grant", "Take back an identity grant", "NAME IDENTITY"),
    ("disable", "Turn an installed tool off", "NAME"),
    ("enable", "Turn a disabled tool back on", "NAME"),
):
    group.add_command(not_built(_name, "C", _summary, _usage))

COMMANDS = [group]
