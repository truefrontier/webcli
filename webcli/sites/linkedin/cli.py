"""`webcli linkedin`: LinkedIn messages, activity, and Boost stats from your own signed-in, hidden Chrome (limsg
parity). Read-only except `messages send` and `post create`: dry runs unless --confirm, and refused (exit 4) unless
the signed-in account is the expected author."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import click

from webcli.core.output import (
    EXIT_AUTH,
    EXIT_OK,
    EXIT_USAGE,
    agent_output_options,
    compact_rows,
    die,
    emit_csv,
    emit_json,
    emit_rows,
    note,
    resolve_format,
    select_fields,
    warn,
    write_atomic,
)
from webcli.sites import data
from webcli.sites.linkedin import ADAPTER as SITE
from webcli.sites.linkedin import author as authors
from webcli.sites.linkedin import boost, parse
from webcli.sites.linkedin.endpoints import DEFAULT_EXPECT_AUTHOR, EXPECT_AUTHOR_ENV, MAX_TEXT

THREAD_FIELDS = ["id", "entityUrn", "peer", "preview", "time", "unread"]
THREAD_COMPACT = ("id", "peer", "time", "unread", "preview")
THREAD_COLUMNS = ["id", "peer", "preview", "time", "unread"]
MESSAGE_FIELDS = ["id", "time", "from", "text", "senderUrn"]
MESSAGE_COMPACT = ("time", "from", "text")
ACTIVITY_FIELDS = {"comments": ("id", "time", "type", "text", "targetUrl", "targetTitle"),
                   "reactions": ("id", "time", "type", "reaction", "targetUrl", "targetTitle"),
                   "posts": ("id", "time", "type", "text", "url")}
ACTIVITY_COMPACT = {"comments": ("id", "time", "type", "text"), "reactions": ("id", "time", "type", "reaction"),
                    "posts": ("id", "time", "type", "text")}
EXPORT_FIELDS = ("kind", "id", "time", "type", "text", "reaction", "targetUrl", "targetTitle", "url")
BOOST_FIELDS = ["contentUrn", "campaignId", "campaignName", "adAccountId", "campaignGroupId", "status", "statusDetail",
                "campaignStatus", "servingStatuses", "creativeStatus", "objective", "currency", "totalBudget",
                "dailyBudget", "spend", "spendUsd", "budgetRemaining", "spendPct", "impressions", "reach", "clicks",
                "landingPageClicks", "ctr", "ctrApi", "cpc", "costPerLandingPageClick", "cpm", "reactions", "comments",
                "reposts", "follows", "totalEngagements", "startAt", "endAt", "dateRange", "metricsSource", "warnings",
                "destinationUrl", "previewUrl", "fetchedAt"]
BOOST_COMPACT = ("contentUrn", "campaignId", "status", "spend", "totalBudget", "currency", "impressions", "clicks",
                 "landingPageClicks", "ctr", "cpc")
BOOST_COLUMNS = ["contentUrn", "status", "spend", "totalBudget", "impressions", "clicks", "landingPageClicks", "ctr",
                 "cpc", "startAt", "endAt"]
AUTHOR_HELP = (f"Write only if signed in as this handle (linkedin.com/in/HANDLE). Default: ${EXPECT_AUTHOR_ENV}, "
               f"then expect_author in config.toml [sites.linkedin], then {DEFAULT_EXPECT_AUTHOR}.")
NOTES = [
    "Identity linkedin:default, LinkedIn's own sign-in: a person runs `webcli identity login linkedin:default` once "
    "(a visible Chrome window). Commands run one at a time in that identity's daemon, a hidden headless Chrome: no "
    "window, no Dock icon, no focus change.",
    "THREAD_OR_PERSON is a thread id (2-...), an entityUrn, or a peer name substring (one match, else exit 3). "
    "`history`/`read` also take a raw thread id or urn outside the newest threads; `send` only a thread among the "
    "newest --limit.",
    "`messages list`/`history` open the messaging page (read-only: the guard aborts its mark-as-read and every other "
    "write) and use the queryIds that page uses, kept in ~/.webcli/sites/linkedin/query_ids.json. `send` reuses "
    "them; run `messages list` first if send can't find a thread.",
    "Times are ISO 8601 UTC. `history` is oldest first (the last --limit messages).",
    "`activity` reads your Recent activity pages (comments, reactions, all posts) as rendered, scrolling for more; "
    "--since keeps rows at or after an ISO time. `activity export` is {comments, reactions, posts}, or CSV with a "
    "kind column (--format is the output format there: json|csv).",
    "`boost` reads your personal Boost ad accounts from Campaign Manager (GET only). `boost stats TARGET` takes "
    "urn:li:share:..., urn:li:activity:..., a feed URL holding one, or a campaign id; one object, not a list. "
    "--since/--until are UTC dates (--until includes that day). Numbers are never invented: see limsg's null rules.",
    f"Writes check the author: the signed-in handle must equal the expected author ({DEFAULT_EXPECT_AUTHOR} unless "
    f"--expect-author, ${EXPECT_AUTHOR_ENV}, or config.toml [sites.linkedin] expect_author say otherwise), ignoring "
    "case. A dry run on a mismatch prints WARNING and the preview and exits 4; --confirm on a mismatch writes nothing "
    "and exits 4 (REFUSED). With --confirm the author is read again right before the last write; a change refuses.",
    "`messages send` and `post create` are dry runs unless --confirm (aliases --yes; `send` also --send); --dry-run "
    "wins over --confirm. A dry run reads (the thread, the author) and writes nothing.",
    f"`post create`: --text or --text-file (UTF-8; at most {MAX_TEXT} characters, counted like LinkedIn's composer) "
    "plus --image (one JPEG, PNG, or GIF by content, at most 10 MB). Without --confirm nothing is uploaded or posted.",
    "Not signed in exits 4: a person runs `webcli identity login linkedin:default`.",
]
WRITES = [
    "Only `messages send THREAD --text T --confirm` (one DM to one existing thread) and `post create ... --confirm` "
    "(one post: text plus one image). Both refuse (exit 4, nothing written) unless signed in as the expected author.",
    "The guard lets through exactly the confirmed write, bound to its thread, sender, and text (a post: its image's "
    "upload, then a post with its text and that image); everything else any page sends is blocked.",
    "Never: reactions, comments, connections, mark-as-read, Campaign Manager changes.",
    "Local only: `activity export --out` writes a file; ~/.webcli/sites/linkedin/query_ids.json keeps messaging "
    "queryIds.",
]

group = data.site_group(SITE, "LinkedIn: messages, activity, Boost stats; send and post are dry runs unless "
                              "--confirm, and only as the expected author (unofficial). Identity linkedin:default.")


def _call(op: str, *args, **kwargs) -> Any:
    data.need_profile()
    return data.call(SITE, op, *args, **kwargs)


def _since_arg(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    try:
        parse.parse_since(value, flag=f"--{param.name}")
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from None
    return value


def _expected(flag: str | None) -> str:
    try:
        return authors.expected(flag)
    except ValueError as exc:
        die(str(exc), EXIT_USAGE)


def _safety(out: dict, did: str, verb: str) -> None:
    """The author verdict on stderr: WARNING for a dry run, REFUSED for --confirm."""
    if out["authorMatches"]:
        return
    mismatch = authors.mismatch(out["author"], out["expectedAuthor"])
    if out["dryRun"]:
        warn(f"WARNING: AUTHOR MISMATCH. {mismatch} --confirm will refuse to {verb}.")
    else:
        warn(f"REFUSED: {mismatch} Nothing was {did}.")


@group.command("status")
@click.option("--expect-author", default=None, metavar="HANDLE", help=AUTHOR_HELP)
@agent_output_options(rows=False)
def status_cmd(expect_author, fmt, quiet) -> None:
    """The signed-in LinkedIn account, and whether writes would be allowed for it (exit 4 if signed out)."""
    try:  # status is a read: it reports sign-in even when no write-author is configured.
        expect = authors.expected(expect_author)
    except ValueError:
        expect = None
    info = data.call(SITE, "status") if data.identity().profile_exists() else {"signedIn": False}
    if info["signedIn"]:
        info = {**info, "expectedAuthor": expect,
                "authorMatches": authors.matches(info, expect) if expect is not None else False}
    data.emit_record("LinkedIn", info, fmt)
    if not info["signedIn"]:
        die("Not signed in to LinkedIn.", EXIT_AUTH, hint=data.login_hint())
    raise SystemExit(EXIT_OK)


# --- messages -----------------------------------------------------------------------------------------------------

@group.group("messages")
def messages_group() -> None:
    """List and read LinkedIn DMs; send one (dry run unless --confirm, only as the expected author)."""


@messages_group.command("list")
@click.option("--limit", default=20, show_default=True, type=click.IntRange(min=1), help="Max threads.")
@agent_output_options()
def messages_list(limit, **opts) -> None:
    """Recent DM threads: peer, preview, time, unread."""
    emit_rows(_call("conversations", limit), title="LinkedIn DMs", fields=THREAD_FIELDS, compact_fields=THREAD_COMPACT,
              columns=THREAD_COLUMNS, noun="threads", **opts)
    raise SystemExit(EXIT_OK)


@messages_group.command("history")
@click.argument("thread_or_person")
@click.option("--limit", default=20, show_default=True, type=click.IntRange(min=1), help="Max messages (the newest).")
@agent_output_options()
def messages_history(thread_or_person, limit, **opts) -> None:
    """The last N messages in a thread (who / text / time), oldest first. READ-ONLY."""
    emit_rows(_call("messages", thread_or_person, limit), title="LinkedIn messages", fields=MESSAGE_FIELDS,
              compact_fields=MESSAGE_COMPACT, columns=list(MESSAGE_COMPACT), noun="messages", **opts)
    raise SystemExit(EXIT_OK)


messages_group.add_command(messages_history, "read")


@messages_group.command("send")
@click.argument("thread_or_person")
@click.option("--text", required=True, help="Message body.")
@click.option("--limit", default=50, show_default=True, type=click.IntRange(min=1),
              help="Thread search window (the newest threads).")
@click.option("--expect-author", default=None, metavar="HANDLE", help=AUTHOR_HELP)
@click.option("--dry-run", is_flag=True, help="Preview only (the default). Wins over --confirm.")
@click.option("--confirm", "--yes", "--send", "confirm", is_flag=True,
              help="Actually send. Without it, nothing is sent.")
@click.option("--quiet", "-q", is_flag=True, help="Suppress non-data messages on stderr.")
def messages_send(thread_or_person, text, limit, expect_author, dry_run, confirm, quiet) -> None:
    """Send a DM to an existing thread. DRY RUN unless --confirm (and no --dry-run); refused (exit 4) unless signed
    in as the expected author."""
    try:
        text = parse.load_message(text)
    except ValueError as exc:
        die(str(exc), EXIT_USAGE)
    expect = _expected(expect_author)
    out = _call("send", thread_or_person, text, expect, confirm=confirm and not dry_run, search_limit=limit)
    if out["dryRun"]:
        note("DRY-RUN: nothing was sent. Re-run with --confirm (and without --dry-run) to send.", quiet=quiet)
    elif out.get("sent"):
        note("SENT", quiet=quiet)
    _safety(out, "sent", "send")
    emit_json(out)
    raise SystemExit(EXIT_OK if out["authorMatches"] else EXIT_AUTH)


# --- activity -----------------------------------------------------------------------------------------------------

@group.group("activity")
def activity_group() -> None:
    """Read-only recent activity: your comments, reactions, and posts."""


def _activity_command(kind: str, summary: str) -> None:
    @activity_group.command(kind, help=summary)
    @click.option("--limit", default=20, show_default=True, type=click.IntRange(min=1), help="Max rows.")
    @click.option("--since", default=None, callback=_since_arg, help="Only rows at or after this ISO-8601 time.")
    @agent_output_options()
    def command(limit, since, **opts) -> None:
        fields = ACTIVITY_FIELDS[kind]
        emit_rows(_call("activity", kind, limit, since), title=f"LinkedIn {kind}", fields=list(fields),
                  compact_fields=ACTIVITY_COMPACT[kind], columns=list(fields), noun=kind, **opts)
        raise SystemExit(EXIT_OK)


_activity_command("comments", "Your recent comments (READ-ONLY).")
_activity_command("reactions", "Your recent reactions (READ-ONLY).")
_activity_command("posts", "Your recent posts and shares (READ-ONLY).")


@activity_group.command("export")
@click.option("--limit", default=20, show_default=True, type=click.IntRange(min=1), help="Max rows per kind.")
@click.option("--since", default=None, callback=_since_arg, help="Only rows at or after this ISO-8601 time.")
@click.option("--out", "out_path", default=None, type=click.Path(dir_okay=False, path_type=Path),
              help="Write to PATH instead of stdout (mode 0600).")
@click.option("--format", "fmt", type=click.Choice(["json", "csv"]), default="json", show_default=True)
@click.option("--compact", is_flag=True, help="High-gravity fields only.")
@click.option("--select", default=None, help="Comma-separated fields.")
@click.option("--quiet", "-q", is_flag=True, help="Suppress non-data messages on stderr.")
@click.option("--csv", "as_csv", is_flag=True, help="CSV (same as --format csv).")
def activity_export(limit, since, out_path, fmt, compact, select, quiet, as_csv) -> None:
    """Comments, reactions, and posts together: a JSON object, or CSV with a kind column. READ-ONLY."""
    bundle = _call("export", limit, since)
    if compact:
        bundle = {kind: compact_rows(rows, ACTIVITY_COMPACT[kind]) for kind, rows in bundle.items()}
    if select:
        bundle = {kind: select_fields(rows, select) for kind, rows in bundle.items()}
    if as_csv or fmt == "csv":
        buffer = io.StringIO()
        fields = list(EXPORT_FIELDS) if not select else ["kind", *[k.strip() for k in select.split(",") if k.strip()]]
        writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for kind, rows in bundle.items():
            writer.writerows({"kind": kind, **row} for row in rows)
        text = buffer.getvalue()
    else:
        text = json.dumps(bundle, indent=2, default=str) + "\n"
    if out_path is None:
        click.echo(text, nl=False)
        raise SystemExit(EXIT_OK)
    write_atomic(out_path.expanduser().resolve(), text)
    note(f"Wrote {out_path}", quiet=quiet)
    raise SystemExit(EXIT_OK)


# --- boost --------------------------------------------------------------------------------------------------------

@group.group("boost")
def boost_group() -> None:
    """Read-only LinkedIn Boost (sponsored post) stats from Campaign Manager."""


def _window(since: str | None, until: str | None) -> None:
    since_dt, until_dt = parse.parse_since(since), parse.parse_since(until, flag="--until")
    if since_dt and until_dt and since_dt.date() > until_dt.date():
        die("--since is after --until.", EXIT_USAGE)


def _target_arg(ctx: click.Context, param: click.Parameter, value: str) -> str:
    try:
        boost.parse_target(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from None
    return value


def _cells(rows: list[dict]) -> list[dict]:
    """Lists and objects as JSON text, for CSV."""
    return [{k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in row.items()} for row in rows]


def _window_options(f):
    f = click.option("--until", default=None, callback=_since_arg,
                     help="Analytics end, ISO date or datetime (inclusive day, UTC).")(f)
    return click.option("--since", default=None, callback=_since_arg,
                        help="Analytics start, ISO date or datetime (UTC).")(f)


@boost_group.command("list")
@_window_options
@click.option("--account", default=None, type=int, help="Only this Boost ad account id.")
@agent_output_options()
def boost_list(since, until, account, fmt, compact, select, quiet, as_csv) -> None:
    """Every boosted campaign across your personal Boost ad accounts, newest first (READ-ONLY)."""
    _window(since, until)
    rows = _call("boosts", since, until, account)
    emit_rows(_cells(rows) if as_csv or fmt == "csv" else rows, title="LinkedIn Boosts", fields=BOOST_FIELDS,
              compact_fields=BOOST_COMPACT, columns=BOOST_COLUMNS, noun="boosts", fmt=fmt, compact=compact,
              select=select, quiet=quiet, as_csv=as_csv)
    raise SystemExit(EXIT_OK)


@boost_group.command("stats")
@click.argument("target", callback=_target_arg)
@_window_options
@agent_output_options()
def boost_stats(target, since, until, fmt, compact, select, quiet, as_csv) -> None:
    """Stats for one boost: urn:li:share:..., urn:li:activity:..., or a campaign id (READ-ONLY). Exit 3 if none."""
    _window(since, until)
    row = _call("boost_stats", target, since, until)
    payload = select_fields(compact_rows([row], BOOST_COMPACT)[0] if compact else row, select)
    out = "csv" if as_csv else resolve_format(fmt)
    if out == "csv":
        emit_csv(_cells([payload]))
    else:
        data.emit_record("LinkedIn Boost stats", payload, fmt)
    raise SystemExit(EXIT_OK)


# --- post ---------------------------------------------------------------------------------------------------------

@group.group("post")
def post_group() -> None:
    """Publish a LinkedIn post (text + one image): dry run unless --confirm, only as the expected author."""


def _emit_post(out: dict, fmt: str | None, quiet: bool) -> None:
    if out["dryRun"]:
        note("DRY-RUN: nothing was posted. Re-run with --confirm (and without --dry-run) to publish.", quiet=quiet)
    elif out.get("posted"):
        note("POSTED", quiet=quiet)
    _safety(out, "uploaded or posted", "post")
    if out.get("warning"):
        warn(f"WARNING: {out['warning']}")
    if resolve_format(fmt, human="text") == "json":
        emit_json(out)
        return
    signed, image = out["author"], out["image"]
    dims = f"  {image['width']}x{image['height']}" if image.get("width") else ""
    lines = [f"URN:    {out['urn'] or '(unknown)'}", f"URL:    {out['url'] or '(unknown)'}"] if out.get("posted") else []
    ids = "  ".join(u for u in (signed["profileUrn"], signed["memberUrn"]) if u)
    lines += [
        f"Author: {signed['name']} ({signed['publicIdentifier']})  {ids}".rstrip(),
        f"Expect: {out['expectedAuthor']}  {'MATCH' if out['authorMatches'] else 'MISMATCH'}",
        f"Image:  {image['path']}  {image['type']}  {image['bytes']:,} bytes{dims}",
        f"Text ({out['textLength']} chars):",
        out["text"],
    ]
    click.echo("\n".join(lines))


@post_group.command("create")
@click.option("--text", default=None, help=f"Post text (max {MAX_TEXT} characters).")
@click.option("--text-file", default=None, type=click.Path(dir_okay=False), help="Read the post text from a UTF-8 file.")
@click.option("--image", required=True, type=click.Path(dir_okay=False), help="One JPEG, PNG, or GIF, max 10 MB.")
@click.option("--expect-author", default=None, metavar="HANDLE", help=AUTHOR_HELP)
@click.option("--dry-run", is_flag=True, help="Preview only (the default). Wins over --confirm.")
@click.option("--confirm", "--yes", "confirm", is_flag=True,
              help="Actually publish. Without it, nothing is uploaded or posted.")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default=None,
              help="Default: text on a terminal, json when piped.")
@click.option("--quiet", "-q", is_flag=True, help="Suppress non-data messages on stderr.")
def post_create(text, text_file, image, expect_author, dry_run, confirm, fmt, quiet) -> None:
    """Post text + one image to your feed. DRY RUN unless --confirm (and no --dry-run); refused (exit 4) unless
    signed in as the expected author."""
    try:
        body = parse.load_text(text, text_file)
        path = parse.load_image(image)["path"]
    except ValueError as exc:
        die(str(exc), EXIT_USAGE)
    expect = _expected(expect_author)
    out = _call("post", body, path, expect, confirm=confirm and not dry_run)
    _emit_post(out, fmt, quiet)
    raise SystemExit(EXIT_OK if out["authorMatches"] else EXIT_AUTH)


data.add_agent_help(group, notes=NOTES, writes=WRITES)
