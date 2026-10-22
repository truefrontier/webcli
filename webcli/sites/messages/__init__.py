"""messages.google.com (webcli messages): gmsg (google-messages-cli) ported onto the shared core. Identity provider
"messages" (messages:default, its own sign-in): Google Messages for web pairs with the phone by QR code, not a Google
account, so it can't share google:default. A person runs `webcli identity login messages:default` once and scans the
code; the pairing lives in that profile.

Read-only except `send`: a dry run by default, and with --confirm the guard lets out one send and nothing else (and,
for `send --new-ok`, the one conversation it opens). One Messages session at a time: run/messages.session.lock."""

from __future__ import annotations

from webcli.sites.base import Site, Strategy


def _cli():
    from webcli.sites.messages.cli import group

    return group


async def _login(timeout: int, on_state=lambda _state: None) -> dict:
    from webcli.sites.messages import fetch

    return await fetch.login(timeout, on_state)


ADAPTER = Site(
    name="messages",
    kind="data",
    provider="messages",
    summary="Google Messages for web: list (alias threads), read, search (past the newest, by contact or number), "
            "status; send to a conversation, or a new one with --new-ok (dry run unless --confirm)",
    cli=_cli,
    fetch="webcli.sites.messages.fetch",
    ops=("status", "conversations", "messages", "search", "send"),
    verbs={"list": Strategy.UI, "threads": Strategy.UI, "read": Strategy.UI, "search": Strategy.UI,
           "send": Strategy.UI, "status": Strategy.UI},
    login=_login,
    status_op="status",
)
