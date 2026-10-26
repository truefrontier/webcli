"""`webcli perplexity`: Perplexity Discover's news cards, read-only, signed in first (else public), from a hidden
Chrome. `login` opens the one visible window, for a person."""

from __future__ import annotations

import click

from webcli.core.output import (
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
from webcli.sites.perplexity import ADAPTER as SITE
from webcli.sites.perplexity.endpoints import DEFAULT_LIMIT, MAX_LIMIT, PAGE_PAUSE_MS, TOPICS

FIELDS = ("source", "id", "url", "title", "author", "publishedAt", "snippet", "thumbnail", "metrics",
          "perplexityUrl", "topic", "auth")
COMPACT = ("publishedAt", "title", "url", "perplexityUrl")
COLUMNS = ["published", "title", "site", "views"]
NOTES = [
    "Signed in first: identity perplexity:default (a person runs `webcli perplexity login` once: a visible Chrome "
    "window). When it isn't signed in (never, or expired), the same reads return the public Discover feed: every row "
    "says auth \"public\" (else \"signed_in\") and stderr says to log in. Commands run one at a time in the "
    "identity's daemon, a hidden Chrome: no window, no focus change.",
    "The guard passes only GETs to www.perplexity.ai /discover, /rest/discover/feed, /rest/discover/topics, and "
    "/api/auth/session. The page's scripts never run: no query, like, follow, bookmark, or analytics beacon is sent.",
    "--topic top (default), tech, science, finance, arts, sports. Perplexity has one \"Tech & Science\" topic, so "
    "science and tech read the same feed; finance is its \"Business\" topic. Each topic is resolved to Perplexity's "
    "topic id from its own topic list.",
    f"--limit defaults to {DEFAULT_LIMIT} (max {MAX_LIMIT}); the feed is read 20 cards at a time, "
    f"{PAGE_PAUSE_MS / 1000:g}s apart. Perplexity caches it for 15-30 minutes.",
    "Rows: source (perplexity), id, url (the first cited source article), title, author (the Perplexity curator), "
    "publishedAt (ISO 8601, UTC), snippet, thumbnail, metrics {views, likes, forks, sources}, perplexityUrl (the "
    "Discover page), topic, auth (signed_in|public). No cards exits 3.",
    "A challenge or block (Cloudflare, HTTP 403/429/503) exits 5: webcli stops and never works around it.",
]
WRITES = ["None. Read-only: GETs of Discover only. `login` opens a visible window for a person to sign in."]

group = data.site_group(SITE, "Perplexity Discover's news cards, read-only: signed in first, else the public feed "
                              "(unofficial). Identity perplexity:default.")
LOGIN_HINT = "a person runs: webcli perplexity login"


def _cell(row: dict, col: str) -> str:
    if col == "published":
        return (row.get("publishedAt") or "")[:16].replace("T", " ")
    if col == "title":
        return truncate(row.get("title"), 70)
    if col == "site":
        from urllib.parse import urlsplit

        return (urlsplit(row.get("url") or "").hostname or "").removeprefix("www.")
    if col == "views":
        views = (row.get("metrics") or {}).get("views")
        return "" if views is None else str(views)
    return str(row.get(col) or "")


@group.command("status")
@json_flag
@agent_output_options(rows=False)
def status_cmd(fmt, quiet) -> None:
    """Whether perplexity:default is signed in (auth signed_in|public) and Discover answers. Exit 0 either way (the
    public feed still works); 5 when Perplexity challenges or blocks."""
    info = data.call(SITE, "status")
    data.emit_record("Perplexity Discover", info, fmt)
    if not info["signedIn"] and not quiet:
        warn(f"perplexity:default isn't signed in: reads use the public feed. {LOGIN_HINT}")
    raise SystemExit(EXIT_OK)


@group.command("login")
@click.option("--timeout", default=600, show_default=True, type=click.IntRange(min=10), help="Seconds to wait.")
def login_cmd(timeout) -> None:
    """Open a VISIBLE Chrome window so a person can sign perplexity:default in (same as `webcli identity login
    perplexity:default`). Never run it unattended."""
    from webcli.apps.common import login_and_report

    login_and_report(data.identity(), timeout)


@group.command("discover")
@click.option("--topic", type=click.Choice(TOPICS), default="top", show_default=True,
              help="Discover feed: science reads Perplexity's \"Tech & Science\" (same as tech).")
@click.option("--limit", default=DEFAULT_LIMIT, show_default=True, type=click.IntRange(1, MAX_LIMIT),
              help="Max cards (read 20 at a time, paced).")
@json_flag
@agent_output_options()
def discover_cmd(topic, limit, fmt, compact, select, quiet, as_csv) -> None:
    """Perplexity Discover's cards for a topic, as Perplexity orders them (READ-ONLY). Exit 3 when there are none."""
    rows = data.call(SITE, "discover", topic, limit)
    if rows and rows[0].get("auth") == "public" and not quiet:
        warn(f"perplexity:default isn't signed in: this is the public feed (auth \"public\"). {LOGIN_HINT}")
    if not rows:
        die(f"Perplexity Discover showed no {topic} cards.", EXIT_NOT_FOUND)
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
        rows_table(f"Perplexity Discover: {topic}", [{c: _cell(r, c) for c in COLUMNS} for r in rows], COLUMNS)
    note_showing(len(rows), quiet=quiet, noun="cards")
    raise SystemExit(EXIT_OK)


data.add_agent_help(group, notes=NOTES, writes=WRITES)
