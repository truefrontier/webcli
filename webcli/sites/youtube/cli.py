"""`webcli youtube`: your YouTube watch history, read-only, from your own signed-in, hidden Chrome (ytcli parity)."""

from __future__ import annotations

from datetime import datetime

import click

from webcli.core.output import (
    EXIT_AUTH,
    EXIT_NOT_FOUND,
    EXIT_OK,
    agent_output_options,
    die,
    emit_csv,
    emit_json,
    emit_md,
    note_showing,
    resolve_format,
    rows_table,
    select_fields,
    truncate,
    warn,
)
from webcli.sites import data
from webcli.sites.youtube import ADAPTER as SITE
from webcli.sites.youtube.endpoints import DEFAULT_LIMIT
from webcli.sites.youtube.parse import parse_since

FIELDS = ("title", "channel", "channelUrl", "videoId", "url", "watchedAt", "watchedAtPrecision", "durationSec",
          "isShort", "source")
COMPACT = ("watchedAt", "title", "channel", "videoId")
COLUMNS = ["watchedAt", "title", "channel", "duration", "short", "videoId"]
NOTES = [
    "Identity google:default (the profile `webcli identity login google:default` signs in, through gemini). Commands "
    "run one at a time in that identity's daemon, a hidden headless Chrome: no window, no Dock icon, no focus change.",
    "When YouTube itself isn't signed in yet, webcli follows YouTube's own passive sign-in link once (Google's "
    "existing session decides; no page to fill in). Signed out after that exits 4.",
    "history --source auto (default) reads My Activity first (exact watch times, watchedAtPrecision \"exact\"), and "
    "falls back to YouTube's history (watchedAtPrecision \"day\": local midnight of the day header) when My "
    "Activity is unavailable or behind Google's \"verify it's you\" check; the reason goes to stderr.",
    "My Activity's \"verify it's you\" check needs a person and a window webcli never opens: turn it off at My "
    "Activity > \"Manage My Activity verification\", or use --source youtube. --source myactivity behind the check "
    "exits 4.",
    f"--limit defaults to {DEFAULT_LIMIT}; with --since there is no cap (it pages back to the cutoff). --since takes "
    "YYYY-MM-DD or ISO 8601 (local time when no offset).",
    "Rows: title, channel, channelUrl, videoId, url, watchedAt (ISO 8601), watchedAtPrecision (exact|day), "
    "durationSec (YouTube source only), isShort, source (myactivity|youtube-history). No history exits 3.",
]
WRITES = ["Nothing: no likes, subscriptions, comments, playlist edits, history pause or clear. The guard allows only "
          "youtubei's browse and account reads, and My Activity's own list paging."]

group = data.site_group(SITE, "YouTube watch history, read-only (unofficial). Identity google:default.")


def _since(ctx: click.Context, param: click.Parameter, value: str | None) -> datetime | None:
    try:
        return parse_since(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from None


def _clock(seconds: int | None) -> str:
    if seconds is None:
        return ""
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _cell(row: dict, col: str) -> str:
    if col == "watchedAt":
        at = row.get("watchedAt") or ""
        return at[:10] if row.get("watchedAtPrecision") == "day" else at[:16].replace("T", " ")
    if col == "duration":
        return _clock(row.get("durationSec"))
    if col == "short":
        return "short" if row.get("isShort") else ""
    if col == "channel":
        return truncate(row.get("channel"), 28)
    return str(row.get(col) or "")


@group.command("status")
@agent_output_options(rows=False)
def status_cmd(fmt, quiet) -> None:
    """Which account google:default is signed in to YouTube as (exit 4 if signed out)."""
    info = data.call(SITE, "status")
    data.emit_record("YouTube session", info, fmt)
    if not info["signedIn"]:
        die("Not signed in to YouTube.", EXIT_AUTH, hint=data.login_hint())
    raise SystemExit(EXIT_OK)


@group.command("history")
@click.option("--limit", default=None, type=click.IntRange(min=1),
              help=f"Max rows. Default {DEFAULT_LIMIT}, or no cap with --since (pages back to the cutoff).")
@click.option("--since", default=None, callback=_since,
              help="Only watches on/after YYYY-MM-DD or an ISO-8601 time (local time when no offset).")
@click.option("--source", type=click.Choice(["auto", "myactivity", "youtube"]), default="auto", show_default=True,
              help="auto tries My Activity (exact times), then falls back to YouTube history (day precision).")
@agent_output_options()
def history_cmd(limit, since, source, fmt, compact, select, quiet, as_csv) -> None:
    """Your watch history, newest first, one row per watch (READ-ONLY). Exit 3 when there is none."""
    if limit is None and since is None:
        limit = DEFAULT_LIMIT
    result = data.call(SITE, "history", source, limit, since.isoformat() if since else None)
    for line in result["notes"]:
        if not quiet:
            warn(line)
    rows = result["rows"]
    if not rows:
        die("No watch history found" + (f" since {since.isoformat()}." if since else "."), EXIT_NOT_FOUND)
    shown = select_fields(rows, select) if select else [{k: r.get(k) for k in (COMPACT if compact else FIELDS)}
                                                         for r in rows]
    out = "csv" if as_csv else resolve_format(fmt)
    if out == "csv":
        emit_csv(shown)
    elif out == "json":
        emit_json(shown)
    elif out == "md":
        emit_md(shown)
    else:
        rows_table("YouTube watch history", [{c: _cell(r, c) for c in COLUMNS} for r in rows], COLUMNS)
    note_showing(len(rows), quiet=quiet, noun="watches")
    raise SystemExit(EXIT_OK)


data.add_agent_help(group, notes=NOTES, writes=WRITES)
