"""www.facebook.com (webcli facebook): fbc (facebook-contacts-cli) ported onto the shared core. Identity provider
"meta": it shares meta:default with muse, whose sign-in cycles through facebook.com, so it has no login of its own.
STRICTLY read-only on Facebook: it navigates, reads, and scrolls; it never clicks, types, or submits, and the guard
aborts every request but page loads and the read queries listed in endpoints.py."""

from __future__ import annotations

from webcli.sites.base import Site, Strategy


def _cli():
    from webcli.sites.facebook.cli import group

    return group


ADAPTER = Site(
    name="facebook",
    kind="data",
    provider="meta",
    summary="Facebook friends, read-only: sync, search, about, enrich, export, status (fbc parity; local cache)",
    cli=_cli,
    fetch="webcli.sites.facebook.fetch",
    ops=("status", "sync", "about", "about_many"),
    verbs={"sync": Strategy.UI, "about": Strategy.UI, "enrich": Strategy.UI, "status": Strategy.UI},
    status_op="status",
)
