"""keep.google.com (webcli keep): google-keep-cli ported onto the shared core. Identity provider "google" (the
google:default profile gemini's login signs in). Read-only."""

from __future__ import annotations

from webcli.sites.base import Site, Strategy


def _cli():
    from webcli.sites.keep.cli import group

    return group


ADAPTER = Site(
    name="keep",
    kind="data",
    provider="google",
    summary="Google Keep notes, read-only: list, get, info, search, status (google-keep-cli parity)",
    cli=_cli,
    fetch="webcli.sites.keep.fetch",
    ops=("status", "notes"),
    verbs={"list": Strategy.INTERCEPT, "get": Strategy.INTERCEPT, "info": Strategy.INTERCEPT,
           "search": Strategy.INTERCEPT, "status": Strategy.COOKIE},
    status_op="status",
)
