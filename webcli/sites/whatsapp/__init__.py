"""web.whatsapp.com (webcli whatsapp): your WhatsApp chats, read from WhatsApp Web's rendered page in your own linked,
hidden Chrome. Identity provider "whatsapp" (whatsapp:default, its own link): WhatsApp Web links to the phone by QR
code, not an account login. A person runs `webcli whatsapp login` (same as `webcli identity login whatsapp:default`)
once and scans the code from the phone; the link lives in that profile.

Read-only except `send`: a dry run by default; with --confirm and --expect-chat matching the open chat's header
exactly, one text to one existing chat."""

from __future__ import annotations

from webcli.sites.base import Site, Strategy


def _cli():
    from webcli.sites.whatsapp.cli import group

    return group


async def _login(timeout: int, on_state=lambda _state: None) -> dict:
    from webcli.sites.whatsapp import fetch

    return await fetch.login(timeout, on_state)


ADAPTER = Site(
    name="whatsapp",
    kind="data",
    provider="whatsapp",
    summary="WhatsApp Web: chats, read, search, status, login; send (dry run unless --confirm and --expect-chat)",
    cli=_cli,
    fetch="webcli.sites.whatsapp.fetch",
    ops=("status", "chats", "messages", "search", "send"),
    verbs={"chats": Strategy.UI, "read": Strategy.UI, "search": Strategy.UI, "send": Strategy.UI,
           "status": Strategy.UI},
    login=_login,
    status_op="status",
)
