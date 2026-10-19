"""Agent-native output for both binaries and every site (ported from gweb/cgpt agent_ux.py).

Exit codes: the unified table in EXIT_CODES (0 ok, 2 usage, 3 not found, 4 sign-in, 5 site/network, 6 rate limited,
7 config, 128+N killed by signal N).
Output: an explicit --format wins, then $LLMCLI_FORMAT / $WEBCLI_FORMAT (whichever binary is running), then JSON
when stdout is not a TTY.
Errors: JSON on stderr when stderr is not a TTY or the format env is json (tmux panes are TTYs).
Redaction: die(), note(), warn(), usage errors, and log() all print through redact.redact_text, and every error is
also appended (redacted) to logs/<app>.log.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
import tempfile
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import click

from webcli.core.errors import AuthRequired, Challenged, ConfigError, Interrupted, NotFound, ProfileBusy, RateLimited
from webcli.core.redact import redact_text

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_AUTH = 4
EXIT_API = 5
EXIT_RATE_LIMITED = 6
EXIT_CONFIG = 7
EXIT_CODES = {
    EXIT_OK: "ok (also a fallback to another model without --strict-model: JSON has fallback true; and an image reply "
             "without --image-out: nothing saved, JSON has imageHint)",
    EXIT_USAGE: "usage error (bad option, argument, id, or format env; --file over the site's upload limits)",
    EXIT_NOT_FOUND: "not found / no results (chat id, model name)",
    EXIT_AUTH: "sign-in needed, or Cloudflare needs a visible check (error \"hidden_browser_blocked\"): a person runs "
               "`<binary> identity login <identity>`",
    EXIT_API: "site or network error (profile busy, timeout, the site changed, the site replied with an error, "
              "--image-out got no image or couldn't save it, --canvas got no Canvas doc)",
    EXIT_RATE_LIMITED: "rate limited: the site refused for a usage cap (JSON {\"error\": \"rate_limited\", \"code\": 6, "
                       "\"message\", \"resets_at\": ISO 8601 or null}; don't retry before resets_at), or --strict-model "
                       "and the site answered with another model (error \"model_fallback\", requestedModel, "
                       "servedModel)",
    EXIT_CONFIG: "config error: missing or invalid config.toml, identity, or site (error names what to fix)",
    128 + 1: "killed by SIGHUP (command cancelled, its Chrome tab closed)",
    128 + 2: "Ctrl-C",
    128 + 15: "killed by SIGTERM (command cancelled, its Chrome tab closed)",
}
ERROR_SHAPES = {
    "default": {"error": "<message>", "code": "<exit code>", "hint": "<optional>"},
    "rate_limited": {"error": "rate_limited", "code": EXIT_RATE_LIMITED, "message": "<the site's notice>",
                     "resets_at": "<ISO 8601 (America/Chicago) or null>", "hint": "<what to do>"},
    "model_fallback": {"error": "model_fallback", "code": EXIT_RATE_LIMITED, "message": "<what happened>",
                       "requestedModel": "<slug>", "servedModel": "<slug>", "hint": "<what to do>"},
    "hidden_browser_blocked": {"error": "hidden_browser_blocked", "code": EXIT_AUTH, "message": "<what happened>",
                               "hint": "<run identity login>"},
}
# Which binary is running: set by apps.llmcli / apps.webcli before anything prints.
APP = "webcli"
FORMAT_ENV = "WEBCLI_FORMAT"
ENV_FORMATS = ("json", "table", "text", "md")
PRINT_FLAGS = ("-p", "--print")
LOG_BYTES, LOG_BACKUPS = 1_000_000, 3


def env_format() -> str | None:
    value = os.environ.get(FORMAT_ENV, "").strip().lower()
    if not value:
        return None
    if value not in ENV_FORMATS:
        raise click.UsageError(f"{FORMAT_ENV} must be one of {', '.join(ENV_FORMATS)} (got {value!r}).")
    return value


def resolve_format(fmt: str | None, *, human: str = "table") -> str:
    """'json', 'md', or the command's human format. table/text in the format env both mean "human"."""
    if fmt:
        return fmt
    env = env_format()
    if env in ("json", "md"):
        return env
    if env:
        return human
    return human if sys.stdout.isatty() else "json"


def json_errors() -> bool:
    try:
        return env_format() == "json" or not sys.stderr.isatty()
    except click.UsageError:
        return not sys.stderr.isatty()


def select_fields(data: Any, select: str | None) -> Any:
    keys = [k.strip() for k in (select or "").split(",") if k.strip()]
    if not keys:
        return data
    if isinstance(data, list):
        return [{k: (item or {}).get(k) for k in keys} for item in data]
    if isinstance(data, dict):
        return {k: data.get(k) for k in keys}
    return data


def compact_rows(rows: Iterable[dict], fields: Sequence[str]) -> list[dict]:
    return [{k: row.get(k) for k in fields} for row in rows]


def emit_json(data: Any) -> None:
    if sys.stdout.isatty():
        click.echo(json.dumps(data, indent=2, default=str))
    else:
        click.echo(json.dumps(data, separators=(",", ":"), default=str))


def emit_csv(rows: Sequence[dict], fields: Sequence[str] | None = None) -> None:
    import csv

    rows = list(rows)
    if not rows:
        return
    fields = list(fields or rows[0].keys())
    writer = csv.DictWriter(sys.stdout, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k) for k in fields})


def _md_cell(value: Any) -> str:
    return ("" if value is None else str(value)).replace("|", "\\|").replace("\n", " ")


def emit_md(rows: Sequence[dict], fields: Sequence[str] | None = None) -> None:
    """A GitHub markdown table."""
    rows = list(rows)
    if not rows:
        return
    fields = list(fields or rows[0].keys())
    lines = ["| " + " | ".join(fields) + " |", "|" + "---|" * len(fields)]
    lines += ["| " + " | ".join(_md_cell(row.get(k)) for k in fields) + " |" for row in rows]
    click.echo("\n".join(lines))


def truncate(text: str | None, n: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _cell(value: Any) -> str:
    from rich.markup import escape

    return escape("" if value is None else str(value))


def kv_table(title: str, data: dict, fmt: str = "table") -> None:
    if fmt == "md":
        emit_md([{"field": k, "value": v} for k, v in data.items()])
        return
    from rich.console import Console
    from rich.table import Table

    table = Table(title=title)
    table.add_column("field")
    table.add_column("value")
    for key, value in data.items():
        table.add_row(key, _cell(value))
    Console().print(table)


def rows_table(title: str, rows: list[dict], columns: list[str]) -> None:
    from rich.console import Console
    from rich.table import Table

    table = Table(title=title)
    for col in columns:
        table.add_column(col)
    for row in rows:
        table.add_row(*[_cell(truncate(str(row.get(c) or ""), 60) if c in ("title", "description") else row.get(c))
                        for c in columns])
    Console().print(table)


def emit_rows(rows, *, title, fields, compact_fields, columns, noun, fmt, compact, select, quiet, as_csv) -> None:
    data = select_fields(rows, select) if select else compact_rows(rows, compact_fields if compact else fields)
    out = "csv" if as_csv else resolve_format(fmt)
    if out == "csv":
        emit_csv(data)
    elif out == "json":
        emit_json(data)
    elif out == "md":
        emit_md(data)
    else:
        rows_table(title, rows, columns)
    note_showing(len(rows), quiet=quiet, noun=noun)


def write_atomic(path: Path, text: str) -> None:
    """Write via a temp file in the same dir, then rename: readers see the old file or the new one,
    never half of it. The file ends up mode 0600."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


# Logs: logs/<name>.log, 0600, rotated at LOG_BYTES. The formatter redacts the whole rendered record, tracebacks
# included, so nothing reaches the file unredacted.

class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


class _PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(fd, "a", encoding=self.encoding or "utf-8")


def logger(name: str) -> logging.Logger:
    """The redacting, rotating file logger for logs/<name>.log."""
    from webcli.core.config import logs_dir

    path = logs_dir() / f"{name}.log"
    log = logging.getLogger(f"webcli.file.{path}")
    if not log.handlers:
        handler = _PrivateRotatingHandler(path, maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
        handler.setFormatter(_RedactingFormatter("%(asctime)s %(message)s", "%Y-%m-%dT%H:%M:%S"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


def log(message: str, *, name: str | None = None) -> None:
    """One redacted line in logs/<name>.log (default: the running binary's). Never raises: a log that can't be
    written must not turn a result into an error."""
    try:
        logger(name or APP).info(redact_text(message))
    except Exception:
        pass


class RedactingStream(io.TextIOBase):
    """A text stream that redacts each complete line (a token split across writes is joined first) and hands it to
    `write_line`: the daemon's sys.stdout / sys.stderr."""

    def __init__(self, write_line: Callable[[str], None]) -> None:
        super().__init__()
        self._write_line, self._pending = write_line, ""

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        self._pending += text
        *lines, self._pending = self._pending.split("\n")
        for line in lines:
            self._write_line(redact_text(line))
        return len(text)

    def flush(self) -> None:
        if self._pending:
            line, self._pending = self._pending, ""
            self._write_line(redact_text(line))


class _RedactingFile:
    """file= for click's echo (exc.show): redacts what it writes."""

    def __init__(self, stream) -> None:
        self.stream = stream

    def write(self, text: str) -> int:
        return self.stream.write(redact_text(text))

    def flush(self) -> None:
        self.stream.flush()


def die(message: str, code: int = EXIT_API, hint: str | None = None, *, error: str | None = None, **extra: Any) -> None:
    """Exit `code` with `message` on stderr, redacted (and logged). With `error` (a fixed tag such as "rate_limited"),
    the JSON error is {"error": error, "code", "message": message, **extra, "hint"}; otherwise
    {"error": message, "code", **extra, "hint"}."""
    message = redact_text(message)
    hint = redact_text(hint) if hint else None
    extra = {k: redact_text(v) if isinstance(v, str) else v for k, v in extra.items()}
    if json_errors():
        payload = {"error": error, "code": code, "message": message} if error else {"error": message, "code": code}
        payload.update(extra)
        if hint:
            payload["hint"] = hint
        click.echo(json.dumps(payload), err=True)
    else:
        click.echo(message, err=True)
        if hint:
            click.echo(hint, err=True)
    log(f"exit {code}: {error + ': ' if error else ''}{message}{' | ' + hint if hint else ''}")
    raise SystemExit(code)


def note(message: str, *, quiet: bool) -> None:
    """A human hint on stderr: only on a terminal, never in JSON-errors mode."""
    if not quiet and not json_errors():
        click.echo(redact_text(message), err=True)


def warn(message: str) -> None:
    """A warning on stderr in every mode (e.g. a model fallback)."""
    click.echo(redact_text(message), err=True)


def note_showing(n: int, *, quiet: bool, noun: str = "results") -> None:
    note(f"Showing {n} {noun}. To narrow: add --limit, --select a,b, or --compact.", quiet=quiet)


def die_rate_limited(message: str, resets: str | None, where: str, *, site: str) -> None:
    """Exit 6 with the site's notice. JSON: {"error": "rate_limited", "code": 6, "message", "resets_at", "hint"}."""
    until = f"until {resets}" if resets else f"until {site}'s limit resets"
    die(message, EXIT_RATE_LIMITED, error="rate_limited", resets_at=resets,
        hint=f"Rate limited {until}: don't retry before then, or try another --model. {where}")


def fail(exc: BaseException, *, site: str, login: str, chats: str, challenge: str | None = None) -> None:
    """Exit with the code for `exc` (the errors.py table). `site` names it in messages ("Gemini"), `login` is the
    sign-in hint, `chats` the command that lists chats (for "check before resending"), `challenge` the Cloudflare
    hint."""
    if isinstance(exc, SystemExit):
        raise exc
    if isinstance(exc, Challenged):
        hint = challenge or login
        if exc.mode == "visible":
            die(str(exc), EXIT_AUTH, error="cloudflare_challenge", hint=hint)
        die(f"Cloudflare needs a visible check: {login}", EXIT_AUTH, error="hidden_browser_blocked",
            hint=f"{hint} No window is ever shown for other commands.")
    if isinstance(exc, AuthRequired):
        die(str(exc), EXIT_AUTH, hint=login)
    if isinstance(exc, NotFound):
        die(str(exc), EXIT_NOT_FOUND, hint=exc.hint)
    if isinstance(exc, RateLimited):
        where = (f"The prompt may have reached {site}: check `{chats}` before sending it again."
                 if exc.sent else "The prompt was not sent.")
        die_rate_limited(exc.message, exc.resets_at, where, site=site)
    if isinstance(exc, Interrupted):
        die(str(exc), 128 + exc.signum)
    if isinstance(exc, ConfigError):
        die(str(exc), EXIT_CONFIG, hint=exc.hint)
    if isinstance(exc, ProfileBusy):
        die(str(exc), EXIT_API)
    die(str(exc) or type(exc).__name__, EXIT_API)


class AgentGroup(click.Group):
    """click.Group whose usage errors are JSON too when errors are JSON (exit 2), whose ConfigErrors exit 7, and
    where `-p` or `--print` among the group's options runs the command named "-p" (print mode, like `claude -p`)."""

    def parse_args(self, ctx, args):
        if "-p" in self.commands:
            valued = {o for p in self.params if isinstance(p, click.Option) and not p.is_flag for o in p.opts}
            i = 0
            while i < len(args) and args[i].startswith("-") and args[i] != "--":
                if args[i] in PRINT_FLAGS:  # "--" so click takes "-p" as the command name, not an option
                    args = [*args[:i], "--", "-p", *args[i + 1:]]
                    break
                i += 2 if args[i] in valued else 1
        return super().parse_args(ctx, args)

    def main(self, args=None, prog_name=None, complete_var=None, standalone_mode=True, **extra):
        if not standalone_mode:
            return super().main(args, prog_name, complete_var, standalone_mode, **extra)
        try:
            rv = super().main(args, prog_name, complete_var, standalone_mode=False, **extra)
        except click.ClickException as exc:
            if json_errors():
                die(exc.format_message(), exc.exit_code)
            exc.show(file=_RedactingFile(sys.stderr))
            raise SystemExit(exc.exit_code) from None
        except click.Abort:
            die("Aborted.", 128 + 2)
        except ConfigError as exc:
            die(str(exc), EXIT_CONFIG, hint=exc.hint)
        raise SystemExit(rv if isinstance(rv, int) else EXIT_OK)


def agent_output_options(*, formats: tuple[str, ...] = ("table", "md", "json"), rows: bool = True) -> Callable:
    """Click decorator: --format (and, for row output, --compact / --select / --csv) plus -q."""
    human = formats[0]
    if rows and "csv" not in formats:
        formats = (*formats[:-1], "csv", formats[-1])

    def decorator(f: Callable) -> Callable:  # applied innermost-first, so --format lists first
        f = click.option("--quiet", "-q", is_flag=True, help="Suppress non-data messages on stderr.")(f)
        if rows:
            f = click.option("--csv", "as_csv", is_flag=True, help="CSV to stdout (same as --format csv).")(f)
            f = click.option("--select", default=None, help="Comma-separated fields (JSON/CSV/md).")(f)
            f = click.option("--compact", is_flag=True, help="High-gravity fields only.")(f)
        return click.option(
            "--format",
            "fmt",
            type=click.Choice(list(formats)),
            default=None,
            help=f"Default: {human} on a terminal, json when piped; ${FORMAT_ENV} overrides the default.",
        )(f)

    return decorator


def write_options(f: Callable) -> Callable:
    """--dry-run (the default for every write verb) / --confirm (alias --yes): the plumbing for future data-site
    writes. A verb calls confirm_write(confirm, plan) and does the write only when it returns True."""
    f = click.option("--confirm", "--yes", "confirm", is_flag=True, help="Do the write (default: dry run).")(f)
    return click.option("--dry-run", "dry_run", is_flag=True,
                        help="Show what would be written and write nothing (the default).")(f)


def confirm_write(confirm: bool, plan: dict, *, dry_run: bool = False) -> bool:
    """True when the write may go ahead (--confirm without --dry-run). Otherwise prints the plan as
    {"dryRun": true, ...plan} and returns False."""
    if confirm and not dry_run:
        return True
    emit_json({"dryRun": True, **plan, "hint": "Nothing was written. Re-run with --confirm to write."})
    return False


def _leaves(group: click.Group, path: tuple[str, ...] = ()):
    """(path, aliases, command) for every visible command, descending into subgroups like `chats`."""
    names: dict[int, list[str]] = {}
    for name, cmd in group.commands.items():
        names.setdefault(id(cmd), []).append(name)
    for name, cmd in group.commands.items():
        if names[id(cmd)][0] != name or cmd.hidden:
            continue
        if isinstance(cmd, click.Group):
            yield from _leaves(cmd, (*path, name))
        else:
            yield (*path, name), names[id(cmd)][1:], cmd


def agent_reference(group: click.Group, ctx: click.Context, *, env: dict[str, str], notes: list[str],
                    writes: list[str]) -> dict:
    """Commands, aliases, args, options, exit codes, error shapes, and env vars, read from the click tree."""
    commands = []
    for path, aliases, cmd in _leaves(group):
        args, options = [], []
        for p in cmd.params:
            if isinstance(p, click.Argument):
                args.append(p.human_readable_name)
            elif isinstance(p, click.Option) and not p.hidden:
                opt = {"flags": p.opts + p.secondary_opts, "help": p.help or ""}
                if isinstance(p.type, click.Choice):
                    opt["choices"] = list(p.type.choices)
                elif not p.is_flag:
                    opt["type"] = p.type.name
                if not p.is_flag and isinstance(p.default, (int, float, str)):
                    opt["default"] = p.default
                options.append(opt)
        commands.append({
            "name": " ".join(path),
            "aliases": aliases,
            "usage": " ".join([ctx.command_path, *path, *cmd.collect_usage_pieces(ctx)]),
            "summary": cmd.get_short_help_str(200),
            "args": args,
            "options": options,
        })
    global_options = [
        {"flags": p.opts, "help": p.help or ""} for p in group.params if isinstance(p, click.Option) and not p.hidden
    ]
    return {
        "name": ctx.command_path,
        "writes": writes,
        "globalOptions": global_options,
        "commands": commands,
        "exitCodes": {str(k): v for k, v in EXIT_CODES.items()},
        "errorShapes": ERROR_SHAPES,
        "env": env,
        "notes": notes,
    }


def reference_markdown(ref: dict) -> str:
    lines = [f"# {ref['name']} agent reference", "", "## Commands"]
    for cmd in ref["commands"]:
        alias = f" (alias: {', '.join(cmd['aliases'])})" if cmd["aliases"] else ""
        lines.append(f"- `{cmd['usage']}`{alias}: {cmd['summary']}")
        for opt in cmd["options"]:
            extra = f" [{'|'.join(map(str, opt['choices']))}]" if "choices" in opt else ""
            extra += f" (default {opt['default']})" if "default" in opt else ""
            lines.append(f"  - `{', '.join(opt['flags'])}`{extra}: {opt['help']}")
    lines += ["", "## Writes (everything else is read-only)"] + [f"- {w}" for w in ref["writes"]]
    lines += ["", "## Global options"] + [f"- `{', '.join(o['flags'])}`: {o['help']}" for o in ref["globalOptions"]]
    lines += ["", "## Exit codes"] + [f"- {k}: {v}" for k, v in ref["exitCodes"].items()]
    lines += ["", "## Error shapes (JSON on stderr)"] + [f"- {k}: `{json.dumps(v)}`"
                                                         for k, v in ref["errorShapes"].items()]
    lines += ["", "## Environment"] + [f"- `{k}`: {v}" for k, v in ref["env"].items()]
    lines += ["", "## Notes"] + [f"- {n}" for n in ref["notes"]]
    return "\n".join(lines)


def agent_help_command(group: click.Group, *, version: str, env: Callable[[], dict], notes: list[str],
                       writes: list[str]) -> click.Command:
    """The `agent-help` command for `group` (every umbrella and site has one)."""

    @click.command("agent-help")
    @click.option("--format", "fmt", type=click.Choice(["markdown", "md", "json"]), default=None,
                  help="Default: markdown on a terminal, json when piped; the format env overrides the default.")
    @click.pass_context
    def agent_help_cmd(ctx: click.Context, fmt: str | None) -> None:
        """Compact command / flag / exit-code reference for agents."""
        ref = {"version": version, **agent_reference(group, ctx.parent, env=env(), notes=notes, writes=writes)}
        if resolve_format(fmt, human="markdown") == "json":
            emit_json(ref)
        else:
            click.echo(reference_markdown(ref))
        raise SystemExit(EXIT_OK)

    return agent_help_cmd


def stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
