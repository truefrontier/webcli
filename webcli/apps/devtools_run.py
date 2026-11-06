"""`webcli dev`, `test`, and `lint`: run, test, and check a tool you're building. Placeholders until Phase B of
webcli 1.0 builds them. webcli.py adds every command in COMMANDS."""

from __future__ import annotations

from webcli.apps.stubs import not_built

COMMANDS = [
    not_built("dev", "B", "Run a tool you're building, as if it were installed", "NAME -- ARGS..."),
    not_built("test", "B", "Run a tool's offline tests", "NAME"),
    not_built("lint", "B", "Run the library's checks on a tool", "NAME"),
]
