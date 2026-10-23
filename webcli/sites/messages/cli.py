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
from webcli.sites.messages.endpoints import (
    CONFIRM_SECONDS,
    CONTACT_PAGES,
    CREATE_SECONDS,
    DEEP_PAGES,
    DETAILS_MAX,
    MAX_PAGES,
    SEND_SECONDS,
)

FIELDS = ["id", "name", "preview", "timestamp", "unread"]
COMPACT = ("id", "name", "timestamp", "unread")
MESSAGE_FIELDS = ["direction", "text", "timestamp"]
IMAGE_FIELDS = [*MESSAGE_FIELDS, "images"]
SEARCH_FIELDS = ["kind", "id", "name", "number", "numbers", "preview", "timestamp", "unread", "matched"]
SEARCH_COMPACT = ("kind", "id", "name", "number", "numbers", "matched")
OK_RULE = "A send needs Kevin's own OK for that exact message: never --confirm a text he hasn't approved word for word."
NOTES = [
    "Identity messages:default, its own pairing: Google Messages for web pairs with your phone by QR code, not a Google "
    "account. A person runs `webcli identity login messages:default` once (a visible Chrome window) and scans the code "
    "from the phone (Google Messages > Device pairing > QR code scanner). Commands run one at a time in that "
    "identity's daemon, a hidden headless Chrome: no window, no Dock icon, no focus change.",
    "`list` is the conversation list the web app has loaded, newest first: {id, name, preview (the latest message), "
    "timestamp (as the app shows it), unread}. `threads` is the same command: Google Messages has one conversation "
    "index.",
    "QUERY (`read`, `send --to`) names a conversation: its id, else a name (exact, then the one containing it; "
    "ignoring case), else a phone number (punctuation ignored; 10 digits are +1 US; compared as E.164) for a "
    "conversation shown by that number. The newest 25 first; past those, Messages' own Start chat contact search "
    "(the contact the name or number belongs to, and group conversations by member) and up to --pages "
    f"(default {DEEP_PAGES}; {CONTACT_PAGES} once the picker names the contact; max {MAX_PAGES}) paced \"Load more\" "
    "pages of 25, stopping once found. A number is typed into the picker as national digits, then +1..., then "
    "(xxx) xxx-xxxx, until a contact with it shows (Messages matches a contact by its number as saved). For a "
    "number, a contact's conversation must use that number: its Details say which. Several matches "
    "exit 2 and list the candidates (use the id); none exits 3. `send --to` is stricter: a name that fits two "
    "contacts exits 2 even when only one has a conversation.",
    "`read` prints the conversation's messages the app shows, oldest first: {direction: sent|received, text, "
    "timestamp}; an image is \"[image]\". The guard blocks the app's mark-as-read, so reading leaves it unread. "
    "stderr names the conversation and the number(s) it uses, read from its menu's Details dialog (opened and "
    "closed; nothing else in that menu is clicked); a contact with two numbers has a conversation per number. A "
    "number QUERY whose contact's conversation uses another of its numbers exits 3.",
    "`read --image-out DIR` also saves each image in those messages (after --limit) full size, as DIR/<conversation "
    "id>-<n>.<jpg|png|gif|webp> (n: the image's place among the conversation's images, so names don't depend on "
    "--limit; a file of that name is replaced), and every row gains images: its saved paths ([] for none). It opens "
    "the full-size viewer for each one, so it takes a few seconds per image. An image that doesn't load is left out "
    "and counted on stderr; the exit is still 0. Without --image-out the guard blocks the app's attachment download.",
    "`search QUERY [--pages N]` finds conversations past the newest: by name, number (E.164, punctuation and +1 "
    "aside), the latest message's preview (not older text), and Messages' own contact and group search. Rows: "
    "{kind: conversation|contact, id, name, number, numbers, preview, timestamp, unread, matched}: a conversation "
    "row's matched lists name/number/preview/group/contact; a contact row is a contact the query names (id: its 1:1 "
    "conversation on that number when listed, else null). numbers: the numbers a conversation uses, from its "
    f"Details (the first {DETAILS_MAX} conversations found are opened for them, read-only; null when not read; "
    "number then is a 1:1's one number); --no-numbers skips that. stderr says how many conversations were looked "
    "through. No match exits 3.",
    "One Messages web session at a time: every command holds run/messages.session.lock (all identities, daemon or "
    "not) while it has Messages open; a second waits up to --lock-wait, then exits 5 (error \"messages_busy\"). "
    "If Messages is open in another window or tab (\"Use here\"), the command exits 5 (error "
    "\"messages_open_elsewhere\") and never clicks \"Use here\"; close it there and retry.",
    "Not paired exits 4 (`status` too): a person runs `webcli identity login messages:default`.",
]
WRITES = [
    OK_RULE,
    "Only `send --to QUERY --text T --confirm` (alias --yes): one text to one existing conversation. Without --confirm "
    "it is a dry run: it resolves the conversation, prints {dryRun: true, to, threadId, text}, sends nothing, and "
    "exits 0. --dry-run wins over --confirm.",
    "New conversations only with --new-ok: `send --to NUMBER|CONTACT [--to ...] --text T --new-ok "
    "[--expect-recipient X ...] [--confirm]`. One --to that names an existing conversation sends there; otherwise "
    "Start chat opens the 1:1 (one --to) or group (several) conversation with exactly those recipients. Each --to is "
    "a number (E.164 after normalizing) or exactly one contact with exactly one number (else exit 2, listing the "
    "candidates; no contact exits 3). The dry run prints {dryRun: true, new: true, group, recipients: [{query, name, "
    "number}], expectRecipients, expectRecipientsMatch, text, staged, chips (a group's)}: it types only into the "
    "contact picker, never a message, and stages the recipients there as a confirmed send would (a group's in "
    "\"Start group conversation\" mode, its chips exactly the recipients) without the click that opens or creates "
    "the conversation; Start chat not offering that exits 5 \"REFUSED (dry run)\". "
    "--confirm needs --expect-recipient (alias --expect-recipients) once per recipient, each a name or number, "
    "matching the recipients exactly (else exit 2 before anything opens; a mismatch exits 5, \"REFUSED (dry run)\" "
    "without --confirm). Several --to without --new-ok exit 2; one with no conversation exits 3. Without --new-ok "
    "no conversation is ever created.",
    "A confirmed --new-ok send picks each recipient in Start chat by its exact number (in a group, the chips must then "
    f"be exactly the recipients), and only then opens the guard's one GET_OR_CREATE_CONVERSATION slot ({CREATE_SECONDS}"
    "s) for the click that opens the conversation; that conversation must then be exactly the recipients before "
    "anything is typed: its Details' numbers (when shown, they decide: another number refuses), else its title (a "
    "group's names, comma-joined). Then the send checks below; output adds new, group, "
    "recipients, created (whether Messages created or opened it), threadId.",
    "A confirmed send checks the page first: the open conversation is the resolved one (its id in the URL, and its "
    "title when the page shows one; when --to or an --expect-recipient is a number, its Details' numbers too, dry "
    "run included, as {numbers}: unreadable or another number exits 5), the compose box holds exactly the text, "
    "and the page has tried no send of its "
    f"own. Only then does the guard open its one slot, for {SEND_SECONDS}s after the click: one SEND_MESSAGE, "
    "nothing else. The guard can't read the conversation or text (Google Messages encrypts them end to end), so "
    "those page checks are what bind the send to them. A send is never retried.",
    f"Exit codes for a confirmed send: 0 when it went out and the conversation then showed the text ({{sent: true, "
    f"confirmed: true}}). 8 (error \"sent_unverified\") when it went out but the conversation didn't show the text "
    f"within {CONFIRM_SECONDS}s (whitespace aside: a line break reads back as one) ({{sent: true, confirmed: "
    "false} still on stdout): the text was sent, so check "
    "`webcli messages read <threadId>` and don't send it again. 5 when nothing went out: a page check refused "
    "(\"REFUSED: ... Nothing was sent.\") or the click sent nothing (\"... Nothing was sent.\"); then there is no "
    "stdout. titleChecked (whether the page showed a title to match before the click) doesn't change the exit code.",
    "Never: mark-as-read, typing indicators, reactions, replies, deletes, archive, new conversations without "
    "--new-ok, group names, settings, unpairing, \"Use here\".",
]

group = data.site_group(SITE, "Google Messages for web: your phone's conversations; send is a dry run unless --confirm "
                              "(unofficial). Identity messages:default. " + OK_RULE)


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
@click.option("--pages", type=click.IntRange(0, MAX_PAGES), default=None,
              help=f"Past the newest 25, load at most this many more pages of 25 to find QUERY (default {DEEP_PAGES}).")
@agent_output_options()
def read_cmd(query, limit, image_out, pages, **opts) -> None:
    """The messages in one conversation (QUERY: its id, a name, or a number), oldest first; with --image-out, its
    images saved too. READ-ONLY: it stays unread."""
    more = {} if pages is None else {"pages": pages}
    if image_out is None:
        found, fields = _call("messages", query, limit, **more), MESSAGE_FIELDS
    else:
        found, fields = _call("messages", query, limit, image_dir=data.out_dir(image_out), **more), IMAGE_FIELDS
    conversation = found["conversation"]
    numbers = conversation.get("numbers")
    uses = f", {', '.join(n for n in numbers if n) or 'no number shown'}" if numbers is not None else ""
    if numbers is not None and not opts["quiet"]:  # which number(s) it uses, in every mode (stdout is the messages)
        warn(f"Conversation: {conversation['name']} ({conversation['id']}{uses})")
    else:
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
@click.option("--limit", type=click.IntRange(min=1), default=None, help="Stop after this many rows.")
@click.option("--pages", type=click.IntRange(0, MAX_PAGES), default=None,
              help=f"Load at most this many more pages of 25 conversations (default {DEEP_PAGES}).")
@click.option("--no-numbers", "no_numbers", is_flag=True,
              help=f"Don't open the first {DETAILS_MAX} conversations found to read which numbers they use.")
@agent_output_options()
def search_cmd(query, limit, pages, no_numbers, **opts) -> None:
    """Conversations (and contacts) matching QUERY: a name, a number, or the latest message, past the newest too;
    each conversation's numbers. Exit 3 if none."""
    found = _call("search", query, pages, **({"numbers": False} if no_numbers else {}))
    rows = found["results"]
    note(f"Looked through {found['scanned']} conversations{' (all)' if found.get('complete') else ''} and Messages' "
         "contact search.", quiet=opts["quiet"])
    if not rows:
        die(f"No conversation or contact matches {query!r}.", EXIT_NOT_FOUND,
            hint="Only names, numbers, and each conversation's latest message are searched; raise --pages to look "
                 "further back.")
    emit_rows(rows[:limit] if limit else rows, title="Google Messages", fields=SEARCH_FIELDS,
              compact_fields=SEARCH_COMPACT, columns=SEARCH_FIELDS, noun="results", **opts)
    raise SystemExit(EXIT_OK)


@group.command("send")
@click.option("--to", "to", required=True, multiple=True, metavar="QUERY",
              help="The conversation (its id, a name, or a number); with --new-ok, a recipient (a number or a "
                   "contact): repeat for a group.")
@click.option("--text", required=True, help="The message.")
@click.option("--new-ok", "new_ok", is_flag=True,
              help="Allow starting a new 1:1 or group conversation with exactly the --to recipients.")
@click.option("--expect-recipient", "--expect-recipients", "expect", multiple=True, metavar="NAME|NUMBER",
              help="A recipient you expect (repeat for each); they must match exactly. Required with --new-ok "
                   "--confirm.")
@click.option("--pages", type=click.IntRange(0, MAX_PAGES), default=None,
              help=f"Past the newest 25, load at most this many more pages to find --to (default {DEEP_PAGES}).")
@click.option("--dry-run", is_flag=True, help="Preview only (the default). Wins over --confirm.")
@click.option("--confirm", "--yes", "confirm", is_flag=True, help="Actually send. Without it, nothing is sent.")
@agent_output_options(rows=False)
def send_cmd(to, text, new_ok, expect, pages, dry_run, confirm, fmt, quiet) -> None:
    """Send one text to one conversation (a new one only with --new-ok): a DRY RUN unless --confirm (and no
    --dry-run). A send needs Kevin's own OK for that exact message."""
    to = [item.strip() for item in to]
    if any(not item for item in to):
        die("--to is empty.", EXIT_USAGE)
    try:
        text = parse.message_text(text)
    except ValueError as exc:
        die(str(exc), EXIT_USAGE)
    confirm = confirm and not dry_run
    if len(to) > 1 and not new_ok:
        die("Several --to make a group conversation, and webcli starts conversations only with --new-ok.", EXIT_USAGE,
            error="new_conversation_not_allowed", hint="Add --new-ok (and --expect-recipient for each recipient).")
    if confirm and new_ok and not expect:
        die("A confirmed --new-ok send needs --expect-recipient for each recipient.", EXIT_USAGE,
            error="missing_expect", hint="e.g. --expect-recipient 'Alex Doe' --expect-recipient +14175550100")
    extra: dict[str, Any] = {}
    if new_ok:
        extra["new_ok"] = True
    if expect:
        extra["expect"] = [item.strip() for item in expect]
    if pages is not None:
        extra["pages"] = pages
    out = _call("send", to[0] if len(to) == 1 else to, text, confirm=confirm, **extra)
    if out["dryRun"]:
        note("DRY-RUN: nothing was sent. Re-run with --confirm (and without --dry-run) to send, only with Kevin's OK "
             "for this exact message.", quiet=quiet)
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
