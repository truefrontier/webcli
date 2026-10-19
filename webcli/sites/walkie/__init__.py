"""Walkie.app (webcli walkie): walkie-cli ported onto the shared core. Its local SQLite history, read-only: no
browser, no identity, no network, no daemon ops."""

from __future__ import annotations

from webcli.sites.base import NO_IDENTITY, Site


def _cli():
    from webcli.sites.walkie.cli import group

    return group


ADAPTER = Site(
    name="walkie",
    kind="data",
    provider=NO_IDENTITY,
    summary="Walkie.app meetings, transcripts, and dictations from its local SQLite: meetings, dictations, status "
            "(walkie-cli parity)",
    cli=_cli,
    fetch="webcli.sites.walkie.db",
    ops=(),
    status_op=None,
)
