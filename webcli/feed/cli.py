"""`webcli feed pull|schedule|push|show`."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from webcli import __version__
from webcli.core import output
from webcli.core.output import (
    EXIT_API, EXIT_CONFIG, EXIT_NOT_FOUND, EXIT_OK, EXIT_USAGE, agent_output_options, emit_json, json_flag, kv_table,
    effects,
    rows_table, warn,
)
from webcli.feed import pull as pullmod
from webcli.feed import push as pushmod
from webcli.feed import schedule as schedmod
from webcli.sites.youtube.feeds import parse_since as uploads_since  # what `youtube uploads --since` takes

NOTES = [
    "`feed pull` runs each site's own read-only command (`webcli <site> <read> --format json`), one site at a time, "
    "and writes one newsfeed.items/1 JSON file atomically (0600) to ~/.webcli/feeds/YYYY-MM-DD-HHMM.json (or --out). "
    "Sources: youtube (uploads --since 48h + home, Shorts never), gnews foryou, perplexity discover, linkedin feed. "
    "x is never read live: --x-file imports a newsfeed.items/1 file (a missing file is skipped and logged).",
    "Signed in first, the shared default: every adapter reads as its identity and falls back to its public feed only "
    "when the identity is missing or expired (perplexity, gnews have one; youtube and linkedin don't). Fallback rows "
    "carry meta.auth \"public\" (else \"signed_in\"); the file's `sources` entry for the site says auth, needs_login, "
    "and hint (e.g. \"webcli perplexity login\"). A site with no public feed that is signed out is "
    "{site, status: \"needs_login\", hint}.",
    "A site that fails, or is signed out with no public feed, is skipped and logged; the file is still written and "
    "pull exits 5 (partial). A public fallback returned rows, so it exits 0 (stderr and sources say to log in). No "
    "site's read writes anything.",
    "Config: ~/.webcli/feed.toml: sites = [\"youtube\", \"gnews\", \"perplexity\", \"linkedin\"], limit_per_site = 20, "
    "[youtube] since = \"48h\", [x] file = \"...\". Flags override it. A bad value exits 7 before any site reads.",
    "`feed schedule install [--at 05:30,12:30] [--no-load]` writes ~/Library/LaunchAgents/ai.truefrontier.webcli.feed"
    ".plist (StartCalendarInterval, `webcli feed pull --quiet`) and loads it unless --no-load; remove unloads and "
    "deletes it; status reports both.",
    "`feed push --to URL [--file F] [--token-env NAME]` HTTP PUTs the latest file (5 MB cap; https, http only to "
    "localhost). The token is read from $NAME and never printed. Behind Tailscale Serve none is needed.",
]
WRITES = ["feed pull writes only its own file under ~/.webcli/feeds (or --out). feed schedule install/remove write "
          "or delete one LaunchAgent plist. feed push sends one file to the URL you give. No site is ever written to."]


@click.group("feed", cls=output.AgentGroup)
def group() -> None:
    """Pull your feeds from the read-only sites into one file (newsfeed.items/1), schedule it, push it."""


def _sites(value: str | None, cfg: dict) -> list[str]:
    configured = cfg.get("sites") or pullmod.DEFAULT_SITES
    if not value and not (isinstance(configured, (list, tuple)) and all(isinstance(s, str) for s in configured)):
        _bad_config('sites must be a list of names, like ["youtube", "gnews"]')
    names = [s.strip() for s in value.split(",")] if value else list(configured)
    bad = [s for s in names if s not in pullmod.SITES]
    if bad or not names:
        raise click.BadParameter(f"unknown site {', '.join(bad) or '(none)'}; choose from {', '.join(pullmod.SITES)}.",
                                 param_hint="--sites")
    return list(dict.fromkeys(names))


def _since_arg(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    """--since, checked here: otherwise only youtube's own read refuses it, and the other sites still read."""
    try:
        uploads_since(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from None
    return value


def _bad_config(problem: str) -> None:
    output.die(f"feed.toml: {problem}.", EXIT_CONFIG, hint=f"Fix it in {pullmod.config_path()}.")


def _config_values(cfg: dict) -> tuple[int, str, str | None]:
    """feed.toml's limit_per_site, [youtube] since and [x] file. A bad one exits 7 before any site reads. A limit
    over a site's own cap is fine: pull caps it per site."""
    try:
        limit = int(cfg.get("limit_per_site") or pullmod.DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = 0
    if limit < 1:
        _bad_config(f"limit_per_site must be a whole number of 1 or more, not {cfg['limit_per_site']!r}")
    youtube, x = cfg.get("youtube") or {}, cfg.get("x") or {}
    since = (youtube.get("since") or "48h") if isinstance(youtube, dict) else None
    try:
        uploads_since(since)
    except (AttributeError, ValueError):  # AttributeError: not a string
        since = None
    if since is None:
        _bad_config("[youtube] since must be 48h, 3d, 1w or an ISO date, in a [youtube] table")
    if not isinstance(x, dict) or not isinstance(x.get("file") or "", str):
        _bad_config("[x] file must be a path, in an [x] table")
    return limit, since, x.get("file") or None


@effects("never")
@group.command("pull")
@click.option("--sites", default=None, help=f"Comma-separated, in order (default {','.join(pullmod.DEFAULT_SITES)}).")
@click.option("--out", "out", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="File to write (default ~/.webcli/feeds/YYYY-MM-DD-HHMM.json).")
@click.option("--limit-per-site", type=click.IntRange(1, 500), default=None,
              help=f"Rows per read (default {pullmod.DEFAULT_LIMIT}; each site's own cap applies).")
@click.option("--since", default=None, callback=_since_arg, help="youtube uploads window (default 48h).")
@click.option("--x-file", default=None, help="Import X items from this newsfeed.items/1 file (adds x to --sites).")
@json_flag
@agent_output_options(rows=False)
def pull_cmd(sites, out, limit_per_site, since, x_file, fmt, quiet) -> None:
    """Read every configured site (read-only) and write one newsfeed.items/1 file. Exit 5 when partial."""
    try:
        cfg = pullmod.load_config()
    except Exception as exc:  # a broken feed.toml
        output.die(f"Couldn't read {pullmod.config_path()}: {exc}", EXIT_CONFIG)
    names = _sites(sites, cfg)
    cfg_limit, cfg_since, cfg_x = _config_values(cfg)
    limit, since, x_file = limit_per_site or cfg_limit, since or cfg_since, x_file or cfg_x
    if x_file and "x" not in names:
        names.append("x")
    progress = (lambda m: None) if quiet else (lambda m: click.echo(m, err=True))
    doc = pullmod.pull(names, limit, since=since, x_file=x_file, progress=progress)
    path = pullmod.write_atomic(out or pullmod.default_out(), doc)
    for entry in doc["sources"]:
        output.log(f"feed pull {entry['site']}: {entry['status']} count={entry['count']} auth={entry.get('auth')}")
        if entry.get("needs_login") and not quiet:
            warn(f"{entry['site']}: {'needs a login' if entry['status'] == 'needs_login' else 'public feed only'}"
                 f" — a person runs: {entry['hint']}")
        if entry["status"] == "error":
            warn(f"{entry['site']}: skipped ({entry.get('error')})")
    summary = {"file": str(path), "items": len(doc["items"]), "partial": pullmod.partial(doc),
               "sources": doc["sources"]}
    if output.resolve_format(fmt) == "json":
        emit_json(summary)
    else:
        rows_table(f"Feed: {path}", [{k: e.get(k) for k in ("site", "status", "auth", "count", "hint")}
                                     for e in doc["sources"]], ["site", "status", "auth", "count", "hint"])
    raise SystemExit(EXIT_API if summary["partial"] else EXIT_OK)


@effects("never")
@group.command("show")
@click.option("--file", "file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None)
@json_flag
@agent_output_options(rows=False)
def show_cmd(file, fmt, quiet) -> None:
    """The latest feed file's path, time, item count, and sources (not its items)."""
    path = file or pullmod.latest()
    if not path:
        output.die("No feed file yet.", EXIT_NOT_FOUND, "webcli feed pull")
    doc = json.loads(path.read_text())
    info = {"file": str(path), "generated_at": doc.get("generated_at"), "items": len(doc.get("items") or []),
            "sources": doc.get("sources") or []}
    if output.resolve_format(fmt) == "json":
        emit_json(info)
    else:
        kv_table("Feed", {k: v for k, v in info.items() if k != "sources"}, fmt or "table")
        rows_table("Sources", [{k: e.get(k) for k in ("site", "status", "auth", "count", "hint")}
                               for e in info["sources"]], ["site", "status", "auth", "count", "hint"])
    raise SystemExit(EXIT_OK)


@group.group("schedule", cls=output.AgentGroup)
def schedule_group() -> None:
    """The LaunchAgent that runs `feed pull` at set times (ai.truefrontier.webcli.feed)."""


def _emit_status(info: dict, fmt: str | None) -> None:
    if output.resolve_format(fmt) == "json":
        emit_json(info)
    else:
        kv_table("Feed schedule", {**info, "times": ", ".join(info["times"]) or "-"}, fmt or "table")


@effects("always", local_setup=True)
@schedule_group.command("install")
@click.option("--at", "at", default=None, help="Times, 24h local, comma separated (default 05:30,12:30).")
@click.option("--no-load", is_flag=True, help="Write the plist only; don't load it.")
@json_flag
@agent_output_options(rows=False)
def schedule_install(at, no_load, fmt, quiet) -> None:
    """Write the LaunchAgent plist and (unless --no-load) load it."""
    try:
        times = schedmod.parse_times(at)
    except ValueError as exc:
        output.die(str(exc), EXIT_USAGE)
    _emit_status(schedmod.install(times, load=not no_load), fmt)
    raise SystemExit(EXIT_OK)


@effects("always", local_setup=True)
@schedule_group.command("remove")
@json_flag
@agent_output_options(rows=False)
def schedule_remove(fmt, quiet) -> None:
    """Unload and delete the LaunchAgent."""
    _emit_status(schedmod.remove(), fmt)
    raise SystemExit(EXIT_OK)


@effects("never")
@schedule_group.command("status")
@json_flag
@agent_output_options(rows=False)
def schedule_status(fmt, quiet) -> None:
    """Whether the plist exists and is loaded, and its times."""
    _emit_status(schedmod.status(), fmt)
    raise SystemExit(EXIT_OK)


@effects("always")
@group.command("push")
@click.option("--to", "to", required=True, help="URL to PUT the file to (https).")
@click.option("--file", "file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None,
              help="Default: the latest file in ~/.webcli/feeds.")
@click.option("--token-env", default=None, help="Env var holding a bearer token (never printed).")
@json_flag
@agent_output_options(rows=False)
def push_cmd(to, file, token_env, fmt, quiet) -> None:
    """HTTP PUT a feed file to a server you read it from."""
    path = file or pullmod.latest()
    if not path:
        output.die("No feed file yet.", EXIT_NOT_FOUND, "webcli feed pull")
    try:
        info = pushmod.push(path, to, token_env)
    except ValueError as exc:
        output.die(str(exc), EXIT_USAGE)
    except RuntimeError as exc:
        output.die(str(exc), EXIT_API)
    if output.resolve_format(fmt) == "json":
        emit_json(info)
    else:
        kv_table("Pushed", info, fmt or "table")
    raise SystemExit(EXIT_OK)


group.add_command(output.agent_help_command(group, version=__version__, env=lambda: {}, notes=NOTES, writes=WRITES))


def cli() -> click.Group:
    return group


if __name__ == "__main__":
    sys.exit(group())
