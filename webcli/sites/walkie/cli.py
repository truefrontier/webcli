"""`webcli walkie`: Walkie.app's meeting transcripts and dictations from its local SQLite (walkie-cli parity).
Read-only; no browser, no identity, no network."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable

import click

from webcli import __version__
from webcli.core import config, output
from webcli.core.errors import ConfigError, NotFound
from webcli.core.output import (
    EXIT_API,
    EXIT_CONFIG,
    EXIT_NOT_FOUND,
    EXIT_OK,
    agent_output_options,
    die,
    emit_json,
    emit_rows,
    kv_table,
    resolve_format,
)
from webcli.sites.walkie import db as walkie_db

NOTES = [
    "Walkie.app's local SQLite history, opened read-only (mode=ro): no browser, no identity, no network.",
    f"Database: --db PATH, else ${walkie_db.DB_ENV}, else ~/Library/Application Support/com.pais.walkie/accounts/"
    "<account>/history.db (--account picks one; default: the only one, or the most recently modified). "
    "--db wins over --account.",
    "Exit 3 when there is no Walkie data on this Mac, no such --account, or no such meeting; exit 7 when --db / "
    f"${walkie_db.DB_ENV} names no file or a file that isn't a readable Walkie database; exit 5 when Walkie changed "
    "its schema.",
    "MEETING is a meeting id or a title substring (the newest match wins).",
    "Times are local ISO 8601; durations and transcript times read like 42s, 3.5m, 1.2h.",
]
WRITES = ["Nothing: the database is opened read-only, and Walkie.app is never contacted."]
MEETING_FIELDS = ["id", "title", "source", "started", "duration", "status", "folder", "segment_count"]
DICTATION_FIELDS = ["id", "title", "timestamp", "app", "text"]
SEGMENT_FIELDS = ["time", "speaker", "text"]


def _env() -> dict[str, str]:
    return {
        output.FORMAT_ENV: "json | table | text | md. Default output when --format is not given.",
        walkie_db.DB_ENV: "The history.db to read when --db is not given.",
        config.HOME_ENV: "The data home (default ~/.webcli; walkie keeps nothing there).",
    }


@click.group(cls=output.AgentGroup, name="walkie")
@click.version_option(__version__, prog_name="webcli walkie")
def group() -> None:
    """Walkie.app meeting transcripts and dictations, read-only from its local SQLite."""
    output.env_format()


def db_options(f: Callable) -> Callable:
    f = click.option("--account", default=None, help="The Walkie account id (when there are several).")(f)
    return click.option("--db", "db_path", type=click.Path(path_type=Path), default=None,
                        help=f"Path to a Walkie history.db (env {walkie_db.DB_ENV}).")(f)


def _read(db_path: Path | None, account: str | None, query: Callable[[sqlite3.Connection, Path], Any]) -> Any:
    """query(conn, path) on the located database; a missing or unreadable one exits 3, 5, or 7 with a hint."""
    try:
        path = walkie_db.locate(db_path, account)
        with walkie_db.connect(path) as conn:
            return query(conn, path)
    except NotFound as exc:
        die(str(exc), EXIT_NOT_FOUND, hint=exc.hint)
    except ConfigError as exc:
        die(str(exc), EXIT_CONFIG, hint=exc.hint)
    except walkie_db.SchemaChanged as exc:
        die(str(exc), EXIT_API, hint="webcli's walkie site needs an update for this Walkie version.")


def _record(title: str, data: dict, fmt: str | None) -> None:
    out = resolve_format(fmt)
    if out == "json":
        emit_json(data)
    else:
        kv_table(title, data, out)


@group.command("status")
@db_options
@agent_output_options(rows=False)
def status_cmd(db_path, account, fmt, quiet) -> None:
    """Which database is read, and its meeting, segment, and dictation counts."""
    _record("Walkie", _read(db_path, account, walkie_db.status), fmt)
    raise SystemExit(EXIT_OK)


@group.group("meetings")
def meetings_group() -> None:
    """Meetings: list, show (alias get), transcript (aliases history, read)."""


@meetings_group.command("list")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="At most this many meetings.")
@db_options
@agent_output_options()
def meetings_list(limit, db_path, account, **opts) -> None:
    """Meetings, newest first."""
    rows = _read(db_path, account, lambda conn, _path: walkie_db.meetings(conn, limit))
    emit_rows(rows, title="Walkie meetings", fields=MEETING_FIELDS, compact_fields=("id", "title", "started"),
              columns=["id", "title", "started", "duration", "source", "segment_count"], noun="meetings", **opts)
    raise SystemExit(EXIT_OK)


@meetings_group.command("show")
@click.argument("meeting_id", metavar="MEETING")
@db_options
@agent_output_options(rows=False)
def meetings_show(meeting_id, db_path, account, fmt, quiet) -> None:
    """One meeting's details: sections, action items, participants, attendees, notes. Exit 3 if none matches."""
    meeting = _read(db_path, account, lambda conn, _: walkie_db.meeting(conn, meeting_id))
    if meeting is None:
        die(f"No Walkie meeting matches {meeting_id!r}.", EXIT_NOT_FOUND, hint="See `webcli walkie meetings list`.")
    _record(meeting["title"], meeting, fmt)
    raise SystemExit(EXIT_OK)


@meetings_group.command("transcript")
@click.argument("meeting_id", metavar="MEETING")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="At most this many segments.")
@db_options
@agent_output_options()
def meetings_transcript(meeting_id, limit, db_path, account, **opts) -> None:
    """A meeting's transcript segments (time, speaker, text) in order. Exit 3 if no meeting matches or it has
    none."""
    def query(conn: sqlite3.Connection, _path: Path) -> list[dict] | None:
        found = walkie_db.meeting(conn, meeting_id)
        return walkie_db.transcript(conn, found["id"], limit) if found else None

    segments = _read(db_path, account, query)
    if not segments:
        die(f"No Walkie meeting or transcript matches {meeting_id!r}.", EXIT_NOT_FOUND,
            hint="See `webcli walkie meetings list`.")
    emit_rows(segments, title="Transcript", fields=SEGMENT_FIELDS, compact_fields=("time", "text"),
              columns=SEGMENT_FIELDS, noun="segments", **opts)
    raise SystemExit(EXIT_OK)


meetings_group.add_command(meetings_show, "get")
meetings_group.add_command(meetings_transcript, "history")
meetings_group.add_command(meetings_transcript, "read")


@group.group("dictations")
def dictations_group() -> None:
    """Dictations: list."""


@dictations_group.command("list")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="At most this many dictations.")
@db_options
@agent_output_options()
def dictations_list(limit, db_path, account, **opts) -> None:
    """Dictations, newest first."""
    rows = _read(db_path, account, lambda conn, _path: walkie_db.dictations(conn, limit))
    emit_rows(rows, title="Walkie dictations", fields=DICTATION_FIELDS, compact_fields=("id", "timestamp", "title"),
              columns=["timestamp", "app", "title", "text"], noun="dictations", **opts)
    raise SystemExit(EXIT_OK)


group.add_command(output.agent_help_command(group, version=__version__, env=_env, notes=NOTES, writes=WRITES))
