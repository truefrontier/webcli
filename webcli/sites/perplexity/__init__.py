"""www.perplexity.ai Discover (webcli perplexity): read-only, signed in first, else the public feed. Identity provider
"perplexity" (perplexity:default; a person signs it in with `webcli perplexity login`)."""

from __future__ import annotations

from webcli.sites.base import Site, Strategy


async def _login(timeout: int, on_state=lambda _state: None) -> dict:
    from webcli.sites.perplexity import fetch

    return await fetch.login(timeout, on_state)


def _cli():
    from webcli.sites.perplexity.cli import group

    return group


ADAPTER = Site(
    name="perplexity",
    kind="data",
    provider="perplexity",
    summary="Perplexity Discover, read-only, signed in first (else public): discover (cards by topic), status, login",
    cli=_cli,
    fetch="webcli.sites.perplexity.fetch",
    ops=("status", "discover"),
    verbs={"discover": Strategy.COOKIE, "status": Strategy.COOKIE},
    login=_login,
    status_op="status",
)
