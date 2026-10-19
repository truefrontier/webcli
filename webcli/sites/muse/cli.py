"""`llmcli muse`: agent-native access to Meta's Muse (muse.ai) from your own signed-in, hidden Chrome (musecli
parity)."""

from __future__ import annotations

import json
from typing import Any

import click

from webcli.core import output
from webcli.core.output import (
    EXIT_AUTH,
    EXIT_OK,
    EXIT_RATE_LIMITED,
    EXIT_USAGE,
    agent_output_options,
    die,
    emit_json,
    emit_rows,
    kv_table,
    note,
    resolve_format,
    warn,
)
from webcli.core.redact import redact_text
from webcli.sites import llm
from webcli.sites.muse import ADAPTER as SITE
from webcli.sites.muse import fetch
from webcli.sites.muse.parse import clean_chat_id, limit_reply, resets_at

CHATS_CMD = "llmcli muse chats list"
NO_MODEL_PICKER = "muse.ai has no model picker; Muse chooses its model."
CHAT_FIELDS = ("id", "title", "updated", "url")
CHAT_COMPACT = ("id", "title")
TIMEOUT = 300
WRITES = [
    "`ask` and `-p` send one prompt to Muse: a new chat by default, or --chat ID. Muse is an agent, so a prompt can "
    "make it act on its own (email, forms, bookings). llmcli muse never clicks an approval.",
    "Opening a chat may mark it seen, as viewing it in a browser does: `chats show`, `ask --chat`, and `chats list` "
    "(it opens each side chat it lists to read its id).",
    "Nothing else: no delete, rename, pin, archive, share, publish, or settings. HTTP requests are guarded "
    "(fail-closed); the encrypted VM socket (wss://*.metaaivm.com) can't be filtered inside, so read commands type "
    "and click nothing that writes.",
]
AGENT_NOTES = [
    "Meta's Muse agent at muse.ai; not the old muse.ai video host (that is Skiv now).",
    "Identity meta:default (profile ~/.webcli/identities/meta-default/chrome-profile): Muse signs in with a Meta "
    "account (Facebook, Instagram, email, or phone). Held by that identity's daemon (started on demand): commands run "
    "one at a time in its Chrome and queue for up to --lock-wait.",
    ("Hidden browser: every command runs Chrome headless (no window, no Dock icon, no focus change). Only `login` "
     "(= `identity login meta:default`) and an explicit --headed show a window; there is no visible fallback."),
    "`login` opens a visible Chrome window and needs a person; never run it unattended.",
    "Chat streams over an end-to-end encrypted WebSocket to the account's cloud VM (wss://*.metaaivm.com): the one "
    "socket host the guard allows, and only from the running command's tab.",
    "Selectors were checked against a signed-in muse.ai on 2026-10-15 (see webcli/sites/muse/selectors.py).",
    "Chat ids are the <id> in muse.ai/thread/<id> (the full URL also works); `main` is the main chat.",
    "No model choice: muse.ai has no model picker. `models` prints [] (no browser opens) and `ask --model` exits 2.",
    ("Every ask JSON has the uniform llm fields (answer, chatId, requestedModel, servedModel, servedModelDisplay: "
     "always null; fallback: always false; images: always []; citations: the answer's outside links as {title, url}; "
     "canvas: always null) plus chatUrl and prompt."),
    "`ask -` reads the prompt from stdin; `ask` never prompts on a TTY.",
    "`llmcli muse -p PROMPT` is print mode, like `claude -p`: only the answer on stdout, `chat: <id>` on stderr. "
    "`-c` sends into the chat of the identity's last finished ask (~/.webcli/sites/muse/<identity>/last_chat). "
    "--prompt-file / --output-file (`-` = stdin/stdout) are the tmuxlet chat-backend contract.",
    "Errors are JSON on stderr ({error, code, hint}) when stderr is not a TTY or LLMCLI_FORMAT=json.",
    ("A reply that is only a usage-cap notice (short, and opening with \"You've reached your ... limit\", \"Your ... "
     "limit resets ...\", \"Too many requests\"...) exits 6: JSON error \"rate_limited\", code 6, message (Muse's "
     "text), resets_at (ISO 8601, America/Chicago, or null). JSON output still prints first."),
    "If Muse asks for approval, `ask` exits 5 and names the chat; a timeout exits 5 with the chat to read later.",
    f"--timeout (default {TIMEOUT}s) bounds the wait for the answer.",
]

group = llm.site_group(SITE, "Meta Muse (muse.ai) chat CLI (unofficial). Identity meta:default (a Meta account).")
_write = llm.writer("Muse", "llmcli muse chats show", "muse.ai")


def _fail(exc: BaseException) -> None:
    output.fail(exc, site="Muse", login=llm.login_hint(SITE), chats=CHATS_CMD)


def _call(op: str, *args, headed: bool = False, **kwargs) -> Any:
    try:
        return llm.call(SITE, op, *args, headed=headed, **kwargs)
    except SystemExit:
        raise
    except Exception as exc:
        _fail(exc)


def _id_arg(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return clean_chat_id(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from None


def _need_session() -> None:
    if not llm.identity().profile_exists():
        die(f"No session yet for {llm.identity().id}.", EXIT_AUTH, hint=llm.login_hint(SITE))


def _ask(prompt: str, chat_id: str | None, **kwargs) -> dict:
    """fetch.ask, then remember its chat for `-p -c`."""
    _need_session()
    result = _call("ask", prompt, chat_id=chat_id, **kwargs)
    last = llm.last_chat_file(SITE)
    if result["chatId"]:
        _write(last, result["chatId"] + "\n", result["chatId"])
    else:
        last.unlink(missing_ok=True)  # so -c can't fall back to an older chat
    return llm.uniform(result)


def _limit_reply(result: dict) -> bool:
    """Whether the reply is a usage-cap notice. If so, the JSON result gets error, code, message, resets_at too."""
    if not limit_reply(result["answer"]):
        return False
    result.update(error="rate_limited", code=EXIT_RATE_LIMITED, message=redact_text(result["answer"]),
                  resets_at=resets_at(result["answer"]))
    return True


def _die_limit(result: dict) -> None:
    """Exit 6 after a cap notice. Any JSON output already went out."""
    where = f" (chat {result['chatId']})" if result["chatId"] else ""
    output.die_rate_limited(result["message"], result["resets_at"], f"The prompt reached Muse{where}.", site="Muse")


timeout_option = click.option("--timeout", default=TIMEOUT, show_default=True, type=click.IntRange(min=10),
                              help="Seconds to wait for the answer.")
headed_option = click.option("--headed", is_flag=True, help="Show the Chrome window (the one visible mode).")


@group.command("status")
@headed_option
@agent_output_options(rows=False)
def status_cmd(headed: bool, fmt: str | None, quiet: bool) -> None:
    """The signed-in Muse account and its access state. Exit 4 if signed out or held (waitlist, invite...)."""
    info = _call("status", headed=headed) if llm.identity().profile_exists() else fetch.signed_out()
    info = {"identity": llm.identity().id, **info}
    if resolve_format(fmt) == "json":
        emit_json(info)
    else:
        kv_table("Muse session", info, resolve_format(fmt))
    if not info["signedIn"]:
        die("Not signed in to Muse.", EXIT_AUTH, hint=llm.login_hint(SITE))
    if info["access"] != "ok":
        die(f"Signed in, but Muse is holding this account on its {info['access']} page.", EXIT_AUTH,
            hint=llm.login_hint(SITE))
    raise SystemExit(EXIT_OK)


@group.command("ask")
@click.argument("prompt", callback=llm.prompt_arg)
@click.option("--new", "new", is_flag=True, help="Start a new chat (the default).")
@click.option("--chat", "chat_id", metavar="ID", default=None, callback=_id_arg,
              help="Continue this chat (`main` for the main chat).")
@click.option("--model", "model", metavar="NAME", default=None, hidden=True,
              help="Not supported: muse.ai has no model picker (exit 2).")
@timeout_option
@headed_option
@agent_output_options(formats=("text", "md", "json"), rows=False)
def ask_cmd(prompt, new, chat_id, model, timeout, headed, fmt, quiet) -> None:
    """Send PROMPT (or `-` for stdin) to Muse and print its answer. The one command that writes."""
    if new and chat_id:
        die("Use --new or --chat, not both.", EXIT_USAGE)
    if model is not None:
        die(f"--model isn't supported: {NO_MODEL_PICKER} Drop --model.", EXIT_USAGE)
    result = _ask(prompt, chat_id, timeout=timeout, headed=headed)
    capped = _limit_reply(result)
    if resolve_format(fmt, human="text") == "json":
        emit_json(result)
    elif not capped:  # Muse's cap notice isn't printed as if it were the answer
        click.echo(result["answer"])
        if result["chatId"]:
            note(f"Chat {result['chatId']}. Continue with --chat {result['chatId']}.", quiet=quiet)
    if capped:
        _die_limit(result)
    raise SystemExit(EXIT_OK)


@group.command("-p")
@click.argument("words", metavar="[PROMPT]...", nargs=-1)
@click.option("--chat", "chat_id", metavar="ID", default=None, callback=_id_arg,
              help="Send into this chat (`main` for the main chat). Default: a new chat.")
@click.option("-c", "--continue", "resume", is_flag=True, help="Send into the chat of the last finished ask.")
@timeout_option
@llm.prompt_file_option
@llm.output_file_option
@llm.output_format_option
@click.option("--quiet", "-q", is_flag=True, help="No `chat: <id>` line on stderr.")
def print_cmd(words, chat_id, resume, timeout, prompt_file, output_file, output_format, quiet) -> None:
    """Print mode (or --print), like `claude -p`: send PROMPT (words, `-`/none for stdin) and print only the answer."""
    if chat_id and resume:
        die("Use --chat or -c, not both.", EXIT_USAGE)
    llm.check_output_file(output_file)
    prompt = llm.print_prompt(words, prompt_file)
    result = _ask(prompt, llm.read_last_chat(SITE, clean_chat_id) if resume else chat_id, timeout=timeout)
    capped = _limit_reply(result)
    if output_format == "json" or not capped:  # Muse's cap notice isn't printed as if it were the answer
        out = (json.dumps(result, separators=(",", ":")) if output_format == "json" else result["answer"]) + "\n"
        llm.emit_answer(output_file, out, lambda path, text: _write(path, text, result["chatId"]))
    if capped:
        _die_limit(result)
    if result["chatId"] and not quiet:
        click.echo(f"chat: {result['chatId']}", err=True)
    raise SystemExit(EXIT_OK)


@group.group("chats", cls=click.Group)
def chats_group() -> None:
    """Your Muse chats (READ-ONLY)."""


@chats_group.command("list")
@click.option("--limit", default=10, show_default=True, type=click.IntRange(min=1),
              help="Max rows, Main chat included. Each side chat is opened to read its id, so keep it small.")
@headed_option
@agent_output_options()
def chats_list_cmd(limit, headed, fmt, compact, select, as_csv, quiet) -> None:
    """Your chats as muse.ai's sidebar lists them: Main chat, then side chats (newest first)."""
    _need_session()
    rows = _call("chats", limit, headed=headed)
    emit_rows(rows, title="Muse chats", fields=CHAT_FIELDS, compact_fields=CHAT_COMPACT,
              columns=["id", "title", "updated"], noun="chats", fmt=fmt, compact=compact, select=select,
              quiet=quiet, as_csv=as_csv)
    raise SystemExit(EXIT_OK)


@chats_group.command("show")
@click.argument("chat_id", metavar="ID", callback=_id_arg)
@headed_option
@agent_output_options(formats=("text", "md", "json"), rows=False)
def chats_show_cmd(chat_id, headed, fmt, quiet) -> None:
    """One chat's messages, oldest first (exit 3 if there's no such chat)."""
    _need_session()
    chat = _call("chat", chat_id, headed=headed)
    if resolve_format(fmt, human="text") == "json":
        emit_json(chat)
    else:
        for message in chat["messages"]:
            click.echo(f"## {'You' if message['role'] == 'user' else 'Muse'}\n\n{message['text']}\n")
    raise SystemExit(EXIT_OK)


@group.command("models")
@agent_output_options()
def models_cmd(fmt, compact, select, as_csv, quiet) -> None:
    """Always an empty list: muse.ai has no model picker, Muse chooses its model (no browser opens)."""
    if not as_csv and resolve_format(fmt) == "json":
        emit_json([])
    if not quiet:
        warn(NO_MODEL_PICKER)
    raise SystemExit(EXIT_OK)


llm.add_common_commands(group, SITE, notes=AGENT_NOTES, writes=WRITES)
