"""mail.google.com (webcli gmail): google-gmail-cli ported onto the shared core, filters first. Identity provider
"google" (the google:default profile gemini's login signs in). `filters create|delete` write only with --confirm."""

from __future__ import annotations

from webcli.sites.base import Site, Strategy


def _cli():
    from webcli.sites.gmail.cli import group

    return group


ADAPTER = Site(
    name="gmail",
    kind="data",
    provider="google",
    summary="Gmail filters: list, create, delete (create/delete dry run unless --confirm), status "
            "(google-gmail-cli parity)",
    cli=_cli,
    fetch="webcli.sites.gmail.fetch",
    ops=("status", "filters", "create_filter", "delete_filter"),
    verbs={"filters list": Strategy.UI, "filters create": Strategy.UI, "filters delete": Strategy.UI,
           "status": Strategy.COOKIE},
    status_op="status",
)
