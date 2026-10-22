"""`chromecli`: your real, signed-in Chrome tabs, shared per tab through the chromecli extension (read-only in P1)."""

from __future__ import annotations

from webcli.chromecli.cli import group, main

__all__ = ["group", "main"]

if __name__ == "__main__":
    main()
