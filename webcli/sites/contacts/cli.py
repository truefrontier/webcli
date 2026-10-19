"""`webcli contacts`: your Google Contacts from your own signed-in, hidden Chrome (gcontacts parity). Read-only except
`label add|remove`, which changes label membership and nothing else, and only with --confirm."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import click

from webcli.core import config
from webcli.core.output import (
    EXIT_API,
    EXIT_AUTH,
    EXIT_NOT_FOUND,
    EXIT_OK,
    agent_output_options,
    die,
    emit_json,
    emit_rows,
    kv_table,
    note,
    resolve_format,
    rows_table,
    write_atomic,
    write_options,
)
from webcli.sites import data
from webcli.sites.contacts import ADAPTER as SITE
from webcli.sites.contacts import export, parse
from webcli.sites.contacts.endpoints import BATCH_PAUSE, MEMBERSHIP_BATCH, WRITE_RPC

FIELDS = ["id", "name", "email", "phone", "org", "labels", "source"]
COMPACT = ("id", "name", "email", "phone")
COLUMNS = ["id", "name", "email", "phone", "org", "labels"]
NOTES = [
    "Identity google:default (the profile `webcli identity login google:default` signs in, through gemini). Commands "
    "run one at a time in that identity's daemon, a hidden headless Chrome: no window, no Dock icon, no focus change.",
    "Contact ids look like c1234567890123456789 (`list`, `search`, `other` print them); `get` also takes people/c..., "
    "a contacts.google.com/person/ URL, an email address, or a phone number.",
    "`list` and `export` read My Contacts; `other` reads Other contacts; `search` covers both (source: contacts|other).",
    "Label names match case-insensitively with whitespace trimmed and collapsed; a label id always works. Two labels "
    "with the same name exit 2, listing both ids.",
    "`export --format` is the FILE format (json|csv|vcf), not the stdout format. Its summary {path, format, count} "
    "is JSON when piped. Default file: ~/.webcli/sites/contacts/exports/contacts-<UTC time>.<format>, mode 0600.",
    "`label add|remove LABEL` needs exactly one selector: --from-label NAME|ID, --query Q (saved contacts only), or "
    "--ids ID (repeat or comma-separate). Dry run by default; --confirm (alias --yes) writes; --dry-run with "
    "--confirm exits 2. Result: {label, labelId, action, matched, wouldChange, changed, alreadyLabeled|alreadyAbsent, "
    "failed, failures, dryRun, createLabel, skippedOtherContacts, contacts}; --confirm adds accepted (changes Google "
    "acknowledged) and, when it stops early, stopped {code, error}.",
    "`label` is idempotent: contacts already in the wanted state are skipped, so rerunning after an exit 6 is safe.",
    f"`label --confirm` sends {MEMBERSHIP_BATCH} contacts per call with a {BATCH_PAUSE[0]:g}-{BATCH_PAUSE[1]:g}s "
    "pause between calls, then reads them back; `changed` counts only contacts Google shows in the new state. A run "
    "with failures exits 5; a stop exits with stopped.code (6 rate limited, 4 sign-in, 5 site) after printing the "
    "result so far.",
    "System groups (My Contacts, Starred, ...) are never a target (exit 2). Other contacts can't hold labels.",
    "Not signed in exits 4: a person runs `webcli identity login google:default`.",
]
WRITES = [
    "Only `label add` / `label remove` with --confirm: adds the selected contacts to one of your labels or removes "
    f"them from it (rpc {WRITE_RPC['membership']}); `label add --create-missing --confirm` creates that label first "
    f"({WRITE_RPC['create_label']}). The guard checks each write's payload: one label, one direction, only the "
    "selected contacts.",
    "Without --confirm every `label` run is a dry run: it reads, prints what would change, and its page can't write.",
    "Never: contact create/edit/delete, starring, merging, hiding, label rename/delete, system groups.",
    "Local only: `export` writes a file (mode 0600).",
]

group = data.site_group(SITE, "Google Contacts: read-only except `label add|remove --confirm` (unofficial). "
                              "Identity google:default.")


def _lookup_arg(ctx: click.Context, param: click.Parameter, value: str) -> str:
    try:
        parse.lookup_key(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from None
    return value


def _ids_arg(ctx: click.Context, param: click.Parameter, value: tuple[str, ...]) -> list[str] | None:
    if not value:
        return None
    out = []
    for raw in (part for v in value for part in re.split(r"[\s,]+", v) if part):
        try:
            kind, cid = parse.lookup_key(raw)
        except ValueError:
            kind, cid = "", raw
        if kind != "id":
            raise click.BadParameter(f"{raw!r} is not a contact id (c123...).")
        out.append(cid)
    if not out:
        raise click.BadParameter("no ids given.")
    return out


def _flat(value) -> str:
    """A field's value as one table cell: lists one item per line, dicts as their values."""
    if isinstance(value, list):
        return "\n".join(_flat(v) for v in value)
    if isinstance(value, dict):
        return "  ".join(str(v) if k in ("value", "formatted", "date", "person") else f"{k}: {v}"
                         for k, v in value.items())
    return "" if value is None else str(value)


def _emit(contacts: list[dict], title: str, *, fmt, compact, select, quiet, as_csv) -> None:
    rows = [parse.row(c) for c in contacts]
    if as_csv or resolve_format(fmt) != "json":
        rows = [{**r, "labels": ", ".join(r["labels"])} for r in rows]
    emit_rows(rows, title=title, fields=FIELDS, compact_fields=COMPACT, columns=COLUMNS, noun="contacts", fmt=fmt,
              compact=compact, select=select, quiet=quiet, as_csv=as_csv)


@group.command("status")
@agent_output_options(rows=False)
def status_cmd(fmt, quiet) -> None:
    """Whether google:default is signed in to Contacts and its API answers (exit 4 if signed out, 5 if the API
    errors)."""
    info = data.call(SITE, "status")
    data.emit_record("Google Contacts", info, fmt)
    if not info["signedIn"]:
        die("Not signed in to Google Contacts.", EXIT_AUTH, hint=data.login_hint())
    raise SystemExit(EXIT_OK if info["contactsApi"] == "ok" else EXIT_API)


@group.command("list")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="Stop after N contacts (default: all).")
@click.option("--label", default=None, metavar="NAME|ID", help="Only members of this label.")
@agent_output_options()
def list_cmd(limit, label, **opts) -> None:
    """My Contacts: id, name, primary email, primary phone, org, labels."""
    result = data.call(SITE, "contacts", limit, label)
    if not result["contacts"]:
        die("No contacts" + (f" with label {result['label']['name']!r}." if result["label"] else "."), EXIT_NOT_FOUND)
    _emit(result["contacts"], "Google Contacts", **opts)
    raise SystemExit(EXIT_OK)


@group.command("other")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="Stop after N contacts (default: all).")
@agent_output_options()
def other_cmd(limit, **opts) -> None:
    """Other contacts: people you've emailed who aren't saved contacts."""
    rows = data.call(SITE, "other", limit)
    if not rows:
        die("No Other contacts.", EXIT_NOT_FOUND)
    _emit(rows, "Other contacts", **opts)
    raise SystemExit(EXIT_OK)


@group.command("search")
@click.argument("query")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="At most this many results.")
@agent_output_options()
def search_cmd(query, limit, **opts) -> None:
    """Google Contacts' own search (names, emails, phones, ...): My Contacts first, then Other contacts. Exit 3 when
    nothing matches."""
    hits = data.call(SITE, "search", query, limit)
    if not hits:
        die(f"No contacts match {query!r}.", EXIT_NOT_FOUND)
    _emit(hits, f"Search: {query}", **opts)
    raise SystemExit(EXIT_OK)


@group.command("get")
@click.argument("ref", metavar="ID|EMAIL|PHONE", callback=_lookup_arg)
@agent_output_options(rows=False)
def get_cmd(ref: str, fmt, quiet) -> None:
    """One contact in full: names, emails, phones, organizations, addresses, birthdays, notes, labels, URLs. Exit 3
    if nothing matches."""
    contact = data.call(SITE, "get", ref)
    if resolve_format(fmt) == "json":
        emit_json(contact)
    else:
        kv_table(contact["name"] or contact["id"], {k: _flat(v) for k, v in contact.items() if v not in (None, [], "")},
                 resolve_format(fmt))
    if contact.get("otherMatches"):
        note(f"{len(contact['otherMatches'])} more contacts match exactly: {', '.join(contact['otherMatches'])}",
             quiet=quiet)
    raise SystemExit(EXIT_OK)


@group.command("labels")
@agent_output_options()
def labels_cmd(fmt, compact, select, quiet, as_csv) -> None:
    """Your labels and the system groups, with member counts."""
    rows = data.call(SITE, "labels")
    emit_rows(rows, title="Contact labels", fields=["id", "name", "count", "system"], compact_fields=("id", "name"),
              columns=["id", "name", "count", "system"], noun="labels", fmt=fmt, compact=compact, select=select,
              quiet=quiet, as_csv=as_csv)
    raise SystemExit(EXIT_OK)


@group.command("export")
@click.option("--format", "fmt", type=click.Choice(export.FORMATS), default="json", show_default=True,
              help="The FILE format. The stdout summary is JSON when piped.")
@click.option("--out", "-o", "out", default=None, type=click.Path(dir_okay=False, resolve_path=True),
              help="Output file (default: ~/.webcli/sites/contacts/exports/contacts-<UTC time>.<format>).")
@click.option("--label", default=None, metavar="NAME|ID", help="Only members of this label.")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="Stop after N contacts (default: all).")
@click.option("--quiet", "-q", is_flag=True, help="Suppress non-data messages on stderr.")
def export_cmd(fmt: str, out: str | None, label: str | None, limit: int | None, quiet: bool) -> None:
    """Write My Contacts (full records) to a JSON, CSV, or vCard 3.0 file (mode 0600); print {path, format, count}."""
    if out and not Path(out).parent.is_dir():
        raise click.BadParameter(f"Directory {Path(out).parent} doesn't exist.", param_hint="--out")
    contacts = data.call(SITE, "contacts", limit, label)["contacts"]
    if not contacts:
        die("No contacts to export.", EXIT_NOT_FOUND)
    if out:
        path = Path(out)
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = config.private_dir(config.site_dir(SITE.name) / "exports") / f"contacts-{stamp}.{fmt}"
    try:
        write_atomic(path, export.render(contacts, fmt))
    except OSError as exc:
        die(f"Can't write {path}: {exc}", EXIT_API)
    if resolve_format(None) == "json":
        emit_json({"path": str(path), "format": fmt, "count": len(contacts)})
    else:
        click.echo(f"Wrote {len(contacts)} contacts to {path}")
    raise SystemExit(EXIT_OK)


@group.group("label")
def label_group() -> None:
    """Change label membership: a dry run unless --confirm. The only writes webcli contacts makes."""


def _label_options(*, create: bool):
    def decorator(f):
        f = agent_output_options(formats=("table", "json"), rows=False)(f)
        f = write_options(f)
        if create:
            f = click.option("--create-missing", is_flag=True,
                             help="Create LABEL if no label has that name (a dry run says it would).")(f)
        f = click.option("--ids", multiple=True, callback=_ids_arg, metavar="ID[,ID...]",
                         help="Selector: these contact ids (repeat the flag or comma-separate).")(f)
        f = click.option("--query", default=None, metavar="Q", help="Selector: saved contacts matching this search.")(f)
        f = click.option("--from-label", default=None, metavar="NAME|ID", help="Selector: members of this label.")(f)
        return click.argument("label", metavar="LABEL")(f)

    return decorator


def _label_text(r: dict) -> None:
    verb = "add to" if r["action"] == "add" else "remove from"
    target = f"{r['label']!r}" + (f" ({r['labelId']})" if r["labelId"] else " (new label)")
    settled = r.get("alreadyLabeled", r.get("alreadyAbsent"))
    state = "already labeled" if r["action"] == "add" else "already without it"
    if r["dryRun"]:
        lead = f"DRY RUN: would {verb} {target}: {r['wouldChange']} of {r['matched']} matched ({settled} {state})."
        if r["createLabel"]:
            lead += " The label would be created (needs --create-missing --confirm)."
        lead += " Nothing was written. Re-run with --confirm to apply."
    else:
        done = "Added to" if r["action"] == "add" else "Removed from"
        lead = f"{done} {target}: {r['changed']} changed, {settled} {state}, {r['failed']} failed."
    click.echo(lead)
    rows_table("contacts", [{**c, "change": "yes" if c["change"] else ""} for c in r["contacts"]],
               ["id", "name", "change"])
    for f in r["failures"]:
        click.echo(f"failed: {f['id']}: {f['error']}")


def _label_run(action: str, label: str, *, from_label, query, ids, dry_run, confirm, create_missing, fmt) -> None:
    if dry_run and confirm:
        raise click.UsageError("--dry-run and --confirm contradict each other; pass one.")
    chosen = [name for name, v in (("--from-label", from_label), ("--query", query), ("--ids", ids)) if v is not None]
    if len(chosen) != 1:
        raise click.UsageError("Pass exactly one selector: --from-label NAME, --query Q, or --ids ID."
                               + (f" (got {', '.join(chosen)})" if chosen else ""))
    if not label.strip():
        raise click.BadParameter("LABEL is empty.", param_hint="LABEL")
    result = data.call(SITE, "label_change", action, label, from_label=from_label, query=query, ids=ids,
                       confirm=confirm, create_missing=create_missing)
    if resolve_format(fmt) == "json":
        emit_json(result)
    else:
        _label_text(result)
    if result.get("stopped"):
        stop = result["stopped"]
        die(f"Stopped after {result['changed']} of {result['wouldChange']} changes: {stop['error']}", stop["code"],
            hint="Rerun the same command: contacts already changed are skipped.")
    if confirm and result["failed"]:
        die(f"{result['failed']} contacts failed (see failures).", EXIT_API)
    raise SystemExit(EXIT_OK)


@label_group.command("add")
@_label_options(create=True)
def label_add_cmd(label, from_label, query, ids, create_missing, dry_run, confirm, fmt, quiet) -> None:
    """Add the selected contacts to LABEL (dry run unless --confirm; skips contacts already in it)."""
    _label_run("add", label, from_label=from_label, query=query, ids=ids, dry_run=dry_run, confirm=confirm,
               create_missing=create_missing, fmt=fmt)


@label_group.command("remove")
@_label_options(create=False)
def label_remove_cmd(label, from_label, query, ids, dry_run, confirm, fmt, quiet) -> None:
    """Remove the selected contacts from LABEL (dry run unless --confirm; skips contacts not in it)."""
    _label_run("remove", label, from_label=from_label, query=query, ids=ids, dry_run=dry_run, confirm=confirm,
               create_missing=False, fmt=fmt)


data.add_agent_help(group, notes=NOTES, writes=WRITES)
