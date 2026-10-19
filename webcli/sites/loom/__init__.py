"""www.loom.com (webcli loom): loom-cli ported onto the shared core. Identity provider "loom" (loom:default, its own
sign-in: a person runs `webcli identity login loom:default`). Read-only: your library, one video's details, and the
official transcript Loom already made."""

from __future__ import annotations

from webcli.sites.base import Site, Strategy


def _cli():
    from webcli.sites.loom.cli import group

    return group


async def _login(timeout: int, on_state=lambda _state: None) -> dict:
    from webcli.sites.loom import fetch

    return await fetch.login(timeout, on_state)


ADAPTER = Site(
    name="loom",
    kind="data",
    provider="loom",
    summary="Loom, read-only: list, info, transcript (official transcripts), status (loom-cli parity)",
    cli=_cli,
    fetch="webcli.sites.loom.fetch",
    ops=("status", "videos", "video", "transcript"),
    verbs={"list": Strategy.COOKIE, "info": Strategy.COOKIE, "transcript": Strategy.INTERCEPT,
           "status": Strategy.COOKIE},
    login=_login,
    status_op="status",
)
