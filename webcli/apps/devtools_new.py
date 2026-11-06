"""`webcli new`: make a tool for a site you use. A placeholder until Phase B of webcli 1.0 builds it.
webcli.py adds every command in COMMANDS."""

from __future__ import annotations

from webcli.apps.stubs import not_built

COMMANDS = [not_built("new", "B", "Make a new tool for a site you use",
                      "NAME [--url URL] [--identity ID] [--capture]")]
