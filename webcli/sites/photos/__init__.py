"""photos.google.com (webcli photos): google-photos-cli ported onto the shared core. Identity provider "google" (the
google:default profile gemini's login signs in). Read-only; `download` saves the original file."""

from __future__ import annotations

from webcli.sites.base import Site, Strategy


def _cli():
    from webcli.sites.photos.cli import group

    return group


ADAPTER = Site(
    name="photos",
    kind="data",
    provider="google",
    summary="Google Photos, read-only: list, search, info, albums, download (the original file), status "
            "(google-photos-cli parity)",
    cli=_cli,
    fetch="webcli.sites.photos.fetch",
    ops=("status", "albums", "photos", "info", "download"),
    verbs={"list": Strategy.UI, "search": Strategy.UI, "info": Strategy.UI, "albums": Strategy.UI,
           "download": Strategy.COOKIE, "status": Strategy.COOKIE},
    status_op="status",
)
