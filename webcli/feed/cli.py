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
    rows_table, warn,
)
from webcli.feed import pull as pullmod
from webcli.feed import push as pushmod
from webcli.feed import schedule as schedmod

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
    "[youtube] since = \"48h\", [x] file = \"...\". Flags override it.",
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
    names = [s.strip() for s in value.split(",")] if value else list(cfg.get("sites") or pullmod.DEFAULT_SITES)
    bad = [s for s in names if s not in pullmod.SITES]
    if bad or not names:
        raise click.BadParameter(f"unknown site {', '.join(bad) or '(none)'}; choose from {', '.join(pullmod.SITES)}.",
                                 param_hint="--sites")
    return list(dict.fromkeys(names))


@group.command("pull")
@click.option("--sites", default=None, help=f"Comma-separated, in order (default {','.join(pullmod.DEFAULT_SITES)}).")
@click.option("--out", "out", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="File to write (default ~/.webcli/feeds/YYYY-MM-DD-HHMM.json).")
@click.option("--limit-per-site", type=click.IntRange(1, 500), default=None,
              help=f"Rows per read (default {pullmod.DEFAULT_LIMIT}; each site's own cap applies).")
@click.option("--since", default=None, help="youtube uploads window (default 48h).")
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
    x_file = x_file or (cfg.get("x") or {}).get("file")
    if x_file and "x" not in names:
        names.append("x")
    limit = limit_per_site or int(cfg.get("limit_per_site") or pullmod.DEFAULT_LIMIT)
    since = since or (cfg.get("youtube") or {}).get("since") or "48h"
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


@schedule_group.command("remove")
@json_flag
@agent_output_options(rows=False)
def schedule_remove(fmt, quiet) -> None:
    """Unload and delete the LaunchAgent."""
    _emit_status(schedmod.remove(), fmt)
    raise SystemExit(EXIT_OK)


@schedule_group.command("status")
@json_flag
@agent_output_options(rows=False)
def schedule_status(fmt, quiet) -> None:
    """Whether the plist exists and is loaded, and its times."""
    _emit_status(schedmod.status(), fmt)
    raise SystemExit(EXIT_OK)


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
