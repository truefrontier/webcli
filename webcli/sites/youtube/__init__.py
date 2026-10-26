"""youtube.com (webcli youtube): ytcli ported onto the shared core. Identity provider "google" (the google:default
profile gemini's login signs in). Read-only watch history; the one write is `playlist create`, a dry run unless
--confirm."""

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
            "subs, uploads, home (recommendations; no Shorts by default), status (ytcli parity); playlist create (dry run unless --confirm)",
    cli=_cli,
    fetch="webcli.sites.youtube.fetch",
    ops=("status", "history", "create_playlist", "subs", "uploads", "home"),
    verbs={"history": Strategy.COOKIE, "subs": Strategy.COOKIE, "uploads": Strategy.COOKIE, "home": Strategy.COOKIE, "status": Strategy.COOKIE, "playlist create": Strategy.COOKIE},
    status_op="status",
)
