"""news.google.com (webcli gnews): Google News "For you", read-only. Identity provider "google" (the google:default
profile gemini's login signs in)."""

from __future__ import annotations

from webcli.sites.base import Site, Strategy


def _cli():
    from webcli.sites.gnews.cli import group

    return group


ADAPTER = Site(
    name="gnews",
    kind="data",
    provider="google",
    summary="Google News \"For you\", read-only: foryou (stories, by section), status",
    cli=_cli,
    fetch="webcli.sites.gnews.fetch",
    ops=("status", "foryou"),
    verbs={"foryou": Strategy.COOKIE, "status": Strategy.COOKIE},
    status_op="status",
)
