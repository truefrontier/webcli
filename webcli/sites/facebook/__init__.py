"""www.facebook.com (webcli facebook): fbc (facebook-contacts-cli) ported onto the shared core. Identity provider
"facebook" (facebook:default, its own sign-in: a person runs `webcli identity login facebook:default`). Not Muse's
meta:default: Muse's Meta sign-in leaves facebook.com's device cookies in that profile but never its session.
STRICTLY read-only on Facebook: it navigates, reads, and scrolls; it never clicks, types, or submits, and the guard
aborts every request but page loads and the read queries listed in endpoints.py."""

from __future__ import annotations

from webcli.sites.base import Site, Strategy


def _cli():
    from webcli.sites.facebook.cli import group

    return group


async def _login(timeout: int, on_state=lambda _state: None) -> dict:
    from webcli.sites.facebook import fetch

    return await fetch.login(timeout, on_state)


ADAPTER = Site(
    name="facebook",
    kind="data",
    provider="facebook",
    summary="Facebook friends, read-only: sync, search, about, enrich, export, status (fbc parity; local cache)",
    cli=_cli,
    fetch="webcli.sites.facebook.fetch",
    ops=("status", "sync", "about", "about_many"),
    verbs={"sync": Strategy.UI, "about": Strategy.UI, "enrich": Strategy.UI, "status": Strategy.UI},
    login=_login,
    status_op="status",
)
