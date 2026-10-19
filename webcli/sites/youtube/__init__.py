"""youtube.com (webcli youtube): ytcli ported onto the shared core. Identity provider "google" (the google:default
profile gemini's login signs in). Read-only watch history."""

from __future__ import annotations

from webcli.sites.base import Site, Strategy


def _cli():
    from webcli.sites.youtube.cli import group

    return group


ADAPTER = Site(
    name="youtube",
    kind="data",
    provider="google",
    summary="YouTube watch history, read-only: history (My Activity's exact times, else YouTube's day precision), "
            "status (ytcli parity)",
    cli=_cli,
    fetch="webcli.sites.youtube.fetch",
    ops=("status", "history"),
    verbs={"history": Strategy.COOKIE, "status": Strategy.COOKIE},
    status_op="status",
)
