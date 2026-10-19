"""`llmcli chatgpt`: agent-native access to chatgpt.com from your own signed-in, hidden Chrome (cgpt parity)."""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any

import click

from webcli.core import output
from webcli.core.output import (
    EXIT_API,
    EXIT_AUTH,
    EXIT_NOT_FOUND,
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
    truncate,
    warn,
)
from webcli.core.redact import redact_text
from webcli.sites import llm
from webcli.sites.chatgpt import ADAPTER as SITE
from webcli.sites.chatgpt import fetch, images
from webcli.sites.chatgpt.parse import clean_chat_id, error_reply, limit_reply, resets_at

CHALLENGE_HINT = ("A person runs `llmcli chatgpt login`, clicks \"Verify you are human\" in the Chrome window it "
                  "opens, waits until it says signed in, then retries.")
CHATS_CMD = "llmcli chatgpt chats list"
CHAT_FIELDS = ("id", "title", "updated", "pinned", "url")
CHAT_COMPACT = ("id", "title", "updated")
MODEL_FIELDS = ("slug", "title", "description", "default", "reasoning", "images")
MODEL_COMPACT = ("slug", "title", "default")
TEXT_TIMEOUT, IMAGE_TIMEOUT = 180, 600  # --timeout defaults: ChatGPT's images take minutes
# ChatGPT's upload limits (help.openai.com "File uploads FAQ"): 512 MB per file, 20 MB per image. How many files
# one message takes is left to ChatGPT (its refusal exits 5 before anything is sent).
MAX_FILE_BYTES = 512 * 2**20
MAX_IMAGE_BYTES = 20 * 2**20
WRITES = [
    "`ask` and `-p` send one prompt (plus any --file) to ChatGPT through the page's own composer, which starts or "
    "extends a chat in your history.",
    "Nothing else: no delete, archive, rename, share, settings, memory, custom instructions, or title generation. "
    "The request guard enforces it (`login` excepted: that window is plain Chrome for a person to sign in, and "
    "nothing is sent there). ChatGPT itself may still save a memory from a sent conversation when Memory is on in "
    "your settings, and it titles new chats on its own.",
]
AGENT_NOTES = [
    "Not OpenAI's API: llmcli chatgpt drives the chatgpt.com web app in your installed Google Chrome, on your "
    "ChatGPT plan.",
    "Identity chatgpt:default (profile ~/.webcli/identities/chatgpt-default/chrome-profile), held by that identity's "
    "daemon (started on demand): commands run one at a time in its Chrome and queue for up to --lock-wait.",
    ("Hidden browser: every command runs Chrome headless (no window, no Dock icon, no focus change). Only `login` "
     "(= `identity login`) and an explicit --headed show a window; there is no visible fallback."),
    "`login` opens a visible Chrome window and needs a person; never run it unattended.",
    ("Cloudflare: when headless Chrome gets Cloudflare's \"Verify you are human\" check, commands exit 4 with JSON "
     "error \"hidden_browser_blocked\". A person runs `llmcli chatgpt login` and clicks the check (llmcli never "
     "clicks it); --headed runs exit 4 with \"cloudflare_challenge\"."),
    "Chat ids are the UUID from chatgpt.com/c/<id> (the full URL works too).",
    "`ask -` reads the prompt from stdin; `ask` never prompts on a TTY.",
    "`llmcli chatgpt -p PROMPT` is print mode, like `claude -p`: only the answer on stdout, `chat: <id>` on stderr. "
    "`-c` sends into the chat of the identity's last finished ask (~/.webcli/sites/chatgpt/<identity>/last_chat). "
    "--prompt-file / --output-file (`-` = stdin/stdout) are the tmuxlet chat-backend contract.",
    "Errors are JSON on stderr ({error, code, hint}) when stderr is not a TTY or LLMCLI_FORMAT=json.",
    ("-m/--model takes a slug from `llmcli chatgpt models` (or a title or slug fragment that names one model). The "
     "answer and the model are read back from the chat's record: JSON requestedModel is the slug -m asked for (null "
     "without -m), servedModel the slug that wrote the reply (message metadata.model_slug), servedModelDisplay its "
     "title. ChatGPT's auto slugs route to their -instant/-thinking variants; that is not a fallback."),
    ("Fallback: when servedModel isn't the requested model, JSON has fallback true, fallbackFrom, fallbackTo, a "
     "warning goes to stderr, and the exit is still 0. --strict-model makes it exit 6 with error \"model_fallback\" "
     "(JSON output still prints first)."),
    ("A usage-cap refusal (HTTP 429, a stream error, a notice on the page, or a reply that is the cap notice) exits "
     "6: JSON error \"rate_limited\", code 6, message (ChatGPT's text), resets_at (ISO 8601, America/Chicago, or "
     "null)."),
    ("`--image-out DIR|PATH` (ask, -p) waits for the reply's images and saves each one as ChatGPT stores it (the "
     "file the record's image pointer names, not a preview). DIR (existing, ending in /, or without an image suffix; "
     "created if needed) gets <prompt-slug>-N.<ext> and never overwrites; PATH is written exactly, more images go to "
     "<stem>-2<suffix>, <stem>-3<suffix>. JSON images: the saved paths; imageFiles: [{path, width, height, bytes, "
     "mime, original, sourceWidth, sourceHeight, source}], source \"original\" when the saved file's size matches the record's, else "
     "\"resized\". A reply without an image exits 5 (JSON output still prints first)."),
    ("Every ask JSON has the uniform llm fields (answer, chatId, requestedModel, servedModel, servedModelDisplay, "
     "fallback, images, citations, canvas: always null here) plus chatUrl, imageCount, and attachments. An image "
     "reply without --image-out exits 0 with imageHint; nothing is saved."),
    ("--file PATH (ask, -p; repeatable) attaches files through the composer's own file input. Checked first (exit "
     "2): 512 MB per file, 20 MB per image. JSON attachments lists what the chat's record says was attached."),
    f"--timeout defaults to {TEXT_TIMEOUT}s, {IMAGE_TIMEOUT}s with --image-out.",
]

group = llm.site_group(SITE, "ChatGPT web CLI (unofficial; not OpenAI's API). Identity chatgpt:default.")
_write = llm.writer("ChatGPT", "llmcli chatgpt chats show", "chatgpt.com")


def _fail(exc: BaseException) -> None:
    output.fail(exc, site="ChatGPT", login=llm.login_hint(SITE), chats=CHATS_CMD, challenge=CHALLENGE_HINT)


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


def _ask(prompt: str, chat_id: str | None, *, image_out: Path | None = None, **kwargs) -> dict:
    """fetch.ask (saving the reply's images under image_out), then remember its chat for `-p -c`."""
    _need_session()
    result = _call("ask", prompt, chat_id=chat_id, want_images=image_out is not None, **kwargs)
    last = llm.last_chat_file(SITE)
    if result["chatId"]:
        _write(last, result["chatId"] + "\n", result["chatId"])
    else:
        last.unlink(missing_ok=True)  # so -c can't fall back to an older chat
    _save_images(result, image_out, prompt)
    if image_out is None and result["imageCount"]:
        where = result["chatUrl"] or "the chat at chatgpt.com"
        result["imageHint"] = (f"ChatGPT made {result['imageCount']} image(s) that were not saved; "
                               f"rerun with --image-out DIR|PATH (or open {where}).")
    return llm.uniform(result)


def _save_images(result: dict, out: Path | None, prompt: str) -> None:
    """Write fetch.ask's imageData under `out` and replace it with result["images"], the saved paths, and
    result["imageFiles"], what each saved file is (both always lists)."""
    data = result.pop("imageData", None) or []
    result["images"], result["imageFiles"] = [], []
    if out is None or not data:
        return
    try:
        result["imageFiles"] = images.save(out, prompt, data)
        result["images"] = [image["path"] for image in result["imageFiles"]]
    except OSError as exc:
        where = f"Open it at {result['chatUrl']}" if result["chatUrl"] else "Open the chat at chatgpt.com."
        die(f"ChatGPT made the image(s), but writing them to {out} failed: {exc}", EXIT_API,
            hint=f"Don't resend the prompt. {where}")
    for image in result["imageFiles"]:
        suffix = Path(image["path"]).suffix.lower()
        if image["mime"] != f"image/{images.SUFFIX_FORMAT.get(suffix)}":
            note(f"{image['path']} holds {image['mime']} data (what ChatGPT stored).", quiet=False)
        if not image["original"]:
            note(f"{image['path']} is {image['width']}x{image['height']}, not the "
                 f"{image['sourceWidth']}x{image['sourceHeight']} the chat record lists.", quiet=False)


def _files_arg(ctx: click.Context, param: click.Parameter, value: tuple[str, ...]) -> list[str]:
    """--file: ChatGPT's upload limits (MAX_*), checked before Chrome opens."""
    for name in value:
        size = Path(name).stat().st_size
        image = (mimetypes.guess_type(name)[0] or "").startswith("image/")
        if size > (MAX_IMAGE_BYTES if image else MAX_FILE_BYTES):
            limit = "20 MB per image" if image else "512 MB per file"
            raise click.BadParameter(f"{name} is {size / 2**20:,.1f} MB; ChatGPT takes up to {limit}.")
    return list(value)


def _timeout(timeout: int | None, image_out: Path | None) -> int:
    if timeout is not None:
        return timeout
    return IMAGE_TIMEOUT if image_out else TEXT_TIMEOUT


def _reply_text(result: dict) -> str:
    """Text output: the answer, then each saved image's path. An image reply that saved nothing says so."""
    lines = [result["answer"]] if result["answer"] else []
    lines += result["images"]
    if result["imageCount"] and not result["images"]:
        count = result["imageCount"]
        lines.append(f"[ChatGPT made {count} image{'s' if count > 1 else ''}: {result['chatUrl'] or 'see the chat'}. "
                     "Save images with --image-out DIR.]")
    return "\n".join(lines)


def _error_reply(result: dict, *, want_images: bool = False, strict_model: bool = False) -> bool:
    """Whether the reply is a usage-cap notice, ChatGPT's own error message, lacks the image that was asked for, or
    (strict_model) came from another model. If so, the JSON result gets the error fields too: error, code, message,
    resets_at for a cap; error, code, message for a fallback; just error otherwise."""
    if limit_reply(result["answer"]):
        result.update(error="rate_limited", code=EXIT_RATE_LIMITED, message=redact_text(result["answer"]),
                      resets_at=resets_at(result["answer"]))
    elif error_reply(result["answer"]):
        result["error"] = redact_text(f"ChatGPT replied with an error instead of an answer: "
                                      f"{truncate(result['answer'], 200)}")
    elif want_images and not result["imageCount"]:
        result["error"] = redact_text(f"ChatGPT answered without an image: "
                                      f"{truncate(result['answer'], 200) or '(empty)'}")
    elif strict_model and result.get("fallback"):
        result.update(error="model_fallback", code=EXIT_RATE_LIMITED,
                      message=f"Asked for {result['fallbackFrom']}, but ChatGPT answered with {result['fallbackTo']}.")
    return "error" in result


def _die_error_reply(result: dict) -> None:
    """Exit 6 after a cap notice or a strict-model fallback, else 5. Any JSON output already went out."""
    where = f" (chat {result['chatId']})" if result["chatId"] else ""
    if result.get("error") == "model_fallback":
        die(result["message"], EXIT_RATE_LIMITED, error="model_fallback", requestedModel=result["requestedModel"],
            servedModel=result["servedModel"],
            hint=f"The prompt reached ChatGPT{where}. The requested model is likely capped: wait, or drop --strict-model.")
    if result.get("code") == EXIT_RATE_LIMITED:
        output.die_rate_limited(result["message"], result["resets_at"], f"The prompt reached ChatGPT{where}.",
                                site="ChatGPT")
    die(result["error"], EXIT_API, hint=f"The prompt reached ChatGPT{where}. Retry it, or try another --model.")


def _finish(result: dict, *, want_images: bool, strict_model: bool) -> bool:
    if result.get("fallback"):
        warn(f"warning: asked for {result['fallbackFrom']}, but ChatGPT answered with {result['fallbackTo']} "
             "(fallback).")
    return _error_reply(result, want_images=want_images, strict_model=strict_model)


model_option = click.option(
    "-m", "--model", "model", metavar="SLUG", default=None,
    help="Use this model: a slug from `llmcli chatgpt models` (or a title / slug fragment naming one model).")
strict_model_option = click.option(
    "--strict-model", is_flag=True,
    help="Exit 6 (error model_fallback) when ChatGPT answers with another model than -m asked for.")
file_option = click.option(
    "--file", "files", multiple=True, type=click.Path(exists=True, dir_okay=False, resolve_path=True),
    callback=_files_arg,
    help="Attach a file (repeatable), e.g. an image to edit. ChatGPT takes up to 512 MB per file, 20 MB per image.")
image_out_option = click.option(
    "--image-out", metavar="DIR|PATH", default=None, callback=llm.image_out_arg(images.is_dir_target),
    help="Wait for the reply's images and save each at full size: into DIR as <prompt-slug>-N.<ext>, or to PATH "
         "(.png/.jpg/.jpeg/.webp; more images get -2, -3...). Exit 5 if no image.")
timeout_option = click.option(
    "--timeout", default=None, type=click.IntRange(min=1),
    help=f"Total seconds for setup, sending, the answer, and image downloads "
         f"(default {TEXT_TIMEOUT}; {IMAGE_TIMEOUT} with --image-out).")
headed_option = click.option("--headed", is_flag=True, help="Show the Chrome window (the one visible mode).")


@group.command("status")
@headed_option
@agent_output_options(rows=False)
def status_cmd(headed: bool, fmt: str | None, quiet: bool) -> None:
    """Whether the identity is signed in to ChatGPT, its plan, and its email (masked). Exit 4 if signed out."""
    info = _call("status", headed=headed) if llm.identity().profile_exists() else fetch.signed_out()
    info = {"identity": llm.identity().id, **info}
    out = resolve_format(fmt)
    if out == "json":
        emit_json(info)
    else:
        kv_table("ChatGPT session", info, out)
    if not info["signedIn"]:
        die("Not signed in to ChatGPT.", EXIT_AUTH, hint=llm.login_hint(SITE))
    raise SystemExit(EXIT_OK)


@group.command("ask")
@click.argument("prompt", callback=llm.prompt_arg)
@model_option
@strict_model_option
@click.option("--new", "new", is_flag=True, help="Start a new chat (the default).")
@click.option("--chat", "chat_id", metavar="ID", default=None, callback=_id_arg, help="Continue this chat.")
@file_option
@image_out_option
@timeout_option
@headed_option
@agent_output_options(formats=("text", "md", "json"), rows=False)
def ask_cmd(prompt, model, strict_model, new, chat_id, files, image_out, timeout, headed, fmt, quiet) -> None:
    """Send PROMPT (or `-` for stdin) and print ChatGPT's answer. The one command that writes."""
    if new and chat_id:
        die("Use --new or --chat, not both.", EXIT_USAGE)
    result = _ask(prompt, chat_id, image_out=image_out, model=model, files=files,
                  timeout=_timeout(timeout, image_out), headed=headed)
    failed = _finish(result, want_images=image_out is not None, strict_model=strict_model)
    if resolve_format(fmt, human="text") == "json":
        emit_json(result)
    elif not failed:  # ChatGPT's error message isn't printed as if it were the answer
        click.echo(_reply_text(result))
        note(f"Chat {result['chatId']} ({result['servedModel'] or 'default model'}). "
             f"Continue with --chat {result['chatId']}.", quiet=quiet)
    if failed:
        _die_error_reply(result)
    raise SystemExit(EXIT_OK)


@group.command("-p")
@click.argument("words", metavar="[PROMPT]...", nargs=-1)
@click.option("--chat", "chat_id", metavar="ID", default=None, callback=_id_arg,
              help="Send into this chat. Default: a new chat.")
@click.option("-c", "--continue", "resume", is_flag=True, help="Send into the chat of the last finished ask.")
@model_option
@strict_model_option
@file_option
@image_out_option
@timeout_option
@llm.prompt_file_option
@llm.output_file_option
@llm.output_format_option
@click.option("--quiet", "-q", is_flag=True, help="No `chat: <id>` line on stderr.")
def print_cmd(words, chat_id, resume, model, strict_model, files, image_out, timeout, prompt_file, output_file,
              output_format, quiet) -> None:
    """Print mode (or --print), like `claude -p`: send PROMPT (words, `-`/none for stdin) and print only the answer."""
    if chat_id and resume:
        die("Use --chat or -c, not both.", EXIT_USAGE)
    llm.check_output_file(output_file)
    prompt = llm.print_prompt(words, prompt_file)
    result = _ask(prompt, llm.read_last_chat(SITE, clean_chat_id) if resume else chat_id, image_out=image_out,
                  model=model, files=files, timeout=_timeout(timeout, image_out))
    failed = _finish(result, want_images=image_out is not None, strict_model=strict_model)
    if output_format == "json" or not failed:  # ChatGPT's error message isn't printed as if it were the answer
        out = (json.dumps(result, separators=(",", ":")) if output_format == "json" else _reply_text(result)) + "\n"
        llm.emit_answer(output_file, out, lambda path, text: _write(path, text, result["chatId"]))
    if failed:
        _die_error_reply(result)
    if result["chatId"] and not quiet:
        click.echo(f"chat: {result['chatId']}", err=True)
    raise SystemExit(EXIT_OK)


@group.group("chats", cls=click.Group)
def chats_group() -> None:
    """Your ChatGPT chat history (READ-ONLY)."""


@chats_group.command("list")
@click.option("--limit", default=50, show_default=True, type=click.IntRange(min=1), help="Max rows.")
@headed_option
@agent_output_options()
def chats_list_cmd(limit, headed, fmt, compact, select, as_csv, quiet) -> None:
    """Your chats, most recently updated first (chats inside projects are not listed)."""
    _need_session()
    rows = _call("chats", limit, headed=headed)
    if not rows:
        die("No chats found.", EXIT_NOT_FOUND)
    emit_rows(rows[:limit], title="ChatGPT chats", fields=CHAT_FIELDS, compact_fields=CHAT_COMPACT,
              columns=["id", "title", "updated", "pinned"], noun="chats", fmt=fmt, compact=compact,
              select=select, quiet=quiet, as_csv=as_csv)
    raise SystemExit(EXIT_OK)


@chats_group.command("show")
@click.argument("chat_id", metavar="ID", callback=_id_arg)
@headed_option
@agent_output_options(formats=("text", "md", "json"), rows=False)
def chats_show_cmd(chat_id, headed, fmt, quiet) -> None:
    """One chat's transcript (the branch the page shows), oldest message first (exit 3 if there's no such chat)."""
    _need_session()
    chat = _call("chat", chat_id, headed=headed)
    if resolve_format(fmt, human="text") == "json":
        emit_json(chat)
    else:
        for message in chat["messages"]:
            who = "You" if message["role"] == "user" else f"ChatGPT ({message['model']})" if message["model"] \
                else "ChatGPT"
            shown = "".join(f"\n[image {i['width']}x{i['height']}]" for i in message["images"])
            click.echo(f"## {who}\n\n{message['text']}{shown}\n")
    raise SystemExit(EXIT_OK)


@group.command("models")
@headed_option
@agent_output_options()
def models_cmd(headed, fmt, compact, select, as_csv, quiet) -> None:
    """ChatGPT's models (slugs for -m), with the default marked (READ-ONLY: nothing is picked)."""
    _need_session()
    rows = _call("models", headed=headed)
    if not rows:
        die("ChatGPT listed no models.", EXIT_NOT_FOUND, hint=fetch.PROBE_HINT)
    emit_rows(rows, title="ChatGPT models", fields=MODEL_FIELDS, compact_fields=MODEL_COMPACT,
              columns=["slug", "title", "default", "reasoning", "images"], noun="models", fmt=fmt, compact=compact,
              select=select, quiet=quiet, as_csv=as_csv)
    raise SystemExit(EXIT_OK)


llm.add_common_commands(group, SITE, notes=AGENT_NOTES, writes=WRITES)
