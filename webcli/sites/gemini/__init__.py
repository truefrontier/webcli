"""gemini.google.com (llmcli gemini): gweb, ported onto the shared core. Identity provider "google"."""

from __future__ import annotations

from webcli.sites.base import Site, Strategy


def _cli():
    from webcli.sites.gemini.cli import group

    return group


async def _login(timeout: int, on_state=lambda _state: None) -> dict:
    from webcli.sites.gemini import fetch

    return await fetch.login(timeout, on_state)


ADAPTER = Site(
    name="gemini",
    kind="llm",
    provider="google",
    summary="gemini.google.com: ask / -p (models, thinking, files, images, canvas, citations), chats, models, status "
            "(gweb parity)",
    cli=_cli,
    fetch="webcli.sites.gemini.fetch",
    ops=("status", "models", "chats", "chat", "ask"),
    verbs={"ask": Strategy.UI, "chats": Strategy.COOKIE, "models": Strategy.UI, "status": Strategy.COOKIE},
    login=_login,
)
