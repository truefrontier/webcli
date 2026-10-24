"""`llmcli notebooklm`: NotebookLM's chat as an llmcli site, the same ask / -p shape as gemini, chatgpt, and muse,
answering from one notebook's sources with citations.

It reuses `webcli notebooklm`'s fetch code (one site, one daemon op table): only its `status`, `notebooks`, and
`ask` ops, listed in OPS and checked before every call. `ask` runs under the site's ASK guard policy (the read
rpcids plus the one chat endpoint), so nothing here can reach CreateArtifact or any notebook, source, or note edit.
Studio outputs (`generate`, `download`) stay under `webcli notebooklm`.
"""

from __future__ import annotations

import json
from typing import Any

import click

from webcli.core import output
from webcli.core.output import (
    EXIT_AUTH,
    EXIT_OK,
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
from webcli.sites import llm
from webcli.sites.notebooklm import ADAPTER as SITE
from webcli.sites.notebooklm import parse

OPS = frozenset({"status", "notebooks", "ask"})  # the only fetch ops llmcli notebooklm may run
NOTEBOOK_ENV = "LLMCLI_NOTEBOOKLM_NOTEBOOK"
NOTEBOOKS_CMD = "llmcli notebooklm notebooks"
NO_MODEL_PICKER = "NotebookLM has no model picker; it answers from the notebook's sources."
TIMEOUT = 180
WRITES = [
    "`ask` and `-p` send one question to one notebook's chat; it joins that notebook's chat history, as if typed in "
    "the app (the notebook's current conversation, or --chat ID).",
    "Nothing else: no Studio outputs (generate / download stay under `webcli notebooklm`), no notebook, source, or "
    "note edits, no sharing or settings. `ask` runs under the ASK guard policy (read rpcids plus the chat endpoint, "
    "fail-closed); every other command under READ.",
]
AGENT_NOTES = [
    "NotebookLM (notebooklm.google.com) as a chat site: the answer comes from one notebook's sources, with citations. "
    "The same site as `webcli notebooklm` (reads, Studio outputs); this is its ask surface only.",
    "Identity google:default (the profile `webcli identity login google:default` / `llmcli gemini login` signs in). "
    "Held by that identity's daemon: commands run one at a time in its hidden headless Chrome and queue for up to "
    "--lock-wait.",
    f"-n/--notebook NOTEBOOK picks the notebook: an id, a notebooklm.google.com/notebook/<id> URL, or its exact "
    f"title (case-insensitive if nothing matches exactly; two notebooks with that title exit 2 naming both ids). "
    f"Env {NOTEBOOK_ENV} is the default. `notebooks` lists them. `-p -c` without -n reuses the last ask's notebook.",
    "--source SOURCE (repeatable) asks only those sources (ids from `webcli notebooklm sources NOTEBOOK`); default "
    "every source.",
    "Conversations: by default a question continues the notebook's current conversation, as in the app. Every answer "
    "carries its conversation id as chatId; --chat ID sends into that conversation, `-p -c` into the last finished "
    "ask's (~/.webcli/sites/notebooklm/<identity>/last_chat).",
    "Every ask JSON has the uniform llm fields (answer, chatId = the conversation id, requestedModel / servedModel / "
    "servedModelDisplay: null, fallback: false, images: [], canvas: null; citations: [{number, title, url, "
    "source_id, source_title, excerpt}], title being the cited source's title and url null) plus notebook_id, "
    "notebook (its title), question, conversation_id.",
    "`ask -` reads the prompt from stdin; `ask` never prompts on a TTY.",
    "`llmcli notebooklm -p -n NOTEBOOK PROMPT` is print mode, like `claude -p`: only the answer on stdout, `chat: "
    "<id>` on stderr. --prompt-file / --output-file (`-` = stdin/stdout) are the tmuxlet chat-backend contract.",
    "No model choice: `models` prints [] (no browser opens) and `ask --model` exits 2.",
    "No such notebook or source exits 3; a usage limit exits 6; not signed in exits 4 (a person runs `llmcli "
    "notebooklm login`). Errors are JSON on stderr when stderr is not a TTY or LLMCLI_FORMAT=json.",
    f"--timeout (default {TIMEOUT}s) bounds the wait for the answer.",
]

group = llm.site_group(SITE, "NotebookLM chat (unofficial): ask one notebook, answers with citations. Identity "
                             "google:default. Studio outputs live under `webcli notebooklm`.")
_write = llm.writer("NotebookLM", "llmcli notebooklm ask --chat", "notebooklm.google.com")


def _call(op: str, *args, **kwargs) -> Any:
    if op not in OPS:  # structural: llmcli notebooklm never runs generate / download / anything else
        raise RuntimeError(f"llmcli notebooklm does not run {op!r}.")
    try:
        return llm.call(SITE, op, *args, headed=False, **kwargs)
    except SystemExit:
        raise
    except Exception as exc:
        output.fail(exc, site="NotebookLM", login=llm.login_hint(SITE), chats=NOTEBOOKS_CMD)


def _need_session() -> None:
    if not llm.identity().profile_exists():
        die(f"No session yet for {llm.identity().id}.", EXIT_AUTH, hint=llm.login_hint(SITE))


def _notebook_arg(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return parse.clean_id(value)


def _id_arg(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    return parse.clean_id(value) if value else None


def uniform(got: dict) -> dict:
    """fetch.ask's result as the uniform llm ask JSON (its own fields kept)."""
    cites = [{"number": c.get("number"), "title": c.get("source_title"), "url": None, **c}
             for c in got.get("citations") or []]
    return llm.uniform({**got, "citations": cites, "chatId": got.get("conversation_id")})


def _read_last() -> tuple[str, str | None]:
    """(notebook id, conversation id) of the identity's last finished ask."""
    try:
        data = json.loads(llm.last_chat_file(SITE).read_text())
        return data["notebook_id"], data.get("conversation_id")
    except (OSError, ValueError, KeyError, TypeError):
        die("No chat to continue: no ask has finished yet.", EXIT_USAGE, hint="Drop -c, or pass -n and --chat ID.")


def _ask(notebook: str | None, prompt: str, sources: tuple[str, ...], chat_id: str | None, timeout: int) -> dict:
    """fetch.ask, then remember its notebook and conversation for `-p -c`."""
    if not notebook:
        die("Which notebook? Pass -n NOTEBOOK (an id, URL, or exact title).", EXIT_USAGE,
            hint=f"List them: {NOTEBOOKS_CMD}. Or set {NOTEBOOK_ENV}.")
    _need_session()
    got = _call("ask", notebook, prompt, [parse.clean_id(s) for s in sources] or None, conversation_id=chat_id,
                timeout=timeout)
    result = uniform(got)
    last = llm.last_chat_file(SITE)
    _write(last, json.dumps({"notebook_id": result["notebook_id"], "conversation_id": result["chatId"]}) + "\n",
           result["chatId"])
    return result


notebook_option = click.option("-n", "--notebook", metavar="NOTEBOOK", envvar=NOTEBOOK_ENV, callback=_notebook_arg,
                               help=f"The notebook: id, URL, or exact title (env {NOTEBOOK_ENV}).")
source_option = click.option("--source", "sources", multiple=True, metavar="SOURCE",
                             help="Ask only this source (repeatable; default every source).")
chat_option = click.option("--chat", "chat_id", metavar="ID", default=None, callback=_id_arg,
                           help="Send into this conversation (an earlier answer's chatId). Default: the notebook's "
                                "current conversation.")
timeout_option = click.option("--timeout", default=TIMEOUT, show_default=True, type=click.IntRange(min=10),
                              help="Seconds to wait for the answer.")


@group.command("status")
@agent_output_options(rows=False)
def status_cmd(fmt: str | None, quiet: bool) -> None:
    """The NotebookLM sign-in check. Exit 4 if signed out."""
    if llm.identity().profile_exists():
        info = _call("status")
    else:
        info = {"signedIn": False, "email": None, "api": None}
    info = {"identity": llm.identity().id, **info}
    if resolve_format(fmt) == "json":
        emit_json(info)
    else:
        kv_table("NotebookLM session", info, resolve_format(fmt))
    if not info["signedIn"]:
        die("Not signed in to NotebookLM.", EXIT_AUTH, hint=llm.login_hint(SITE))
    raise SystemExit(EXIT_OK)


@group.command("notebooks")
@agent_output_options()
def notebooks_cmd(**opts) -> None:
    """Your notebooks (what -n takes): id, title, sources (count), viewed_at. Read-only."""
    _need_session()
    rows = _call("notebooks")
    emit_rows(rows, title="Notebooks", fields=["id", "title", "emoji", "sources", "created_at", "viewed_at", "url"],
              compact_fields=("id", "title", "sources"), columns=["id", "title", "sources", "viewed_at"],
              noun="notebooks", **opts)
    raise SystemExit(EXIT_OK)


@group.command("ask")
@click.argument("prompt", callback=llm.prompt_arg)
@notebook_option
@source_option
@chat_option
@click.option("--model", "model", metavar="NAME", default=None, hidden=True,
              help="Not supported: NotebookLM has no model picker (exit 2).")
@timeout_option
@click.option("--json", "as_json", is_flag=True, help="JSON output (same as --format json).")
@agent_output_options(formats=("text", "md", "json"), rows=False)
def ask_cmd(prompt, notebook, sources, chat_id, model, timeout, as_json, fmt, quiet) -> None:
    """Ask NOTEBOOK (-n) PROMPT (or `-` for stdin): the answer and its citations. The one command that writes."""
    if model is not None:
        die(f"--model isn't supported: {NO_MODEL_PICKER} Drop --model.", EXIT_USAGE)
    result = _ask(notebook, prompt, sources, chat_id, timeout)
    if as_json or resolve_format(fmt, human="text") == "json":
        emit_json(result)
    else:
        click.echo(result["answer"])
        if result["citations"]:
            click.echo("\nCitations:")
            for cite in result["citations"]:
                click.echo(f"  [{cite['number']}] {cite['title'] or cite['source_id']}")
        if result["chatId"]:
            note(f"Conversation {result['chatId']}. Continue with --chat {result['chatId']}.", quiet=quiet)
    raise SystemExit(EXIT_OK)


@group.command("-p")
@click.argument("words", metavar="[PROMPT]...", nargs=-1)
@notebook_option
@source_option
@chat_option
@click.option("-c", "--continue", "resume", is_flag=True,
              help="Send into the last finished ask's conversation (and notebook, without -n).")
@timeout_option
@llm.prompt_file_option
@llm.output_file_option
@llm.output_format_option
@click.option("--quiet", "-q", is_flag=True, help="No `chat: <id>` line on stderr.")
def print_cmd(words, notebook, sources, chat_id, resume, timeout, prompt_file, output_file, output_format,
              quiet) -> None:
    """Print mode (or --print), like `claude -p`: ask -n NOTEBOOK PROMPT (words, `-`/none for stdin), print only the
    answer."""
    if chat_id and resume:
        die("Use --chat or -c, not both.", EXIT_USAGE)
    llm.check_output_file(output_file)
    prompt = llm.print_prompt(words, prompt_file)
    if resume:
        last_notebook, chat_id = _read_last()
        if notebook and notebook != last_notebook:
            die(f"-c continues notebook {last_notebook}'s conversation, not {notebook}.", EXIT_USAGE,
                hint="Drop -n, or drop -c to ask this notebook.")
        notebook = notebook or last_notebook
    result = _ask(notebook, prompt, sources, chat_id, timeout)
    out = (json.dumps(result, separators=(",", ":")) if output_format == "json" else result["answer"]) + "\n"
    llm.emit_answer(output_file, out, lambda path, text: _write(path, text, result["chatId"]))
    if result["chatId"] and not quiet:
        click.echo(f"chat: {result['chatId']}", err=True)
    raise SystemExit(EXIT_OK)


@group.command("models")
@agent_output_options()
def models_cmd(fmt, compact, select, as_csv, quiet) -> None:
    """Always an empty list: NotebookLM has no model picker (no browser opens)."""
    if not as_csv and resolve_format(fmt) == "json":
        emit_json([])
    if not quiet:
        warn(NO_MODEL_PICKER)
    raise SystemExit(EXIT_OK)


llm.add_common_commands(group, SITE, notes=AGENT_NOTES, writes=WRITES)
