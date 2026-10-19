"""`webcli recorder`: your Google Recorder (Pixel) recordings, read-only, from your own signed-in, hidden Chrome
(google-recorder-cli parity, without its MCP server)."""

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
    emit_rows,
    select_fields,
    warn,
    write_atomic,
)
from webcli.sites import data, google
from webcli.sites.recorder import ADAPTER as SITE
from webcli.sites.recorder import parse

FIELDS = ["id", "title", "created_at", "duration_seconds", "has_transcript"]
COMPACT = ("id", "title", "created_at")
COLUMNS = ["id", "title", "created_at", "duration", "transcript"]
NOTES = [
    "Identity google:default (the profile `webcli identity login google:default` signs in, through gemini). Commands "
    "run one at a time in that identity's daemon, a hidden headless Chrome: no window, no Dock icon, no focus change.",
    "Every command loads recorder.google.com and reads the recording list the app itself fetches (GetRecordingList; "
    "its first page, as google-recorder-cli read it). RECORDING_ID is a recording's id from `list`.",
    "`transcript` is Recorder's on-device live transcript, words joined by spaces. On long recordings Recorder may "
    "have stopped transcribing partway, with no marker: check the length against the recording.",
    "`transcript` writes <id>.txt into -o DIR (default: the current directory; `-o -` prints it instead). "
    "`download` writes <title>.m4a into -o DIR. `sync` writes transcripts/<title>.txt and audio/<title>.m4a under "
    "-o DIR (default ./recorder-backup), replacing files of those names; a title already used in that sync (ignoring "
    "case) gets the recording id appended. Files are mode 0600.",
    "`sync` keeps going past a failed recording and lists it under errors; it exits 5 when any failed.",
    "No recording or search result exits 3; a recording without a transcript exits 3.",
    "Not signed in exits 4: a person runs `webcli identity login google:default`.",
    "Not ported from google-recorder-cli: its MCP server (`recorder mcp`, `recorder-mcp`) and `recorder skill` "
    "(a Claude skill file that documents the old binary and that MCP server). `webcli recorder agent-help` is the "
    "agent reference.",
]
WRITES = ["Nothing on Google Recorder: no rename, delete, share, or edit. The guard allows only the GetRecordingList "
          "and GetTranscription reads. `transcript`, `download`, and `sync` write only the local files you asked "
          "for."]

group = data.site_group(SITE, "Google Recorder (Pixel) recordings, read-only (unofficial). Identity google:default.")


def _table_rows(rows: list[dict]) -> list[dict]:
    return [{**row, "duration": "{}m {}s".format(*divmod(row["duration_seconds"], 60)),
             "transcript": "yes" if row["has_transcript"] else "no"} for row in rows]


def _emit(rows: list[dict], title: str, opts: dict) -> None:
    emit_rows(_table_rows(rows), title=title, fields=FIELDS, compact_fields=COMPACT, columns=COLUMNS,
              noun="recordings", **opts)


def _one(recording_id: str) -> dict:
    found = parse.find(data.call(SITE, "recordings"), recording_id)
    if found is None:
        die(f"No recording {recording_id}.", EXIT_NOT_FOUND, hint="run: webcli recorder list")
    return found


out_option = click.option("-o", "--output", "out", default=".", show_default=True, help="Directory to write into.")


@group.command("status")
@agent_output_options(rows=False)
def status_cmd(fmt, quiet) -> None:
    """Whether google:default is signed in to Google Recorder (exit 4 if not)."""
    info = data.call(SITE, "status")
    data.emit_record("Google Recorder", info, fmt)
    if not info["signedIn"]:
        die("Not signed in to Google Recorder.", EXIT_AUTH, hint=data.login_hint())
    raise SystemExit(EXIT_OK)


@group.command("list")
@click.option("--id-only", is_flag=True, help="Print only recording ids, one per line.")
@agent_output_options()
def list_cmd(id_only, **opts) -> None:
    """Your recordings: id, title, created_at, duration_seconds, has_transcript."""
    rows = data.call(SITE, "recordings")
    if id_only:
        for row in rows:
            click.echo(row["id"])
    else:
        _emit(rows, "Recordings", opts)
    raise SystemExit(EXIT_OK)


@group.command("info")
@click.argument("recording_id")
@agent_output_options()
def info_cmd(recording_id, fmt, compact, select, quiet, as_csv) -> None:
    """One recording's metadata. Exit 3 if there is no such recording."""
    found = _one(recording_id)
    payload = select_fields({k: found[k] for k in (COMPACT if compact else FIELDS)}, select)
    if as_csv:
        emit_csv([payload])
    else:
        data.emit_record(found["title"], payload, fmt)
    raise SystemExit(EXIT_OK)


@group.command("search")
@click.argument("query")
@click.option("--id-only", is_flag=True, help="Print only recording ids, one per line.")
@agent_output_options()
def search_cmd(query, id_only, **opts) -> None:
    """Recordings whose title contains QUERY (case-insensitive). Exit 3 when none does."""
    rows = [row for row in data.call(SITE, "recordings") if parse.matches(row, query)]
    if not rows:
        die(f"No recording title matches {query!r}.", EXIT_NOT_FOUND, hint="run: webcli recorder list")
    if id_only:
        for row in rows:
            click.echo(row["id"])
    else:
        _emit(rows, f"Search: {query}", opts)
    raise SystemExit(EXIT_OK)


@group.command("transcript")
@click.argument("recording_id")
@out_option
@agent_output_options(rows=False)
def transcript_cmd(recording_id, out, fmt, quiet) -> None:
    """Save a recording's transcript as <id>.txt in -o DIR (`-o -`: print it). Exit 3 if it has none."""
    found = data.call(SITE, "transcript", recording_id)
    if out == "-":
        click.echo(found["text"])
        raise SystemExit(EXIT_OK)
    path = Path(data.out_dir(out)) / f"{google.safe_name(found['id'], 'transcript')}.txt"
    try:
        write_atomic(path, found["text"])
    except OSError as exc:
        die(f"Can't write {path}: {exc}", EXIT_API)
    data.emit_record("Transcript", {"id": found["id"], "title": found["title"], "path": str(path),
                                    "chars": len(found["text"])}, fmt)
    raise SystemExit(EXIT_OK)


@group.command("download")
@click.argument("recording_id")
@out_option
@agent_output_options(rows=False)
def download_cmd(recording_id, out, fmt, quiet) -> None:
    """Save a recording's audio as <title>.m4a in -o DIR. Prints {id, title, path, bytes}."""
    data.emit_record("Downloaded", data.call(SITE, "download", recording_id, data.out_dir(out)), fmt)
    raise SystemExit(EXIT_OK)


@group.command("sync")
@click.option("-o", "--output", "out", default="./recorder-backup", show_default=True,
              help="Directory to back up into (transcripts/ and audio/ under it).")
@agent_output_options(rows=False)
def sync_cmd(out, fmt, quiet) -> None:
    """Back up every recording: transcripts/<title>.txt and audio/<title>.m4a. Prints the saved paths and errors;
    exit 5 when any recording failed."""
    result = data.call(SITE, "sync", data.out_dir(out))
    data.emit_record("Recorder sync", result, fmt)
    if result["errors"]:
        if not quiet:
            for line in result["errors"]:
                warn(line)
        die(f"{len(result['errors'])} transcript or audio download(s) failed.", EXIT_API,
            hint="Rerun `webcli recorder sync`; files that saved are replaced, not duplicated.")
    raise SystemExit(EXIT_OK)


data.add_agent_help(group, notes=NOTES, writes=WRITES)
