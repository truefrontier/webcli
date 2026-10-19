"""`webcli loom`: your Loom library and its official transcripts, read-only, from your own signed-in, hidden Chrome
(loom-cli parity)."""

from __future__ import annotations

from pathlib import Path

import click

from webcli.core import output
from webcli.core.output import (
    EXIT_API,
    EXIT_AUTH,
    EXIT_OK,
    agent_output_options,
    die,
    emit_csv,
    emit_json,
    emit_rows,
    kv_table,
    resolve_format,
    select_fields,
)
from webcli.sites import data
from webcli.sites.loom import ADAPTER as SITE
from webcli.sites.loom import parse
from webcli.sites.loom.endpoints import TRANSCRIPT_SECONDS

FIELDS = ["id", "title", "visibility"]
COMPACT = ("id", "title", "visibility")
INFO_COMPACT = ("id", "title", "created_at", "duration_seconds", "views")
NOTES = [
    "Identity loom:default, Loom's own sign-in: a person runs `webcli identity login loom:default` once (a visible "
    "Chrome window). Commands run one at a time in that identity's daemon, a hidden headless Chrome: no window, no "
    "Dock icon, no focus change.",
    "VIDEO is a 32-hex video id or a share URL (https://www.loom.com/share/<id>).",
    "`list` reads your library (videos you made, not in a folder), newest first, 12 per page, following the cursor "
    "until --limit or the end.",
    f"`transcript` opens the share page and reads the official transcript file Loom made; none within "
    f"{TRANSCRIPT_SECONDS}s exits 3. Default output is text (`[1m 5s] phrase` lines) even when piped; --format json "
    "gives [{ts, text}].",
    "Not signed in exits 4: a person runs `webcli identity login loom:default`.",
]
WRITES = ["Nothing: no upload, edit, delete, share, comment, or reaction. The guard lets only GraphQL queries "
          "through; any mutation the page tries is blocked."]

group = data.site_group(SITE, "Loom: your library and official transcripts, read-only (unofficial). Identity "
                              "loom:default.")


def _video_arg(ctx: click.Context, param: click.Parameter, value: str) -> str:
    try:
        return parse.video_id(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from None


def _call(op: str, *args):
    data.need_profile()
    return data.call(SITE, op, *args)


@group.command("status")
@agent_output_options(rows=False)
def status_cmd(fmt, quiet) -> None:
    """Whether loom:default is signed in to Loom (exit 4 if not)."""
    info = data.call(SITE, "status") if data.identity().profile_exists() else {"signedIn": False}
    data.emit_record("Loom", info, fmt)
    if not info["signedIn"]:
        die("Not signed in to Loom.", EXIT_AUTH, hint=data.login_hint())
    raise SystemExit(EXIT_OK)


@group.command("list")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="Stop after this many videos.")
@click.option("--id-only", is_flag=True, help="Print only video ids, one per line.")
@agent_output_options()
def list_cmd(limit, id_only, **opts) -> None:
    """Videos in your library, newest first."""
    rows = _call("videos", limit)
    if id_only:
        for row in rows:
            click.echo(row["id"])
        raise SystemExit(EXIT_OK)
    emit_rows(rows, title="Looms", fields=FIELDS, compact_fields=COMPACT, columns=FIELDS, noun="videos", **opts)
    raise SystemExit(EXIT_OK)


@group.command("info")
@click.argument("video", callback=_video_arg)
@agent_output_options()
def info_cmd(video, fmt, compact, select, quiet, as_csv) -> None:
    """One video: title, created, duration, views, owner, share URL. Exit 3 if Loom has no such video for you."""
    record = _call("video", video)
    payload = select_fields({k: record.get(k) for k in INFO_COMPACT} if compact else record, select)
    out = "csv" if as_csv else resolve_format(fmt)
    if out == "csv":
        emit_csv([payload])
    elif out == "json":
        emit_json(payload)
    else:
        kv_table(record["title"] or video, {**payload, "duration_seconds": parse.fmt_duration(
            record.get("duration_seconds"))} if "duration_seconds" in payload else payload, out)
    raise SystemExit(EXIT_OK)


@group.command("transcript")
@click.argument("video", callback=_video_arg)
@click.option("-o", "--output", "out_path", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Write the text transcript to this file instead of stdout.")
@agent_output_options(formats=("text", "json"))
def transcript_cmd(video, out_path, fmt, compact, select, quiet, as_csv) -> None:
    """The official transcript (text by default, even when piped; --format json for [{ts, text}]). Exit 3 if
    there is none."""
    rows = _call("transcript", video)
    if not rows:
        die(f"No transcript for {video}.", output.EXIT_NOT_FOUND, hint="See `webcli loom info ID`.")
    payload = select_fields(rows, select)
    out = "csv" if as_csv else fmt or ("json" if output.env_format() == "json" else "text")
    if out == "csv":
        emit_csv(payload)
    elif out == "json":
        emit_json(payload)
    elif out_path is not None:
        text = parse.transcript_text(rows)
        try:
            out_path.expanduser().write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            die(f"Can't write {out_path}: {exc}", EXIT_API)
        if not quiet:
            click.echo(f"Wrote {out_path}", err=True)
    else:
        click.echo(parse.transcript_text(rows))
    raise SystemExit(EXIT_OK)


data.add_agent_help(group, notes=NOTES, writes=WRITES)
