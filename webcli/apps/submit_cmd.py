"""`webcli submit`: open a pull request adding your tool to the webcli-tools library. A placeholder until Phase D of
webcli 1.0 builds it. webcli.py adds every command in COMMANDS."""

from __future__ import annotations

from webcli.apps.stubs import not_built

COMMANDS = [not_built("submit", "D", "Open a pull request adding a tool to the webcli-tools library",
                      "NAME [--confirm]")]
