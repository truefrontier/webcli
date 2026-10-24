"""`webcli notebooklm`: your NotebookLM notebooks from your own signed-in, hidden Chrome. Reads (notebooks, sources,
source text and guides, notes, Studio artifacts), questions with citations (`ask`), and Studio outputs:
`generate <kind>` (a dry run that exits 2 unless --confirm), `status ARTIFACT`, and `download ARTIFACT -o PATH`.
And creating: `create TITLE` (a notebook, optionally with its sources) and `source add NOTEBOOK` (URLs, pasted text,
local .md / .txt / .pdf files), both dry runs that exit 2 unless --confirm."""

from __future__ import annotations

import functools
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import click

from webcli.core.output import (
    EXIT_API,
    EXIT_AUTH,
    EXIT_OK,
    EXIT_USAGE,
    agent_output_options,
    die,
    emit_json,
    emit_rows,
    resolve_format,
    warn,
    write_atomic,
)
from webcli.sites import data
from webcli.sites.notebooklm import ADAPTER as SITE
from webcli.sites.notebooklm import parse

NOTES = [
    "Identity google:default (the profile `webcli identity login google:default` signs in, through gemini). Commands "
    "run one at a time in that identity's daemon, a hidden headless Chrome: no window, no Dock icon, no focus change.",
    "Every command loads notebooklm.google.com and calls the app's own RPCs from inside the page (batchexecute, and "
    "GenerateFreeFormStreamed for `ask`); the session token never leaves the page. NOTEBOOK is a notebook id from "
    "`notebooks` (a notebook URL works too; `ask` also takes its exact title); SOURCE from `sources NOTEBOOK`; ARTIFACT from `artifacts NOTEBOOK`.",
    "Output: a table on a terminal, JSON when piped; --format json (or --json) forces JSON everywhere.",
    "`ask` uses every source unless --source (repeatable). The question joins the notebook's chat history, as if "
    "typed in the app. JSON: {answer, citations: [{number, source_id, source_title, excerpt}], conversation_id}.",
    "`generate KIND NOTEBOOK` creates one Studio output. Kinds: audio (--style deep-dive|brief|critique|debate, "
    "--length short|default|long), video (--style explainer|brief|cinematic|short, --visual auto|classic|whiteboard|"
    "heritage|paper-craft|watercolor|anime|retro-print|kawaii|custom with --visual-prompt), report (--style "
    "briefing-doc|study-guide|blog-post|custom), quiz and flashcards (--quantity, --difficulty), mind-map, "
    "infographic (--orientation, --detail, --visual), slide-deck (--style detailed|presenter, --length default|short), "
    "data-table. All take --language (default en), --instructions, --source (repeatable). FAQ and timeline are not "
    "offered: NotebookLM's current RPCs have no such output (`generate kinds` lists what is).",
    "Without --confirm `generate` is a dry run: it checks the notebook and sources, prints the plan "
    "({dryRun: true, ...}), creates nothing, and exits 2. --confirm (alias --yes) creates it and prints the new "
    "artifact (status pending); --wait then polls (every --interval s, up to --timeout s) until it is completed "
    "(exit 0) or failed (exit 5); a timeout exits 5 and the output keeps generating. --dry-run with --confirm exits 2.",
    "`status ARTIFACT` prints one artifact's status (pending, in_progress, completed, failed); `status` alone is the "
    "sign-in check. -n NOTEBOOK makes either faster; without it every notebook's artifacts are searched.",
    "`download ARTIFACT -o PATH` saves a completed artifact: audio .m4a, video .mp4, infographic .png, slide deck "
    ".pdf, report .md, quiz / flashcards / mind map / data table .json. PATH may be a file or a directory (then "
    "<title><ext>). Files are mode 0600. Not completed yet exits 2.",
    "No such notebook, source, or artifact exits 3. A usage limit exits 6. Not signed in exits 4: a person runs "
    "`webcli identity login google:default`.",
    "`create TITLE` makes a notebook and prints {notebook_id, title, url, sources}; with --file / --url / --urls-file "
    "/ --text it adds those sources to it in the same run. `source add NOTEBOOK` adds sources to an existing notebook "
    "and prints {notebook_id, notebook, url, sources: [{kind, input, id, title, type, status, error}]}. Sources: "
    "--file PATH (repeatable; .md, .markdown, .txt, .pdf, up to 50 MB each), --url URL (repeatable; web pages and "
    "YouTube), --urls-file FILE (one URL per line; # comments; - for stdin), --text TEXT --title T (pasted text; "
    "--text - reads stdin). Each source is sent once, in that order (files, URLs, text); one that fails gets its "
    "error and the rest still go (exit 5 if any failed).",
    "Without --confirm `create` and `source add` are dry runs: they print the plan ({dryRun: true, ...}, `source add` "
    "after checking the notebook exists), create nothing, and exit 2. --wait polls the notebook (every --interval s, "
    "up to --timeout s) until every new source is ready (exit 0) or one errors (exit 5); a timeout exits 5 and they "
    "keep processing.",
    "`llmcli notebooklm` is this site's ask as an llmcli chat site (ask / -p with -n NOTEBOOK, --chat, -c, the "
    "uniform ask JSON); it runs only the reads and the chat.",
    "Reference: the RPC ids and payload shapes follow teng-lin/notebooklm-py (MIT), re-implemented here.",
]
WRITES = [
    "`ask` adds the question and answer to the notebook's chat history (as the app does).",
    "`generate KIND --confirm` creates exactly one Studio output (one CreateArtifact call; the guard allows no other "
    "write). Without --confirm nothing is created.",
    "`create TITLE --confirm` creates exactly one notebook (one CreateProject call) and, with sources, adds them. "
    "`source add NOTEBOOK --confirm` adds exactly the sources named: one AddSources call per URL or text, and per file "
    "one register call plus its upload (two POSTs to NotebookLM's own upload path). The guard counts each and aborts "
    "any call past that budget. Without --confirm nothing is created.",
    "Never: renaming, sharing, or deleting notebooks; editing, refreshing, renaming, or removing sources; notes; "
    "deleting, renaming, or exporting artifacts; research; settings. The guard aborts every one of those RPCs.",
    "`source get -o`, `download`: only the local files you asked for.",
]

group = data.site_group(SITE, "NotebookLM: notebooks, sources, notes, ask with citations, Studio outputs, and "
                              "creating notebooks / adding sources (generate, create, and source add are dry runs "
                              "unless --confirm). Unofficial. Identity google:default.")


def json_flag(f):
    """--json: the same as --format json (webcli's convention is --format; this alias is for muscle memory)."""

    @click.option("--json", "as_json", is_flag=True, help="JSON output (same as --format json).")
    @functools.wraps(f)
    def wrapper(*args, as_json=False, **kwargs):
        if as_json:
            kwargs["fmt"] = "json"
        return f(*args, **kwargs)

    return wrapper


def _id(value: str) -> str:
    return parse.clean_id(value)


notebook_option = click.option("-n", "--notebook", default=None, metavar="NOTEBOOK",
                               help="The notebook it is in (faster; else every notebook is searched).")


@group.command("status")
@click.argument("artifact_id", required=False, metavar="[ARTIFACT]")
@notebook_option
@json_flag
@agent_output_options(rows=False)
def status_cmd(artifact_id, notebook, fmt, quiet) -> None:
    """Sign-in check (exit 4 if signed out), or with ARTIFACT that Studio output's status."""
    if artifact_id:
        row = data.call(SITE, "artifact", _id(artifact_id), _id(notebook) if notebook else None)
        data.emit_record(row["title"] or row["id"], _record(row), fmt)
        raise SystemExit(EXIT_API if row["status"] == "failed" else EXIT_OK)
    info = data.call(SITE, "status")
    data.emit_record("NotebookLM", info, fmt)
    if not info["signedIn"]:
        die("Not signed in to NotebookLM.", EXIT_AUTH, hint=data.login_hint())
    raise SystemExit(EXIT_OK)


@group.command("notebooks")
@json_flag
@agent_output_options()
def notebooks_cmd(**opts) -> None:
    """Your notebooks: id, title, sources (count), created_at, viewed_at."""
    rows = data.call(SITE, "notebooks")
    emit_rows(rows, title="Notebooks", fields=["id", "title", "emoji", "sources", "created_at", "viewed_at", "url"],
              compact_fields=("id", "title", "sources"), columns=["id", "title", "sources", "viewed_at"],
              noun="notebooks", **opts)
    raise SystemExit(EXIT_OK)


@group.command("sources")
@click.argument("notebook")
@json_flag
@agent_output_options()
def sources_cmd(notebook, **opts) -> None:
    """A notebook's sources: id, title, type, status, words, created_at, url."""
    nb = data.call(SITE, "sources", _id(notebook))
    emit_rows(nb["sources"], title=f"Sources: {nb['title']}",
              fields=["id", "title", "type", "status", "words", "created_at", "url"],
              compact_fields=("id", "title", "type"), columns=["id", "title", "type", "status"], noun="sources",
              **opts)
    raise SystemExit(EXIT_OK)


@group.group("source")
def source_group() -> None:
    """Sources: `source get SOURCE` (its indexed text, or --guide), `source add NOTEBOOK` (dry run unless --confirm)."""


@source_group.command("get")
@click.argument("source_id", metavar="SOURCE")
@notebook_option
@click.option("--guide", is_flag=True, help="The source guide (summary and keywords) instead of the text.")
@click.option("-o", "--output", "out", default=None, help="Write the text to this file instead (0600).")
@json_flag
@agent_output_options(rows=False)
def source_get_cmd(source_id, notebook, guide, out, fmt, quiet) -> None:
    """A source's full indexed text (what NotebookLM reads), or with --guide its summary and keywords."""
    found = data.call(SITE, "source", _id(source_id), _id(notebook) if notebook else None, guide=guide)
    if out:
        path = Path(out).expanduser().resolve()
        write_atomic(path, found["summary"] if guide else found["text"])
        if not quiet:
            warn(f"Wrote {path}")
    elif resolve_format(fmt) == "json":
        emit_json(found)
    elif guide:
        click.echo(found["summary"])
        if found["keywords"]:
            click.echo("\nKeywords: " + ", ".join(found["keywords"]))
    else:
        click.echo(found["text"])
    raise SystemExit(EXIT_OK)


@group.command("notes")
@click.argument("notebook")
@json_flag
@agent_output_options()
def notes_cmd(notebook, **opts) -> None:
    """A notebook's saved notes (and note-backed mind maps): id, title, kind, content."""
    rows = data.call(SITE, "notes", _id(notebook))
    emit_rows(rows, title="Notes", fields=["id", "title", "kind", "updated_at", "content"],
              compact_fields=("id", "title", "kind"), columns=["id", "title", "kind"], noun="notes", **opts)
    raise SystemExit(EXIT_OK)


ARTIFACT_FIELDS = ["id", "title", "kind", "status", "created_at", "notebook_id"]


def _record(row: dict) -> dict:
    return {k: row.get(k) for k in ARTIFACT_FIELDS}


@group.command("artifacts")
@click.argument("notebook")
@json_flag
@agent_output_options()
def artifacts_cmd(notebook, **opts) -> None:
    """A notebook's Studio outputs, newest first: id, title, kind, status, created_at."""
    rows = [_record(row) for row in data.call(SITE, "artifacts", _id(notebook))]
    emit_rows(rows, title="Studio outputs", fields=ARTIFACT_FIELDS, compact_fields=("id", "title", "kind", "status"),
              columns=["id", "title", "kind", "status", "created_at"], noun="artifacts", **opts)
    raise SystemExit(EXIT_OK)


@group.command("ask")
@click.argument("notebook")
@click.argument("question")
@click.option("--source", "source_ids", multiple=True, metavar="SOURCE", help="Ask only these sources (repeatable).")
@json_flag
@agent_output_options(rows=False)
def ask_cmd(notebook, question, source_ids, fmt, quiet) -> None:
    """Ask NOTEBOOK a question: the answer and its citations."""
    if not question.strip():
        die("The question is empty.", EXIT_USAGE)
    got = data.call(SITE, "ask", _id(notebook), question, [_id(s) for s in source_ids] or None)
    if resolve_format(fmt) == "json":
        emit_json(got)
    else:
        click.echo(got["answer"])
        if got["citations"]:
            click.echo("\nCitations:")
            for cite in got["citations"]:
                click.echo(f"  [{cite['number']}] {cite['source_title'] or cite['source_id']}")
    raise SystemExit(EXIT_OK)


@group.command("download")
@click.argument("artifact_id", metavar="ARTIFACT")
@click.option("-o", "--output", "out", required=True, help="File, or existing directory (then <title><ext>).")
@notebook_option
@json_flag
@agent_output_options(rows=False)
def download_cmd(artifact_id, out, notebook, fmt, quiet) -> None:
    """Save a completed Studio output: audio .m4a, video .mp4, infographic .png, slides .pdf, report .md, quiz /
    flashcards / mind map / data table .json."""
    path = Path(out).expanduser().resolve()
    if out.endswith("/"):
        path = Path(data.out_dir(out))
    saved = data.call(SITE, "download", _id(artifact_id), str(path), _id(notebook) if notebook else None)
    data.emit_record(saved["title"] or saved["id"], saved, fmt)
    raise SystemExit(EXIT_OK)


# create / source add

DONE = ("ready", "error")


def _source_options(f):
    for decorator in reversed([
        click.option("--file", "files", multiple=True, metavar="PATH",
                     help="A local file to upload: .md, .markdown, .txt, .pdf (repeatable)."),
        click.option("--url", "urls", multiple=True, metavar="URL", help="A web page or YouTube URL (repeatable)."),
        click.option("--urls-file", default=None, metavar="FILE",
                     help="URLs, one per line (blank lines and # comments skipped; - reads stdin)."),
        click.option("--text", default=None, metavar="TEXT", help="Pasted text to add as a source (- reads stdin)."),
        click.option("--title", "text_title", default=None, metavar="T", help="The --text source's title."),
    ]):
        f = decorator(f)
    return f


def _write_options(f):
    for decorator in reversed([
        click.option("--wait", is_flag=True, help="With --confirm: poll until every new source is ready or errors."),
        click.option("--timeout", type=int, default=600, show_default=True, help="--wait limit in seconds."),
        click.option("--interval", type=int, default=5, show_default=True, help="--wait poll interval, seconds."),
        click.option("--confirm", "--yes", "confirm", is_flag=True, help="Create it (default: dry run, exit 2)."),
        click.option("--dry-run", "dry_run", is_flag=True, help="Print the plan, create nothing, exit 2 (default)."),
        json_flag,
        agent_output_options(rows=False),
    ]):
        f = decorator(f)
    return f


def _check_url(url: str) -> str:
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or any(c.isspace() for c in url):
        raise click.UsageError(f"Not an http(s) URL: {url!r}")
    return url


def _items(files, urls, urls_file, text, text_title) -> list[dict]:
    """The sources a create / source add names, validated before anything opens: files (exist, a supported suffix,
    1 byte to 50 MB), URLs (http/https, duplicates dropped), and the one --text (with --title). Files first, then
    URLs, then text: the order they are added in."""
    if urls_file == "-" and text == "-":
        raise click.UsageError("--urls-file - and --text - can't both read stdin.")
    items: list[dict] = []
    for raw in files:
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            raise click.UsageError(f"No such file: {raw}")
        content_type = parse.UPLOAD_TYPES.get(path.suffix.lower())
        if not content_type:
            raise click.UsageError(f"{path.name}: webcli uploads only {', '.join(parse.UPLOAD_TYPES)} files.")
        size = path.stat().st_size
        if size == 0:
            raise click.UsageError(f"{path} is empty.")
        if size > parse.MAX_UPLOAD_BYTES:
            raise click.UsageError(f"{path} is over {parse.MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
        items.append({"kind": "file", "path": str(path), "name": path.name, "content_type": content_type,
                      "bytes": size})
    listed = list(urls)
    if urls_file:
        try:
            body = sys.stdin.read() if urls_file == "-" else Path(urls_file).expanduser().read_text()
        except OSError as exc:
            raise click.UsageError(f"Can't read {urls_file}: {exc}") from None
        listed += [line.strip() for line in body.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    seen = set()
    for raw in listed:
        url = _check_url(raw)
        if url not in seen:
            seen.add(url)
            items.append({"kind": "url", "url": url})
    if text_title is not None and text is None:
        raise click.UsageError("--title names a --text source; pass --text too.")
    if text is not None:
        body = sys.stdin.read() if text == "-" else text
        if not body.strip():
            raise click.UsageError("--text is empty.")
        if not (text_title or "").strip():
            raise click.UsageError("--text needs --title.")
        items.append({"kind": "text", "title": text_title.strip(), "text": body})
    return items


def _emit_added(result: dict, fmt, heading: str) -> None:
    if resolve_format(fmt) == "json":
        emit_json(result)
        return
    click.echo(f"{heading}\n{result['url']}")
    for row in result["sources"]:
        line = f"  {row['id'] or '-':<36}  {row['status'] or '-':<10}  {row['kind']:<4}  {row['input']}"
        click.echo(line + (f"  ({row['error']})" if row.get("error") else ""))


def _wait_sources(result: dict, *, timeout: int, interval: int) -> bool:
    """Poll the notebook until every new source is ready or errored; False on timeout. Updates result in place."""
    rows = [r for r in result["sources"] if r["id"]]
    deadline = time.monotonic() + timeout
    while any(r["status"] not in DONE for r in rows):
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(interval, 2))
        fresh = {s["id"]: s for s in data.call(SITE, "sources", result["notebook_id"])["sources"]}
        for row in rows:
            if row["id"] in fresh and fresh[row["id"]]["status"]:
                row["status"] = fresh[row["id"]]["status"]
    return True


def _finish(result: dict, fmt, heading: str, *, wait: bool, timeout: int, interval: int) -> None:
    finished = _wait_sources(result, timeout=timeout, interval=interval) if wait and result["sources"] else True
    _emit_added(result, fmt, heading)
    failed = [r for r in result["sources"] if r.get("error") or r["status"] == "error"]
    if failed:
        die(f"{len(failed)} of {len(result['sources'])} sources failed.", EXIT_API,
            hint=f"run: webcli notebooklm sources {result['notebook_id']}")
    if not finished:
        die(f"Sources still processing after {timeout}s; they keep processing.", EXIT_API,
            hint=f"run: webcli notebooklm sources {result['notebook_id']}")
    raise SystemExit(EXIT_OK)


@group.command("create")
@click.argument("title")
@_source_options
@_write_options
def create_cmd(title, files, urls, urls_file, text, text_title, wait, timeout, interval, confirm, dry_run, fmt,
               quiet) -> None:
    """Create a notebook titled TITLE (dry run, exit 2, unless --confirm), optionally with its sources."""
    if dry_run and confirm:
        raise click.UsageError("--dry-run and --confirm contradict each other; pass one.")
    title = title.strip()
    if not title:
        raise click.UsageError("The title is empty.")
    items = _items(files, urls, urls_file, text, text_title)
    if not confirm:
        emit_json({"dryRun": True, "title": title, "sources": [parse.describe_item(i) for i in items],
                   "calls": parse.write_budget(items, create=True),
                   "hint": "Nothing was created. Re-run with --confirm to create it."})
        die("Dry run: nothing was created.", EXIT_USAGE, hint="re-run with --confirm")
    result = data.call(SITE, "create", title, items, confirm=True)
    _finish(result, fmt, f"Created notebook {result['title']} ({result['notebook_id']})", wait=wait,
            timeout=timeout, interval=interval)


@source_group.command("add")
@click.argument("notebook")
@_source_options
@_write_options
def source_add_cmd(notebook, files, urls, urls_file, text, text_title, wait, timeout, interval, confirm, dry_run, fmt,
                   quiet) -> None:
    """Add sources to NOTEBOOK: --file (.md/.txt/.pdf), --url, --urls-file, --text with --title (dry run, exit 2,
    unless --confirm)."""
    if dry_run and confirm:
        raise click.UsageError("--dry-run and --confirm contradict each other; pass one.")
    items = _items(files, urls, urls_file, text, text_title)
    if not items:
        raise click.UsageError("Name at least one source: --file, --url, --urls-file, or --text with --title.")
    nb_id = _id(notebook)
    if not confirm:
        plan = data.call(SITE, "plan_sources", nb_id, items)
        emit_json({"dryRun": True, **plan, "hint": "Nothing was added. Re-run with --confirm to add them."})
        die("Dry run: nothing was added.", EXIT_USAGE, hint="re-run with --confirm")
    result = data.call(SITE, "add_sources", nb_id, items, confirm=True)
    _finish(result, fmt, f"Added to {result['notebook'] or result['notebook_id']}", wait=wait, timeout=timeout,
            interval=interval)


# generate

@group.group("generate")
def generate_group() -> None:
    """Create a Studio output (dry run, exit 2, unless --confirm). `generate kinds` lists them."""


KIND_HELP = {
    "audio": "Audio Overview (podcast-style).",
    "video": "Video Overview.",
    "report": "Report: briefing doc, study guide, blog post, or custom.",
    "quiz": "Quiz.",
    "flashcards": "Flashcards.",
    "mind-map": "Mind map (interactive).",
    "infographic": "Infographic.",
    "slide-deck": "Slide deck.",
    "data-table": "Data table.",
}
UNAVAILABLE = {"faq": "not in NotebookLM's current Studio RPCs (the old FAQ note type is gone)",
               "timeline": "not in NotebookLM's current Studio RPCs (the old timeline note type is gone)"}


@generate_group.command("kinds")
@json_flag
@agent_output_options(rows=False)
def kinds_cmd(fmt, quiet) -> None:
    """Every output kind `generate` can create, and the ones it can't."""
    rows = [{"kind": k, "available": True, "about": v} for k, v in KIND_HELP.items()]
    rows += [{"kind": k, "available": False, "about": v} for k, v in UNAVAILABLE.items()]
    if resolve_format(fmt) == "json":
        emit_json(rows)
    else:
        for row in rows:
            click.echo(f"{row['kind']:<12} {'yes' if row['available'] else 'no ':<4} {row['about']}")
    raise SystemExit(EXIT_OK)


def _common(f):
    for decorator in reversed([
        click.argument("notebook"),
        click.option("--source", "source_ids", multiple=True, metavar="SOURCE",
                     help="Use only these sources (repeatable; default all)."),
        click.option("--language", default="en", show_default=True, help="Output language code."),
        click.option("--instructions", default=None, help="Custom instructions (what to focus on)."),
        click.option("--wait", is_flag=True, help="With --confirm: poll until it is completed or failed."),
        click.option("--timeout", type=int, default=1800, show_default=True, help="--wait limit in seconds."),
        click.option("--interval", type=int, default=15, show_default=True, help="--wait poll interval, seconds."),
        click.option("--confirm", "--yes", "confirm", is_flag=True, help="Create it (default: dry run, exit 2)."),
        click.option("--dry-run", "dry_run", is_flag=True, help="Print the plan, create nothing, exit 2 (default)."),
        json_flag,
        agent_output_options(rows=False),
    ]):
        f = decorator(f)
    return f


def _run(kind: str, notebook: str, source_ids, opts: dict, *, confirm: bool, dry_run: bool, wait: bool,
         timeout: int, interval: int, fmt) -> None:
    if dry_run and confirm:
        raise click.UsageError("--dry-run and --confirm contradict each other; pass one.")
    if opts.get("style_prompt") and opts.get("style") != "custom":
        raise click.UsageError("--visual-prompt needs --visual custom.")
    if kind == "video" and opts.get("style") == "custom" and not opts.get("style_prompt"):
        raise click.UsageError("--visual custom needs --visual-prompt.")
    nb_id = _id(notebook)
    sources = [_id(s) for s in source_ids] or None
    if not confirm:
        plan = data.call(SITE, "plan", kind, nb_id, sources, opts)
        emit_json({"dryRun": True, **plan, "hint": "Nothing was created. Re-run with --confirm to create it."})
        die("Dry run: nothing was created.", EXIT_USAGE, hint="re-run with --confirm")
    row = data.call(SITE, "generate", kind, nb_id, sources, opts, confirm=True)
    if not wait:
        data.emit_record(row["title"] or kind, _record(row), fmt)
        raise SystemExit(EXIT_OK)
    deadline = time.monotonic() + timeout
    while row["status"] not in ("completed", "failed"):
        if time.monotonic() >= deadline:
            data.emit_record(row["title"] or kind, _record(row), fmt)
            die(f"{kind} {row['id']} is still {row['status']} after {timeout}s; it keeps generating.", EXIT_API,
                hint=f"run: webcli notebooklm status {row['id']} -n {nb_id}")
        time.sleep(max(interval, 3))
        row = data.call(SITE, "artifact", row["id"], nb_id)
    data.emit_record(row["title"] or kind, _record(row), fmt)
    if row["status"] == "failed":
        die(f"NotebookLM reports {kind} {row['id']} failed.", EXIT_API)
    raise SystemExit(EXIT_OK)


def _choice(values: dict) -> click.Choice:
    return click.Choice(list(values))


@generate_group.command("audio")
@_common
@click.option("--style", "fmt_", type=_choice(parse.AUDIO_FORMATS), default="deep-dive", show_default=True,
              help="Audio format.")
@click.option("--length", type=_choice(parse.AUDIO_LENGTHS), default="default", show_default=True)
def gen_audio(notebook, source_ids, language, instructions, fmt_, length, **run) -> None:
    """Audio Overview: deep-dive (two hosts), brief, critique, or debate."""
    _run("audio", notebook, source_ids, {"format": fmt_, "length": length, "language": language,
                                         "instructions": instructions}, **_pop(run))


@generate_group.command("video")
@_common
@click.option("--style", "fmt_", type=_choice(parse.VIDEO_FORMATS), default="explainer", show_default=True,
              help="Video format (cinematic ignores --visual).")
@click.option("--visual", type=_choice(parse.VIDEO_STYLES), default="auto", show_default=True, help="Visual style.")
@click.option("--visual-prompt", default=None, help="The style description for --visual custom.")
def gen_video(notebook, source_ids, language, instructions, fmt_, visual, visual_prompt, **run) -> None:
    """Video Overview: explainer, brief, cinematic, or short, in a visual style."""
    _run("video", notebook, source_ids, {"format": fmt_, "style": visual, "style_prompt": visual_prompt,
                                         "language": language, "instructions": instructions}, **_pop(run))


@generate_group.command("report")
@_common
@click.option("--style", "fmt_", type=_choice(parse.REPORTS), default="briefing-doc", show_default=True,
              help="Report type (custom: --instructions is the whole prompt).")
def gen_report(notebook, source_ids, language, instructions, fmt_, **run) -> None:
    """Report: briefing doc, study guide, blog post, or custom."""
    _run("report", notebook, source_ids, {"format": fmt_, "language": language, "instructions": instructions},
         **_pop(run))


def _app(kind: str):
    @_common
    @click.option("--quantity", type=_choice(parse.QUANTITIES), default="standard", show_default=True)
    @click.option("--difficulty", type=_choice(parse.DIFFICULTIES), default="medium", show_default=True)
    def command(notebook, source_ids, language, instructions, quantity, difficulty, **run) -> None:
        _run(kind, notebook, source_ids, {"quantity": quantity, "difficulty": difficulty, "language": language,
                                          "instructions": instructions}, **_pop(run))

    command.__doc__ = KIND_HELP[kind]
    return generate_group.command(kind)(command)


_app("quiz")
_app("flashcards")


@generate_group.command("mind-map")
@_common
def gen_mind_map(notebook, source_ids, language, instructions, **run) -> None:
    """Mind map (interactive)."""
    _run("mind-map", notebook, source_ids, {"language": language, "instructions": instructions}, **_pop(run))


@generate_group.command("infographic")
@_common
@click.option("--orientation", type=_choice(parse.ORIENTATIONS), default="landscape", show_default=True)
@click.option("--detail", type=_choice(parse.DETAILS), default="standard", show_default=True)
@click.option("--visual", type=_choice(parse.INFOGRAPHIC_STYLES), default="auto", show_default=True)
def gen_infographic(notebook, source_ids, language, instructions, orientation, detail, visual, **run) -> None:
    """Infographic."""
    _run("infographic", notebook, source_ids, {"orientation": orientation, "detail": detail, "style": visual,
                                               "language": language, "instructions": instructions}, **_pop(run))


@generate_group.command("slide-deck")
@_common
@click.option("--style", "fmt_", type=_choice(parse.SLIDE_FORMATS), default="detailed", show_default=True)
@click.option("--length", type=_choice(parse.SLIDE_LENGTHS), default="default", show_default=True)
def gen_slides(notebook, source_ids, language, instructions, fmt_, length, **run) -> None:
    """Slide deck: detailed or presenter slides."""
    _run("slide-deck", notebook, source_ids, {"format": fmt_, "length": length, "language": language,
                                              "instructions": instructions}, **_pop(run))


@generate_group.command("data-table")
@_common
def gen_table(notebook, source_ids, language, instructions, **run) -> None:
    """Data table."""
    _run("data-table", notebook, source_ids, {"language": language, "instructions": instructions}, **_pop(run))


def _pop(run: dict) -> dict:
    return {k: run[k] for k in ("confirm", "dry_run", "wait", "timeout", "interval", "fmt")}


data.add_agent_help(group, notes=NOTES, writes=WRITES)
