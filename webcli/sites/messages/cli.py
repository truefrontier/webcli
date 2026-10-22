"""`webcli messages`: your phone's conversations from Google Messages for web, in your own paired, hidden Chrome (gmsg
parity). Read-only except `send`: a dry run unless --confirm."""

from __future__ import annotations

from typing import Any

import click

from webcli.core.output import (
    EXIT_AUTH,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_SENT_UNVERIFIED,
    EXIT_USAGE,
    agent_output_options,
    die,
    emit_rows,
    note,
    warn,
)
from webcli.sites import data
from webcli.sites.messages import ADAPTER as SITE
from webcli.sites.messages import parse
from webcli.sites.messages.endpoints import CONFIRM_SECONDS, SEND_SECONDS

FIELDS = ["id", "name", "preview", "timestamp", "unread"]
COMPACT = ("id", "name", "timestamp", "unread")
MESSAGE_FIELDS = ["direction", "text", "timestamp"]
IMAGE_FIELDS = [*MESSAGE_FIELDS, "images"]
SEARCH_FIELDS = [*FIELDS, "matched"]
NOTES = [
    "Identity messages:default, its own pairing: Google Messages for web pairs with your phone by QR code, not a Google "
    "account. A person runs `webcli identity login messages:default` once (a visible Chrome window) and scans the code "
    "from the phone (Google Messages > Device pairing > QR code scanner). Commands run one at a time in that "
    "identity's daemon, a hidden headless Chrome: no window, no Dock icon, no focus change.",
    "`list` is the conversation list the web app has loaded, newest first: {id, name, preview (the latest message), "
    "timestamp (as the app shows it), unread}. `threads` is the same command: Google Messages has one conversation "
    "index.",
    "QUERY (`read`, `search`, `send --to`) names a conversation in that list: its id, else a name (exact, then the one "
    "containing it; ignoring case), else a phone number (7-15 digits, formatting aside) for a conversation shown by "
    "number. Several matches exit 2 (use the id); none exits 3. Nothing outside the list is opened, and no new chat "
    "is ever started.",
    "`read` prints the conversation's messages the app shows, oldest first: {direction: sent|received, text, "
    "timestamp}; an image is \"[image]\". The guard blocks the app's mark-as-read, so reading leaves it unread.",
    "`read --image-out DIR` also saves each image in those messages (after --limit) full size, as DIR/<conversation "
    "id>-<n>.<jpg|png|gif|webp> (n: the image's place among the conversation's images, so names don't depend on "
    "--limit; a file of that name is replaced), and every row gains images: its saved paths ([] for none). It opens "
    "the full-size viewer for each one, so it takes a few seconds per image. An image that doesn't load is left out "
    "and counted on stderr; the exit is still 0. Without --image-out the guard blocks the app's attachment download.",
    "`search` matches the name, the number, and the latest message's preview (not older message text); each row "
    "has matched: the fields that hit. No match exits 3.",
    "Google Messages keeps one active web session: every command takes it over (as gmsg did, it clicks \"Use "
    "here\"), so a Messages tab open elsewhere then asks to \"Use here\" again.",
    "Not paired exits 4 (`status` too): a person runs `webcli identity login messages:default`.",
]
WRITES = [
    "Only `send --to QUERY --text T --confirm` (alias --yes): one text to one existing conversation. Without --confirm "
    "it is a dry run: it resolves the conversation, prints {dryRun: true, to, threadId, text}, sends nothing, and "
    "exits 0. --dry-run wins over --confirm.",
    "A confirmed send checks the page first: the open conversation is the resolved one (its id in the URL, and its "
    "title when the page shows one), the compose box holds exactly the text, and the page has tried no send of its "
    f"own. Only then does the guard open its one slot, for {SEND_SECONDS}s after the click: one SEND_MESSAGE, "
    "nothing else. The guard can't read the conversation or text (Google Messages encrypts them end to end), so "
    "those page checks are what bind the send to them. A send is never retried.",
    f"Exit codes for a confirmed send: 0 when it went out and the conversation then showed the text ({{sent: true, "
    f"confirmed: true}}). 8 (error \"sent_unverified\") when it went out but the conversation didn't show the text "
    f"within {CONFIRM_SECONDS}s ({{sent: true, confirmed: false}} still on stdout): the text was sent, so check "
    "`webcli messages read <threadId>` and don't send it again. 5 when nothing went out: a page check refused "
    "(\"REFUSED: ... Nothing was sent.\") or the click sent nothing (\"... Nothing was sent.\"); then there is no "
    "stdout. titleChecked (whether the page showed a title to match before the click) doesn't change the exit code.",
    "Never: mark-as-read, typing indicators, reactions, replies, deletes, archive, new conversations, settings, "
    "unpairing.",
]

group = data.site_group(SITE, "Google Messages for web: your phone's conversations; send is a dry run unless --confirm "
                              "(unofficial). Identity messages:default.")


def _call(op: str, *args, **kwargs) -> Any:
    data.need_profile()
    return data.call(SITE, op, *args, **kwargs)


def _query_arg(ctx: click.Context, param: click.Parameter, value: str) -> str:
    if not value.strip():
        raise click.BadParameter("is empty")
    return value.strip()


@group.command("status")
@agent_output_options(rows=False)
def status_cmd(fmt, quiet) -> None:
    """Whether messages:default is paired with your phone (exit 4 if not)."""
    info = (data.call(SITE, "status") if data.identity().profile_exists()
            else {"signedIn": False, "paired": False, "status": "needs_pairing"})
    data.emit_record("Google Messages", info, fmt)
    if not info["signedIn"]:
        die("Google Messages isn't paired with your phone.", EXIT_AUTH, hint=data.login_hint())
    raise SystemExit(EXIT_OK)


@group.command("list")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="Stop after this many conversations.")
@agent_output_options()
def list_cmd(limit, **opts) -> None:
    """Recent conversations: name, latest message, time, unread."""
    emit_rows(_call("conversations", limit), title="Google Messages", fields=FIELDS, compact_fields=COMPACT,
              columns=FIELDS, noun="conversations", **opts)
    raise SystemExit(EXIT_OK)


group.add_command(list_cmd, "threads")


@group.command("read")
@click.argument("query", callback=_query_arg)
@click.option("--limit", type=click.IntRange(min=1), default=None, help="Only the last N messages.")
@click.option("--image-out", "image_out", metavar="DIR", default=None,
              help="Save each image in these messages full size into DIR (created); rows gain images: the paths.")
@agent_output_options()
def read_cmd(query, limit, image_out, **opts) -> None:
    """The messages in one conversation (QUERY: its id, a name, or a number), oldest first; with --image-out, its
    images saved too. READ-ONLY: it stays unread."""
    if image_out is None:
        found, fields = _call("messages", query, limit), MESSAGE_FIELDS
    else:
        found, fields = _call("messages", query, limit, image_dir=data.out_dir(image_out)), IMAGE_FIELDS
    conversation = found["conversation"]
    note(f"Conversation: {conversation['name']} ({conversation['id']})", quiet=opts["quiet"])
    if image_out is not None:
        note(f"Saved {sum(len(row['images']) for row in found['messages'])} image(s).", quiet=opts["quiet"])
        if missing := found.get("imagesMissing"):
            warn(f"{missing} image(s) didn't load, so they weren't saved (their rows have images: []).")
    emit_rows(found["messages"], title=conversation["name"], fields=fields, compact_fields=fields, columns=fields,
              noun="messages", **opts)
    raise SystemExit(EXIT_OK)


@group.command("search")
@click.argument("query", callback=_query_arg)
@click.option("--limit", type=click.IntRange(min=1), default=None, help="Stop after this many conversations.")
@agent_output_options()
def search_cmd(query, limit, **opts) -> None:
    """Conversations whose name, number, or latest message matches QUERY. Exit 3 if none."""
    rows = parse.search(_call("conversations", None), query)
    if not rows:
        die(f"No conversation matches {query!r}.", EXIT_NOT_FOUND,
            hint="Only the name, the number, and the latest message are searched; see `webcli messages list`.")
    emit_rows(rows[:limit] if limit else rows, title="Google Messages", fields=SEARCH_FIELDS, compact_fields=COMPACT,
              columns=SEARCH_FIELDS, noun="conversations", **opts)
    raise SystemExit(EXIT_OK)


@group.command("send")
@click.option("--to", "to", required=True, metavar="QUERY", callback=_query_arg,
              help="The conversation: its id, a name, or a number (an existing conversation only).")
@click.option("--text", required=True, help="The message.")
@click.option("--dry-run", is_flag=True, help="Preview only (the default). Wins over --confirm.")
@click.option("--confirm", "--yes", "confirm", is_flag=True, help="Actually send. Without it, nothing is sent.")
@agent_output_options(rows=False)
def send_cmd(to, text, dry_run, confirm, fmt, quiet) -> None:
    """Send one text to one existing conversation: a DRY RUN unless --confirm (and no --dry-run)."""
    try:
        text = parse.message_text(text)
    except ValueError as exc:
        die(str(exc), EXIT_USAGE)
    out = _call("send", to, text, confirm=confirm and not dry_run)
    if out["dryRun"]:
        note("DRY-RUN: nothing was sent. Re-run with --confirm (and without --dry-run) to send.", quiet=quiet)
    else:
        note("SENT", quiet=quiet)
    data.emit_record("Google Messages send", out, fmt)
    if not out["dryRun"] and not out["confirmed"]:
        die(f"The text was sent, but it couldn't be verified: conversation {out['threadId']} didn't show it within "
            f"{CONFIRM_SECONDS}s.", EXIT_SENT_UNVERIFIED, error="sent_unverified",
            hint=f"Check with `webcli messages read {out['threadId']}` instead of sending it again (a send is never "
                 "retried).")
    raise SystemExit(EXIT_OK)


data.add_agent_help(group, notes=NOTES, writes=WRITES)
