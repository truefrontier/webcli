"""Artificial Analysis (webcli aa): aanalysis ported onto the shared core. The Free Data API over plain HTTPS with
an API key: no browser, no identity, no daemon ops."""

from __future__ import annotations

from webcli.sites.base import NO_IDENTITY, Site, Strategy


def _cli():
    from webcli.sites.aa.cli import group

    return group


ADAPTER = Site(
    name="aa",
    kind="data",
    provider=NO_IDENTITY,
    summary="Artificial Analysis LLM and TTS leaderboards: models, digest, tts, config (aanalysis parity)",
    cli=_cli,
    fetch="webcli.sites.aa.api",
    ops=(),
    verbs={"models": Strategy.PUBLIC, "digest": Strategy.PUBLIC, "tts": Strategy.PUBLIC},
    status_op=None,
)
