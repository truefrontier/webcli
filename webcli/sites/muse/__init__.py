"""muse.ai (llmcli muse): Meta's Muse agent chat, musecli ported onto the shared core. Identity provider "meta".

Muse signs in with a Meta account (Facebook, Instagram, email, or phone): signed out, muse.ai cycles through
auth.muse.ai, facebook.com, and instagram.com looking for a Meta session, then shows its own login form. So the
identity is meta:default. Chat streams over an end-to-end encrypted WebSocket to the account's cloud VM
(wss://*.metaaivm.com), the one socket host the guard lets through. No model picker, files, images, or canvas.
"""

from __future__ import annotations

from webcli.sites.base import Site, Strategy


def _cli():
    from webcli.sites.muse.cli import group

    return group


async def _login(timeout: int, on_state=lambda _state: None) -> dict:
    from webcli.sites.muse import fetch

    return await fetch.login(timeout, on_state)


ADAPTER = Site(
    name="muse",
    kind="llm",
    provider="meta",
    summary="muse.ai (Meta's Muse agent): ask / -p, chats, models (always empty), status (musecli parity)",
    cli=_cli,
    fetch="webcli.sites.muse.fetch",
    ops=("status", "chats", "chat", "ask"),
    verbs={"ask": Strategy.UI, "chats": Strategy.UI, "status": Strategy.COOKIE},
    login=_login,
)
