"""`webcli gmail`: your Gmail filters from your own signed-in, hidden Chrome (google-gmail-cli parity, filters first).
`filters create` and `filters delete` are writes: dry runs unless --confirm."""

from __future__ import annotations

import click

from webcli.core.output import (
    EXIT_AUTH,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_USAGE,
    agent_output_options,
    confirm_write,
    die,
    emit_json,
    emit_rows,
    kv_table,
    resolve_format,
    write_options,
)
from webcli.sites import data
from webcli.sites.gmail import ADAPTER as SITE
from webcli.sites.gmail import parse
from webcli.sites.gmail.endpoints import WRITE_ACTS

FIELDS = ["id", "matches", "do_this", "from", "to", "subject", "query", "hasAttachment", "criteria"]
COMPACT = ("id", "matches", "do_this", "from", "query")
COLUMNS = ["id", "matches", "do_this"]
NOTES = [
    "Identity google:default (the profile `webcli identity login google:default` signs in, through gemini). Commands "
    "run one at a time in that identity's daemon, a hidden headless Chrome: no window, no Dock icon, no focus change.",
    "Every command loads Gmail's Settings > Filters page and reads its table, so each one takes several seconds. "
    "There is no filters API a browser session can use.",
    "FILTER_ID is the id `filters list` prints, matched exactly (never a prefix or a fragment).",
    "`filters create` needs at least one criterion (--from, --to, --subject, --query, --negated-query, "
    "--has-attachment) and one action (--skip-inbox, --mark-as-read, --star, --delete-it, --never-spam). Its dry run "
    "is offline: it checks the plan and prints it.",
    "`filters delete FILTER_ID`'s dry run reads the list and prints the filter it would delete (exit 3 if none).",
    "Writes: --confirm (alias --yes) creates or deletes; without it nothing is written; --dry-run with --confirm "
    "exits 2. The guard lets through exactly one filter write per --confirm run, bound to that filter's id or "
    "criteria; anything else the page tries is blocked. The write's act values are unverified: when Gmail uses "
    "another, the write is blocked, nothing changes, and the error names the blocked act.",
    "Not signed in exits 4: a person runs `webcli identity login google:default`.",
]
WRITES = [
    "Only `filters create --confirm` (one new filter through Gmail's create dialogs) and `filters delete FILTER_ID "
    f"--confirm` (that one filter). Gmail act values allowed: create {sorted(WRITE_ACTS['create'])}, delete "
    f"{sorted(WRITE_ACTS['delete'])} (unverified; anything else is blocked).",
    "Without --confirm both are dry runs that write nothing. Never: mail, labels, settings other than that filter.",
]

group = data.site_group(SITE, "Gmail filters: list, create, delete; writes are dry runs unless --confirm "
                              "(unofficial). Identity google:default.")


def _check(dry_run: bool, confirm: bool) -> None:
    if dry_run and confirm:
        raise click.UsageError("--dry-run and --confirm contradict each other; pass one.")


def _emit_one(title: str, row: dict, fmt) -> None:
    if resolve_format(fmt) == "json":
        emit_json(row)
    else:
        kv_table(title, row, resolve_format(fmt))


@group.command("status")
@agent_output_options(rows=False)
def status_cmd(fmt, quiet) -> None:
    """Whether google:default is signed in to Gmail (exit 4 if not)."""
    info = data.call(SITE, "status")
    data.emit_record("Gmail", info, fmt)
    if not info["signedIn"]:
        die("Not signed in to Gmail.", EXIT_AUTH, hint=data.login_hint())
    raise SystemExit(EXIT_OK)


@group.group("filters")
def filters_group() -> None:
    """List, create, and delete Gmail filters (create and delete: dry runs unless --confirm)."""


@filters_group.command("list")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="At most this many filters.")
@agent_output_options()
def list_cmd(limit, **opts) -> None:
    """Your message filters: id, what they match, what they do."""
    rows = data.call(SITE, "filters")
    emit_rows(rows[:limit], title="Gmail filters", fields=FIELDS, compact_fields=COMPACT, columns=COLUMNS,
              noun="filters", **opts)
    raise SystemExit(EXIT_OK)


@filters_group.command("create")
@click.option("--from", "from_", default=None, help="Match the From address.")
@click.option("--to", default=None, help="Match the To address.")
@click.option("--subject", default=None, help="Match the subject.")
@click.option("--query", default=None, help="Has the words.")
@click.option("--negated-query", default=None, help="Doesn't have.")
@click.option("--has-attachment", is_flag=True, help="Has an attachment.")
@click.option("--skip-inbox", is_flag=True, help="Skip the Inbox (archive it).")
@click.option("--mark-as-read", is_flag=True, help="Mark as read.")
@click.option("--star", is_flag=True, help="Star it.")
@click.option("--delete-it", is_flag=True, help="Delete it.")
@click.option("--never-spam", is_flag=True, help="Never send it to Spam.")
@write_options
@agent_output_options(formats=("table", "json"), rows=False)
def create_cmd(from_, to, subject, query, negated_query, has_attachment, skip_inbox, mark_as_read, star, delete_it,
               never_spam, dry_run, confirm, fmt, quiet) -> None:
    """Create a filter (dry run unless --confirm: prints the plan, writes nothing)."""
    _check(dry_run, confirm)
    criteria = {"from": from_, "to": to, "subject": subject, "query": query, "negatedQuery": negated_query,
                "hasAttachment": has_attachment}
    flags = {"skipInbox": skip_inbox, "markAsRead": mark_as_read, "star": star, "delete": delete_it,
             "neverSpam": never_spam}
    try:
        plan = parse.create_plan(criteria, [name for name, on in flags.items() if on])
    except ValueError as exc:
        die(str(exc), EXIT_USAGE)
    if not confirm_write(confirm, {"action": "create", "filter": plan}):
        raise SystemExit(EXIT_OK)
    _emit_one("Created filter", data.call(SITE, "create_filter", plan, confirm=True), fmt)
    raise SystemExit(EXIT_OK)


@filters_group.command("delete")
@click.argument("filter_id")
@write_options
@agent_output_options(formats=("table", "json"), rows=False)
def delete_cmd(filter_id, dry_run, confirm, fmt, quiet) -> None:
    """Delete one filter by its exact id from `filters list` (dry run unless --confirm: shows the filter, deletes
    nothing). Exit 3 if no filter has that id."""
    _check(dry_run, confirm)
    if not confirm:
        target = parse.find(data.call(SITE, "filters"), filter_id)
        if target is None:
            die(f"No filter {filter_id}.", EXIT_NOT_FOUND, hint="See `webcli gmail filters list`.")
        confirm_write(False, {"action": "delete", "filter": target})
        raise SystemExit(EXIT_OK)
    _emit_one("Deleted filter", data.call(SITE, "delete_filter", filter_id, confirm=True), fmt)
    raise SystemExit(EXIT_OK)


data.add_agent_help(group, notes=NOTES, writes=WRITES)
