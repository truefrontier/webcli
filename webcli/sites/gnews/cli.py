"""`webcli gnews`: Google News "For you", read-only, from your own signed-in, hidden Chrome (google:default)."""

from __future__ import annotations

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
    json_flag,
    note_showing,
    resolve_format,
    rows_table,
    select_fields,
    truncate,
    warn,
)
from webcli.sites import data
from webcli.sites.gnews import ADAPTER as SITE
from webcli.sites.gnews.endpoints import DEFAULT_LIMIT, MAX_LIMIT, SECTIONS

FIELDS = ("source", "id", "url", "title", "author", "publisher", "publishedAt", "snippet", "thumbnail", "metrics",
          "newsUrl", "section", "auth")
COMPACT = ("publishedAt", "publisher", "title", "url")
COLUMNS = ["published", "publisher", "title"]
NOTES = [
    "Signed in first: identity google:default (signed in through gemini's login; webcli never signs in). When it is "
    "signed out or expired, For you falls back to the public Google News home and every row says auth \"public\" "
    "(else \"signed_in\"); stderr says to log in. Commands run one at a time in that identity's daemon, a hidden "
    "headless Chrome: no window, no focus change.",
    "Read off the rendered page with GETs only: the guard aborts every batchexecute RPC and all logging, so no "
    "follow, hide, \"fewer stories like this\", or save is ever sent.",
    f"--section foryou (default) or one of the page's own sections: {', '.join(s for s in SECTIONS if s != 'foryou')}"
    ". --limit defaults to " + f"{DEFAULT_LIMIT} (max {MAX_LIMIT}: what one page renders; webcli never scrolls).",
    "Rows: source (google_news), id (Google's story token), url and newsUrl (the news.google.com/read link; the "
    "publisher's own URL would need one more Google RPC, so it isn't resolved), title, author, publisher, "
    "publishedAt (ISO 8601, UTC), snippet (null: the page shows none), thumbnail, metrics ({}), section, auth. "
    "No stories exits 3.",
    "A challenge or block (Google's \"unusual traffic\" page, HTTP 403/429) exits 5: webcli stops and never works "
    "around it.",
]
WRITES = ["None. Read-only: GETs of news.google.com only."]

group = data.site_group(SITE, "Google News \"For you\", read-only (unofficial). Identity google:default.")


def _cell(row: dict, col: str) -> str:
    if col == "published":
        return (row.get("publishedAt") or "")[:16].replace("T", " ")
    if col == "publisher":
        return truncate(row.get("publisher"), 24)
    if col == "title":
        return truncate(row.get("title"), 80)
    return str(row.get(col) or "")


@group.command("status")
@json_flag
@agent_output_options(rows=False)
def status_cmd(fmt, quiet) -> None:
    """Whether google:default is signed in to Google News (exit 4 if not)."""
    info = data.call(SITE, "status")
    data.emit_record("Google News session", info, fmt)
    if not info["signedIn"]:
        die("Not signed in to Google News.", EXIT_AUTH, hint=data.login_hint())
    raise SystemExit(EXIT_OK)


@group.command("foryou")
@click.option("--section", type=click.Choice(list(SECTIONS)), default="foryou", show_default=True,
              help="For you, or one of the page's sections.")
@click.option("--limit", default=DEFAULT_LIMIT, show_default=True, type=click.IntRange(1, MAX_LIMIT),
              help="Max stories (what one page renders).")
@json_flag
@agent_output_options()
def foryou_cmd(section, limit, fmt, compact, select, quiet, as_csv) -> None:
    """Your Google News "For you" stories in page order (READ-ONLY). Exit 3 when there are none."""
    rows = data.call(SITE, "foryou", section, limit)
    if rows and rows[0].get("auth") == "public" and not quiet:
        warn(f"google:default isn't signed in: this is the public Google News feed (auth \"public\"). "
             f"{data.login_hint()}")
    if not rows:
        die(f"Google News showed no stories in {section}.", EXIT_NOT_FOUND)
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
        rows_table(f"Google News: {section}", [{c: _cell(r, c) for c in COLUMNS} for r in rows], COLUMNS)
    note_showing(len(rows), quiet=quiet, noun="stories")
    raise SystemExit(EXIT_OK)


data.add_agent_help(group, notes=NOTES, writes=WRITES)
