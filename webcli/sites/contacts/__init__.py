"""contacts.google.com (webcli contacts): gcontacts (google-contacts-cli) ported onto the shared core. Identity
provider "google" (the google:default profile gemini's login signs in). Read-only except `label add|remove
--confirm`, which changes label membership and nothing else."""

from __future__ import annotations

from webcli.sites.base import Site, Strategy


def _cli():
    from webcli.sites.contacts.cli import group

    return group


ADAPTER = Site(
    name="contacts",
    kind="data",
    provider="google",
    summary="Google Contacts: list, get, search, other, labels, export, status; label add|remove (dry run unless "
            "--confirm) (gcontacts parity)",
    cli=_cli,
    fetch="webcli.sites.contacts.fetch",
    ops=("status", "labels", "contacts", "other", "search", "get", "label_change"),
    verbs={"list": Strategy.COOKIE, "get": Strategy.COOKIE, "search": Strategy.COOKIE, "other": Strategy.COOKIE,
           "labels": Strategy.COOKIE, "export": Strategy.COOKIE, "status": Strategy.COOKIE,
           "label add": Strategy.COOKIE, "label remove": Strategy.COOKIE},
    status_op="status",
)
