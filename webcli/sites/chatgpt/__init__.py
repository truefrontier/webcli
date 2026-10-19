"""chatgpt.com (llmcli chatgpt): cgpt, ported onto the shared core. Identity provider "chatgpt"."""

from __future__ import annotations

from webcli.sites.base import Site, Strategy


def _cli():
    from webcli.sites.chatgpt.cli import group

    return group


async def _login(timeout: int, on_state=lambda _state: None) -> dict:
    from webcli.sites.chatgpt import fetch

    return await fetch.login(timeout, on_state)


ADAPTER = Site(
    name="chatgpt",
    kind="llm",
    provider="chatgpt",
    summary="chatgpt.com: ask / -p, chats, models, status (cgpt parity)",
    cli=_cli,
    fetch="webcli.sites.chatgpt.fetch",
    ops=("status", "models", "chats", "chat", "ask"),
    verbs={"ask": Strategy.UI, "chats": Strategy.COOKIE, "models": Strategy.COOKIE, "status": Strategy.COOKIE},
    login=_login,
)
