"""`webcli keep`: your Google Keep notes, read-only, from your own signed-in, hidden Chrome (google-keep-cli parity)."""

from __future__ import annotations

from pathlib import Path

import click

from webcli.core.output import (
    EXIT_API,
    EXIT_AUTH,
    EXIT_NOT_FOUND,
    EXIT_OK,
    agent_output_options,
    die,
    emit_csv,
    emit_json,
    emit_rows,
    kv_table,
    resolve_format,
    select_fields,
    warn,
)
from webcli.sites import data
from webcli.sites.keep import ADAPTER as SITE
from webcli.sites.keep import parse
from webcli.sites.keep.endpoints import SYNC_SECONDS

FIELDS = ["id", "server_id", "type", "title", "text", "pinned", "archived", "color", "updated", "created", "labels"]
COMPACT = ("id", "type", "title", "updated", "pinned", "archived")
COLUMNS = ["id", "type", "title", "updated"]
NOTES = [
    "Identity google:default (the profile `webcli identity login google:default` signs in, through gemini). Commands "
    "run one at a time in that identity's daemon, a hidden headless Chrome: no window, no Dock icon, no focus change.",
    "Every command loads keep.google.com and reads the notes sync the app itself runs (notes/v1/changes), so each "
    "one takes a few seconds and sees what Keep streams while it loads.",
    "Archived notes are left out of `list` and `search` unless --archived; `info` and `get` find them too.",
    "NOTE_ID is a note's id or server id; a fragment of an id also matches (the first match wins).",
    f"When Keep doesn't finish its sync within {SYNC_SECONDS}s a warning goes to stderr: the notes may be incomplete.",
    "List items under a list are not separate rows; the list's text has them when Keep sends indexableText.",
    "Not signed in exits 4: a person runs `webcli identity login google:default`.",
]
WRITES = ["Nothing: no create, edit, archive, delete, label, or share. The guard aborts any notes sync that would "
          "upload an edit."]

group = data.site_group(SITE, "Google Keep notes, read-only (unofficial). Identity google:default.")


def _notes(*, archived: bool, quiet: bool) -> list[dict]:
    result = data.call(SITE, "notes")
    if not result["complete"] and not quiet:
        warn(f"Keep didn't finish its sync within {SYNC_SECONDS}s: the notes may be incomplete.")
    return [n for n in result["notes"] if archived or not n["archived"]]


def _emit(rows: list[dict], title: str, *, id_only: bool, fmt, compact, select, quiet, as_csv) -> None:
    if id_only:
        for row in rows:
            click.echo(row["id"])
        return
    if compact or (not as_csv and resolve_format(fmt) == "table"):
        rows = [{**row, "title": parse.display_title(row)} for row in rows]
    emit_rows(rows, title=title, fields=FIELDS, compact_fields=COMPACT, columns=COLUMNS, noun="notes", fmt=fmt,
              compact=compact, select=select, quiet=quiet, as_csv=as_csv)


def _one(note_id: str) -> dict:
    found = parse.find(_notes(archived=True, quiet=False), note_id)
    if found is None:
        die(f"No note {note_id}.", EXIT_NOT_FOUND, hint="See `webcli keep list --id-only`.")
    return found


@group.command("status")
@agent_output_options(rows=False)
def status_cmd(fmt, quiet) -> None:
    """Whether google:default is signed in to Keep (exit 4 if not)."""
    info = data.call(SITE, "status")
    data.emit_record("Keep", info, fmt)
    if not info["signedIn"]:
        die("Not signed in to Google Keep.", EXIT_AUTH, hint=data.login_hint())
    raise SystemExit(EXIT_OK)


@group.command("list")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="At most this many notes.")
@click.option("--archived", is_flag=True, help="Include archived notes.")
@click.option("--id-only", is_flag=True, help="Print only note ids, one per line.")
@agent_output_options()
def list_cmd(limit, archived, id_only, **opts) -> None:
    """Your notes, most recently edited first."""
    rows = _notes(archived=archived, quiet=opts["quiet"])
    _emit(rows[:limit], "Keep notes", id_only=id_only, **opts)
    raise SystemExit(EXIT_OK)


@group.command("search")
@click.argument("query")
@click.option("--limit", type=click.IntRange(min=1), default=50, show_default=True, help="At most this many notes.")
@click.option("--archived", is_flag=True, help="Include archived notes.")
@click.option("--id-only", is_flag=True, help="Print only note ids, one per line.")
@agent_output_options()
def search_cmd(query, limit, archived, id_only, **opts) -> None:
    """Notes whose title, text, or id contains QUERY (case-insensitive). Exit 3 when none does."""
    rows = [n for n in _notes(archived=archived, quiet=opts["quiet"]) if parse.matches(n, query)]
    if not rows:
        die(f"No note matches {query!r}.", EXIT_NOT_FOUND, hint="Try --archived, or a shorter query.")
    _emit(rows[:limit], f"Search: {query}", id_only=id_only, **opts)
    raise SystemExit(EXIT_OK)


@group.command("info")
@click.argument("note_id")
@agent_output_options(formats=("table", "md", "text", "json"))
def info_cmd(note_id, fmt, compact, select, quiet, as_csv) -> None:
    """One note's fields (`--format text`: its title and text). Exit 3 if no note matches."""
    note = _one(note_id)
    payload = {**{k: note[k] for k in COMPACT}, "title": parse.display_title(note)} if compact else note
    payload = select_fields(payload, select)
    out = "csv" if as_csv else resolve_format(fmt)
    if out == "csv":
        emit_csv([payload])
    elif out == "json":
        emit_json(payload)
    elif out == "text":
        click.echo(parse.text(note), nl=False)
    else:
        kv_table(note["title"] or note["id"], payload, out)
    raise SystemExit(EXIT_OK)


@group.command("get")
@click.argument("note_id")
@click.option("-o", "--output", "out_path", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Write the text to this file instead of stdout.")
@click.option("--quiet", "-q", is_flag=True, help="Suppress non-data messages on stderr.")
def get_cmd(note_id, out_path, quiet) -> None:
    """A note's text: its title, a blank line, then the body. Exit 3 if no note matches."""
    text = parse.text(_one(note_id))
    if out_path is None:
        click.echo(text, nl=False)
        raise SystemExit(EXIT_OK)
    try:
        out_path.expanduser().write_text(text, encoding="utf-8")
    except OSError as exc:
        die(f"Can't write {out_path}: {exc}", EXIT_API)
    if not quiet:
        click.echo(f"Wrote {out_path}", err=True)
    raise SystemExit(EXIT_OK)


data.add_agent_help(group, notes=NOTES, writes=WRITES)
