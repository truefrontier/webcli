"""`llmcli gemini`: agent-native access to gemini.google.com from your own signed-in, hidden Chrome (gweb parity)."""

from __future__ import annotations

import json
import mimetypes
import re
import zipfile
from pathlib import Path
from typing import Any

import click

from webcli.core import output
from webcli.core.errors import RateLimited
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
)
from webcli.core.redact import redact_text
from webcli.sites import llm
from webcli.sites.gemini import ADAPTER as SITE
from webcli.sites.gemini import fetch, images
from webcli.sites.gemini.parse import clean_chat_id, error_reply, limit_reply, resets_at

CHATS_CMD = "llmcli gemini chats list"
CHAT_FIELDS = ("id", "title", "updated", "pinned", "url")
CHAT_COMPACT = ("id", "title", "updated")
MODEL_FIELDS = ("name", "description", "current")
MODEL_COMPACT = ("name", "current")
TEXT_TIMEOUT, IMAGE_TIMEOUT, CANVAS_TIMEOUT = 150, 300, 300  # --timeout defaults: images and docs take minutes
IMAGE_MODEL = re.compile(r"argon|nano.?banana|imagen|\bimage", re.IGNORECASE)  # "4 Argon" is Nano Banana
# Gemini web's upload limits, from https://support.google.com/gemini/answer/14903178 ("Up to 10 files ... in the
# same prompt", "Each video can be up to 2 GB", "All other supported file types can be up to 100 MB", a ZIP "Can
# contain up to 10 files" and "Can't include video or audio files"). Checked live 2026-10-18: an 11th file gets
# "you can only upload 10 attachments at a time", and "File larger than 100 MB" starts at 100 MiB + 1 byte. The page
# itself accepts any extension and doesn't check video size before uploading.
MAX_FILES = 10
MAX_FILE_BYTES = 100 * 2**20
MAX_VIDEO_BYTES = 2 * 2**30
MAX_ZIP_FILES = 10
WRITES = [
    "`ask` and `-p` send one prompt (plus any --file) to Gemini, which starts or extends a chat in your history. "
    "With --image-out they also turn on the composer's Create image tool for that prompt; with --canvas or "
    "--canvas-out, its Canvas tool.",
    "Nothing else: no delete, rename, pin, share, settings, Gems, or extensions. The request guard enforces it.",
]
AGENT_NOTES = [
    "Not Google's official `gemini` CLI: llmcli gemini drives the gemini.google.com web app in Chrome.",
    "Identity google:default (profile ~/.webcli/identities/google-default/chrome-profile), held by that identity's "
    "daemon (started on demand): commands run one at a time in its Chrome and queue for up to --lock-wait.",
    ("Hidden browser: every command runs Chrome headless (no window, no Dock icon, no focus change). Only `login` "
     "(= `identity login`) and an explicit --headed show a window; there is no visible fallback."),
    "`login` opens a visible Chrome window and needs a person; never run it unattended.",
    "Chat ids are the id from gemini.google.com/app/<id> (a c_ prefix or the full URL also works).",
    "`ask` reads the answer as the page renders it (Gemini's WebChannel answer is protobuf), so markdown "
    "marks such as ** or code fences may be missing; `chats show ID` returns the stored markdown. A cited answer "
    "is the stored markdown already (see the citations note).",
    "`ask -` reads the prompt from stdin; `ask` never prompts on a TTY.",
    "`llmcli gemini -p PROMPT` is print mode, like `claude -p`: only the answer on stdout, `chat: <id>` on stderr. "
    "`-c` sends into the chat of the identity's last finished ask (~/.webcli/sites/gemini/<identity>/last_chat). "
    "--prompt-file / --output-file (`-` = stdin/stdout) are the tmuxlet chat-backend contract.",
    "Errors are JSON on stderr ({error, code, hint}) when stderr is not a TTY or LLMCLI_FORMAT=json.",
    ("`--thinking low|medium|high` (or l/m/h) picks Gemini's Thinking level after --model. Without it the level "
     "is left as it is and the JSON `thinking` field is null."),
    ("Models: JSON requestedModel is --model as given (null without it); servedModel and servedModelDisplay are the "
     "model the picker shows once chosen (also `model`); Gemini reports no fallback, so fallback is false."),
    ("When Gemini's whole answer is its own error message (\"Something went wrong…\", "
     "\"I encountered an error…\"), `ask` and `-p` exit 5 with it on stderr. JSON output still prints first, with "
     "an `error` field; text output prints nothing."),
    ("A usage-limit notice (\"You've reached your limit…\", \"Your limit resets at 1:53 PM\") as the answer, or in a "
     "toast, dialog, or banner before or after sending, exits 6. The JSON error (and an answer's JSON output) has "
     "error \"rate_limited\", code 6, message (Gemini's text), and resets_at (ISO 8601, America/Chicago, or null). "
     "Don't retry before resets_at."),
    ("`--image-out DIR|PATH` (ask, -p) turns on Gemini's Create image tool, waits for the image reply, and saves each "
     "image at full size, the way Gemini's \"Download full size image\" button gets it (the c8o8Fe full_size_image "
     "rpc): PNG, or JPEG/WebP when PATH ends .jpg/.jpeg/.webp. DIR (existing, ending in /, or without an image "
     "suffix; created if needed) gets <prompt-slug>-N.<ext> and never overwrites; PATH is written exactly, more "
     "images go to <stem>-2<suffix>, <stem>-3<suffix>. Text output prints the saved paths after the answer. JSON "
     "images: the saved paths; imageFiles: [{path, width, height, bytes, mime, source_url_kind}] (\"full_size\", or "
     "\"preview\" when only the smaller preview could be fetched). A reply without an image exits 5 (JSON output still prints first)."),
    ("Every ask JSON has the uniform llm fields (answer, chatId, requestedModel, servedModel, servedModelDisplay, "
     "fallback, images, citations, canvas) plus chatUrl, model, thinking, and imageCount. An image reply "
     "without --image-out still finishes once its images render and exits 0: text output prints "
     "\"[Gemini made N image(s) …]\" so it isn't empty, and JSON adds imageHint. Nothing is saved without "
     "--image-out."),
    (f"--file PATH (ask, -p; repeatable) attaches files through Gemini's upload menu. Gemini's limits are checked "
     f"first (exit 2): at most {MAX_FILES} files per prompt, 100 MB per file (Gemini counts 100 MiB), 2 GB per video, "
     f"and a .zip holds at most {MAX_ZIP_FILES} files and no audio or video. Video and audio length limits (5 and "
     "10 minutes without a Google AI plan) are left to Gemini."),
    ("`--canvas` (ask, -p) turns on Gemini's Canvas tool and reads the reply's Canvas doc from the chat transcript "
     "(its stored markdown). JSON canvas {title, markdown, chars, path} (null without --canvas); answer is then "
     "the reply's text without the doc. `--canvas-out PATH` (implies --canvas) also writes the markdown to PATH "
     "atomically, replacing PATH; missing parent directories are created. Text output prints the answer, then the "
     "saved path, or the markdown when nothing was saved. A reply without a Canvas doc exits 5 (JSON output still "
     "prints first). Not with --image-out."),
    ("A cited answer (Gemini's source chips: attached files, or web pages) has footnotes: [^n] after each cited "
     "sentence and a block at the end, \"[^1]: notes.md\" or \"[^2]: Page Title, https://...\". The same "
     "source keeps its number; numbers follow first appearance. JSON citations lists them: [{n, label, file}] or "
     "[{n, label, title, url}] ([] when none; label is the chip's text, e.g. MD or Wikipedia). The sources come from "
     "the chat's transcript; when it can't be read, from the chips themselves (web title/url null). --no-footnotes "
     "(ask, -p) drops the marks and the block; citations stays. `chats show` footnotes cited messages too."),
    (f"--timeout defaults to {TEXT_TIMEOUT}s, {IMAGE_TIMEOUT}s with --image-out or an image --model "
     f"(Argon, Nano Banana, Imagen), or {CANVAS_TIMEOUT}s with --canvas."),
]

group = llm.site_group(SITE, "Gemini web CLI (unofficial; not Google's `gemini` CLI). Identity google:default.")
_write = llm.writer("Gemini", "llmcli gemini chats show", "gemini.google.com")


def _call(op: str, *args, headed: bool = False, **kwargs) -> Any:
    try:
        return llm.call(SITE, op, *args, headed=headed, **kwargs)
    except SystemExit:
        raise
    except Exception as exc:
        if isinstance(exc, RateLimited) and exc.resets_at is None:
            exc.resets_at = resets_at(exc.message)  # a notice raised without one: read it from Gemini's text
        output.fail(exc, site="Gemini", login=llm.login_hint(SITE), chats=CHATS_CMD)


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


def _ask(prompt: str, chat_id: str | None, *, image_out: Path | None = None, canvas_out: Path | None = None,
         model: str | None = None, **kwargs) -> dict:
    """fetch.ask (saving the reply's images under image_out and its Canvas doc to canvas_out), then remember its
    chat for `-p -c`."""
    _need_session()
    fmt = images.wanted_format(image_out) if image_out else None
    result = _call("ask", prompt, chat_id=chat_id, model=model, image_format=fmt, **kwargs)
    last = llm.last_chat_file(SITE)
    if result["chatId"]:
        _write(last, result["chatId"] + "\n", result["chatId"])
    else:
        last.unlink(missing_ok=True)  # so -c can't fall back to an older chat
    _save_images(result, image_out, prompt)
    _save_canvas(result, canvas_out)
    if image_out is None and result["imageCount"]:
        where = result["chatUrl"] or "the chat at gemini.google.com"
        result["imageHint"] = (f"Gemini made {result['imageCount']} image(s) that were not saved; "
                               f"rerun with --image-out DIR|PATH (or open {where}).")
    result.update(requestedModel=model, servedModel=result.get("model"), servedModelDisplay=result.get("model"),
                  fallback=False)
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
        where = f"Open it at {result['chatUrl']}" if result["chatUrl"] else "Open the chat at gemini.google.com."
        die(f"Gemini made the image(s), but writing them to {out} failed: {exc}", EXIT_API,
            hint=f"Don't resend the prompt. {where}")
    for image in result["imageFiles"]:
        suffix = Path(image["path"]).suffix.lower()
        if image["mime"] != f"image/{images.SUFFIX_FORMAT.get(suffix)}":
            note(f"{image['path']} holds {image['mime']} data (what Gemini sent).", quiet=False)
        if image["source_url_kind"] == "preview":
            note(f"{image['path']} is Gemini's smaller preview: the full-size link failed.", quiet=False)


def _save_canvas(result: dict, out: Path | None) -> None:
    """fetch.ask's canvas {title, markdown} becomes {title, markdown, chars, path}, after writing the markdown to
    `out` (atomically) when given."""
    doc = result.get("canvas")
    if not doc:
        return
    doc.update(chars=len(doc["markdown"]), path=None)
    if out is not None:
        _write(out, doc["markdown"], result["chatId"])
        doc["path"] = str(out.resolve())


def _canvas_out_arg(ctx: click.Context, param: click.Parameter, value: str | None) -> Path | None:
    """--canvas-out: a file path. Its directory is created now, so a bad path fails before anything is sent."""
    if value is None:
        return None
    path = Path(value).expanduser()
    if path.is_dir() or value.endswith(("/", "\\")):
        raise click.BadParameter(f"{value} is a directory; give a file path, e.g. {path / 'canvas.md'}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise click.BadParameter(f"can't create directory {path.parent}: {exc}") from None
    return path


def _files_arg(ctx: click.Context, param: click.Parameter, value: tuple[str, ...]) -> list[str]:
    """--file: Gemini's upload limits (MAX_*), checked before Chrome opens."""
    if len(value) > MAX_FILES:
        raise click.BadParameter(f"Gemini takes at most {MAX_FILES} files per prompt; got {len(value)}.")
    for name in value:
        size = Path(name).stat().st_size
        video = _media(name) == "video"
        if size > (MAX_VIDEO_BYTES if video else MAX_FILE_BYTES):
            limit = "2 GB per video" if video else "100 MB per file (2 GB for a video)"
            raise click.BadParameter(f"{name} is {size / 2**20:,.1f} MB; Gemini takes up to {limit}.")
        if name.lower().endswith(".zip"):
            _check_zip(name)
    return list(value)


def _media(name: str) -> str | None:
    """"video" or "audio" by the file name's MIME type, else None."""
    kind = (mimetypes.guess_type(name)[0] or "").partition("/")[0]
    return kind if kind in ("video", "audio") else None


def _check_zip(name: str) -> None:
    try:
        with zipfile.ZipFile(name) as archive:
            members = [m.filename for m in archive.infolist() if not m.is_dir()]
    except (OSError, zipfile.BadZipFile) as exc:
        raise click.BadParameter(f"{name} isn't a readable ZIP: {exc}") from None
    if len(members) > MAX_ZIP_FILES:
        raise click.BadParameter(f"{name} holds {len(members)} files; Gemini takes a ZIP of up to {MAX_ZIP_FILES}.")
    if media := next((m for m in members if _media(m)), None):
        raise click.BadParameter(f"{name} holds {media}; Gemini takes no audio or video inside a ZIP.")


def _timeout(timeout: int | None, model: str | None, image_out: Path | None, canvas: bool = False) -> int:
    """An explicit --timeout, else IMAGE_TIMEOUT for --image-out or an image model, CANVAS_TIMEOUT for --canvas,
    else TEXT_TIMEOUT."""
    if timeout is not None:
        return timeout
    if image_out or (model and IMAGE_MODEL.search(model)):
        return IMAGE_TIMEOUT
    return CANVAS_TIMEOUT if canvas else TEXT_TIMEOUT


def _canvas(canvas: bool, canvas_out: Path | None, image_out: Path | None) -> bool:
    """Whether to use the Canvas tool: --canvas, or --canvas-out (which implies it). Not with --image-out."""
    wanted = canvas or canvas_out is not None
    if wanted and image_out is not None:
        die("Use --canvas/--canvas-out or --image-out, not both: Gemini runs one tool per prompt.", EXIT_USAGE)
    return wanted


def _reply_text(result: dict) -> str:
    """Text output: the answer, then each saved image's path, then the Canvas doc's path (or its markdown when it
    wasn't saved). An image reply that saved nothing says so."""
    lines = [result["answer"]] if result["answer"] else []
    lines += result["images"]
    if doc := result.get("canvas"):
        lines.append(doc["path"] or doc["markdown"])
    if result["imageCount"] and not result["images"]:
        count = result["imageCount"]
        lines.append(f"[Gemini made {count} image{'s' if count > 1 else ''}: {result['chatUrl'] or 'see the chat'}. "
                     "Save images with --image-out DIR.]")
    return "\n".join(lines)


def _error_reply(result: dict, *, want_images: bool = False, want_canvas: bool = False) -> bool:
    """Whether Gemini's answer is its usage-limit notice or its own error message, or lacks the image (want_images)
    or the Canvas doc (want_canvas) that was asked for.
    If so, the JSON result gets the error fields too: error, code, message, resets_at for a limit; just error otherwise."""
    if limit_reply(result["answer"]):
        result.update(error="rate_limited", code=EXIT_RATE_LIMITED, message=redact_text(result["answer"]),
                      resets_at=resets_at(result["answer"]))
    elif error_reply(result["answer"]):
        result["error"] = redact_text(f"Gemini replied with an error instead of an answer: "
                                      f"{truncate(result['answer'], 200)}")
    elif want_images and not result["imageCount"]:
        result["error"] = redact_text(f"Gemini answered without an image: "
                                      f"{truncate(result['answer'], 200) or '(empty)'}")
    elif want_canvas and not result.get("canvas"):
        result["error"] = redact_text(f"Gemini answered without a Canvas doc: "
                                      f"{truncate(result['answer'], 200) or '(empty)'}")
    return "error" in result


def _die_error_reply(result: dict) -> None:
    """Exit 6 after a limit notice, else 5. Any JSON output already went out, answer and error included."""
    where = f" (chat {result['chatId']})" if result["chatId"] else ""
    if result.get("code") == EXIT_RATE_LIMITED:
        output.die_rate_limited(result["message"], result["resets_at"], f"The prompt reached Gemini{where}.",
                                site="Gemini")
    die(result["error"], EXIT_API, hint=f"The prompt reached Gemini{where}. Retry it, or try another --model.")


def _thinking_arg(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    """l/m/h -> low/medium/high. click.Choice has already lower-cased the value."""
    return {level[0]: level for level in fetch.THINKING_LEVELS}.get(value, value)


file_option = click.option(
    "--file", "files", multiple=True, type=click.Path(exists=True, dir_okay=False, resolve_path=True),
    callback=_files_arg,
    help=f"Attach a file (repeatable), e.g. an image for --image-out to edit. Gemini takes up to {MAX_FILES} per "
         "prompt, 100 MB each, 2 GB per video.")
canvas_option = click.option(
    "--canvas", is_flag=True,
    help="Turn on Gemini's Canvas tool and return the reply's Canvas doc (JSON canvas: title, markdown, chars, "
         "path). Exit 5 if the reply has none.")
canvas_out_option = click.option(
    "--canvas-out", metavar="PATH", default=None, callback=_canvas_out_arg,
    help="Write the Canvas doc's markdown to PATH (atomically, replacing PATH; implies --canvas).")
image_out_option = click.option(
    "--image-out", metavar="DIR|PATH", default=None, callback=llm.image_out_arg(images.is_dir_target),
    help="Turn on Gemini's Create image tool and save the reply's images at full size: into DIR as "
         "<prompt-slug>-N.<ext>, or to PATH (.png/.jpg/.jpeg/.webp; more images get -2, -3...). Exit 5 if no image.")
no_footnotes_option = click.option(
    "--no-footnotes", "no_footnotes", is_flag=True,
    help="Drop the [^n] marks and the footnote block from a cited answer (JSON citations still lists the sources).")
timeout_option = click.option(
    "--timeout", default=None, type=click.IntRange(min=1),
    help=f"Total seconds for setup, sending, the final answer, and image downloads "
         f"(default {TEXT_TIMEOUT}; {IMAGE_TIMEOUT} with --image-out or an image --model such as Argon; "
         f"{CANVAS_TIMEOUT} with --canvas).")
thinking_option = click.option(
    "--thinking", type=click.Choice([*fetch.THINKING_LEVELS, *(level[0] for level in fetch.THINKING_LEVELS)],
                                    case_sensitive=False),
    default=None, callback=_thinking_arg,
    help="Pick this Thinking level in Gemini's model menu first (low/medium/high or l/m/h). "
         "Default: leave Gemini's level alone.")
headed_option = click.option("--headed", is_flag=True, help="Show the Chrome window (the one visible mode).")


@group.command("status")
@headed_option
@agent_output_options(rows=False)
def status_cmd(headed: bool, fmt: str | None, quiet: bool) -> None:
    """Show the identity's signed-in Google account and whether the chat API answers (exit 4 if signed out)."""
    info = _call("status", headed=headed) if llm.identity().profile_exists() else fetch.signed_out()
    info = {"identity": llm.identity().id, **info}
    out = resolve_format(fmt)
    if out == "json":
        emit_json(info)
    else:
        kv_table("Gemini session", info, out)
    if not info["signedIn"]:
        die("Not signed in to Gemini.", EXIT_AUTH, hint=llm.login_hint(SITE))
    raise SystemExit(EXIT_OK)


@group.command("ask")
@click.argument("prompt", callback=llm.prompt_arg)
@click.option("-m", "--model", "model", metavar="NAME", default=None,
              help="Pick this model in Gemini's model menu first (case-insensitive; see `llmcli gemini models`).")
@thinking_option
@click.option("--new", "new", is_flag=True, help="Start a new chat (the default).")
@click.option("--chat", "chat_id", metavar="ID", default=None, callback=_id_arg, help="Continue this chat.")
@file_option
@image_out_option
@canvas_option
@canvas_out_option
@no_footnotes_option
@timeout_option
@headed_option
@agent_output_options(formats=("text", "md", "json"), rows=False)
def ask_cmd(prompt, model, thinking, new, chat_id, files, image_out, canvas, canvas_out, no_footnotes, timeout, headed,
            fmt, quiet) -> None:
    """Send PROMPT (or `-` for stdin) and print Gemini's answer. The one command that writes."""
    if new and chat_id:
        die("Use --new or --chat, not both.", EXIT_USAGE)
    canvas = _canvas(canvas, canvas_out, image_out)
    result = _ask(prompt, chat_id, image_out=image_out, canvas_out=canvas_out, model=model, thinking=thinking,
                  files=files, timeout=_timeout(timeout, model, image_out, canvas), headed=headed, canvas=canvas,
                  footnotes=not no_footnotes)
    failed = _error_reply(result, want_images=image_out is not None, want_canvas=canvas)
    if resolve_format(fmt, human="text") == "json":
        emit_json(result)
    elif not failed:  # Gemini's error message isn't printed as if it were the answer
        click.echo(_reply_text(result))
        note(f"Chat {result['chatId']} ({result['model'] or 'default model'}). Continue with --chat {result['chatId']}.",
             quiet=quiet)
    if failed:
        _die_error_reply(result)
    raise SystemExit(EXIT_OK)


@group.command("-p")
@click.argument("words", metavar="[PROMPT]...", nargs=-1)
@click.option("--chat", "chat_id", metavar="ID", default=None, callback=_id_arg,
              help="Send into this chat. Default: a new chat.")
@click.option("-c", "--continue", "resume", is_flag=True, help="Send into the chat of the last finished ask.")
@click.option("-m", "--model", "model", metavar="NAME", default=None,
              help="Pick this model in Gemini's model menu first (case-insensitive; see `llmcli gemini models`).")
@thinking_option
@file_option
@image_out_option
@canvas_option
@canvas_out_option
@no_footnotes_option
@timeout_option
@llm.prompt_file_option
@llm.output_file_option
@llm.output_format_option
@click.option("--quiet", "-q", is_flag=True, help="No `chat: <id>` line on stderr.")
def print_cmd(words, chat_id, resume, model, thinking, files, image_out, canvas, canvas_out, no_footnotes, timeout,
              prompt_file, output_file, output_format, quiet) -> None:
    """Print mode (or --print), like `claude -p`: send PROMPT (words, `-`/none for stdin) and print only the answer."""
    if chat_id and resume:
        die("Use --chat or -c, not both.", EXIT_USAGE)
    llm.check_output_file(output_file)
    canvas = _canvas(canvas, canvas_out, image_out)
    prompt = llm.print_prompt(words, prompt_file)
    result = _ask(prompt, llm.read_last_chat(SITE, clean_chat_id) if resume else chat_id, image_out=image_out,
                  canvas_out=canvas_out, model=model, thinking=thinking, files=files,
                  timeout=_timeout(timeout, model, image_out, canvas), canvas=canvas, footnotes=not no_footnotes)
    failed = _error_reply(result, want_images=image_out is not None, want_canvas=canvas)
    if output_format == "json" or not failed:  # Gemini's error message isn't printed as if it were the answer
        out = (json.dumps(result, separators=(",", ":")) if output_format == "json" else _reply_text(result)) + "\n"
        llm.emit_answer(output_file, out, lambda path, text: _write(path, text, result["chatId"]))
    if failed:
        _die_error_reply(result)
    if result["chatId"] and not quiet:
        click.echo(f"chat: {result['chatId']}", err=True)
    raise SystemExit(EXIT_OK)


@group.group("chats", cls=click.Group)
def chats_group() -> None:
    """Your Gemini chat history (READ-ONLY)."""


@chats_group.command("list")
@click.option("--limit", default=50, show_default=True, type=click.IntRange(min=1), help="Max rows.")
@headed_option
@agent_output_options()
def chats_list_cmd(limit, headed, fmt, compact, select, as_csv, quiet) -> None:
    """Your chats in sidebar order: pinned first, then most recent."""
    _need_session()
    rows = _call("chats", limit, headed=headed)
    if not rows:
        die("No chats found.", EXIT_NOT_FOUND)
    emit_rows(rows[:limit], title="Gemini chats", fields=CHAT_FIELDS, compact_fields=CHAT_COMPACT,
              columns=["id", "title", "updated", "pinned"], noun="chats", fmt=fmt, compact=compact,
              select=select, quiet=quiet, as_csv=as_csv)
    raise SystemExit(EXIT_OK)


@chats_group.command("show")
@click.argument("chat_id", metavar="ID", callback=_id_arg)
@headed_option
@agent_output_options(formats=("text", "md", "json"), rows=False)
def chats_show_cmd(chat_id, headed, fmt, quiet) -> None:
    """One chat's full transcript, oldest turn first (exit 3 if there's no such chat)."""
    _need_session()
    chat = _call("chat", chat_id, headed=headed)
    if resolve_format(fmt, human="text") == "json":
        emit_json(chat)
    else:
        for message in chat["messages"]:
            click.echo(f"## {'You' if message['role'] == 'user' else 'Gemini'}\n\n{message['text']}\n")
    raise SystemExit(EXIT_OK)


@group.command("models")
@headed_option
@agent_output_options()
def models_cmd(headed, fmt, compact, select, as_csv, quiet) -> None:
    """Models in Gemini's model menu, with the current one marked (READ-ONLY: nothing is picked)."""
    _need_session()
    rows = _call("models", headed=headed)
    if not rows:
        die("Gemini's model menu had no entries.", EXIT_NOT_FOUND, hint=fetch.SELECTOR_HINT)
    emit_rows(rows, title="Gemini models", fields=MODEL_FIELDS, compact_fields=MODEL_COMPACT,
              columns=["name", "description", "current"], noun="models", fmt=fmt, compact=compact,
              select=select, quiet=quiet, as_csv=as_csv)
    raise SystemExit(EXIT_OK)


llm.add_common_commands(group, SITE, notes=AGENT_NOTES, writes=WRITES)
