"""`webcli facebook`: search and enrich your own Facebook friends from your own signed-in, hidden Chrome (fbc parity).
Read-only on Facebook; the local cache is ~/.webcli/sites/facebook/."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from webcli.core.output import (
    EXIT_AUTH,
    EXIT_NOT_FOUND,
    EXIT_OK,
    agent_output_options,
    die,
    emit_json,
    emit_rows,
    note,
    resolve_format,
    write_atomic,
)
from webcli.sites import data
from webcli.sites.facebook import ADAPTER as SITE
from webcli.sites.facebook import enrich, fetch, store
from webcli.sites.facebook.parse import Person, person_from_arg

SEARCH_FIELDS = ["key", "name", "subtitle", "city", "employer", "role", "linkedin", "profileUrl", "aboutFetchedAt",
                 "enrichedAt"]
SEARCH_COMPACT = ("key", "name", "city", "employer")
SEARCH_COLUMNS = ["key", "name", "city", "employer", "role", "subtitle"]
NOTES = [
    "Identity facebook:default, its own profile (not Muse's meta:default). When `status` exits 4, a person runs "
    "`webcli identity login facebook:default` and signs in to Facebook; it finishes once Facebook's own session is "
    "set and the account reads as signed in from a fresh hidden Chrome. Commands run one at a time in the "
    "identity's daemon, a hidden headless Chrome: no window, no Dock icon, no focus change.",
    "Profile ids are a username (jane.doe), a numeric id, or a profile URL (facebook.com/profile.php?id=N works).",
    "`search` and `export` read only the local cache (~/.webcli/sites/facebook/friends.sqlite): no Chrome, no "
    "Facebook traffic. Run `sync` first.",
    "`about`, `enrich` (when About isn't cached), and `search --enrich` load each About section as a page, with "
    "random pauses: a few seconds per person.",
    "Enriched fields are {value, source, confidence} or null. null means unknown; nothing is guessed. The LinkedIn "
    f"lookup uses the optional `tvly` CLI (${enrich.TVLY_ENV}: its path, or `off`); without it LinkedIn stays null "
    "unless the About tab lists one, and webSearch says \"unavailable\".",
    "`sync` prints {synced, new, updated, cachedFriends, scrolls, complete, blocked}; complete false means the list "
    "may be short (run it again, or see blocked).",
    "`export --format` is the FILE format (json|csv). Default file: ~/.webcli/sites/facebook/exports/"
    "facebook-<UTC time>.<format>, mode 0600.",
]
WRITES = [
    "Nothing on Facebook: no friending, messages, reactions, comments, posts, pokes, follows, or settings. Nothing "
    "clicks, types, or submits; the guard aborts every request but page loads and the read-only queries in "
    "endpoints.py.",
    "Local only: `sync`, `about`, `enrich`, and `search --enrich` update the cache; `export` writes a file (0600).",
]

group = data.site_group(SITE, "Facebook friends: sync, search, about, enrich, export; read-only on Facebook "
                              "(unofficial). Identity facebook:default.")


def _person_arg(ctx: click.Context, param: click.Parameter, value: str | None) -> Person | None:
    if value is None:
        return None
    try:
        return person_from_arg(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from None


def _call(op: str, *args) -> Any:
    data.need_profile()
    return data.call(SITE, op, *args)


def _cell(value: Any) -> str:
    if isinstance(value, dict) and "value" in value:
        value = f"{value['value']}  ({value['source']}, {value['confidence']})"
    elif isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    return escape("" if value is None else str(value))


def _kv_table(title: str, record: dict) -> None:
    table = Table(title=title)
    table.add_column("field")
    table.add_column("value")
    for key, value in record.items():
        table.add_row(key, _cell(value))
    Console().print(table)


def _emit_record(title: str, record: dict, fmt: str | None) -> None:
    if resolve_format(fmt) == "json":
        emit_json(record)
    else:
        _kv_table(title, record)


def _cached(who: Person) -> dict | None:
    return store.get(who.key, who.username, who.numeric_id)


def _about_table(record: dict) -> dict:
    """The About record with work, education, and links as readable lines (table format only)."""
    work = [" ".join(filter(None, [w["raw"], "" if w["current"] else "(former)"])) for w in record.get("work") or []]
    return {**{k: v for k, v in record.items() if k not in ("work", "education", "links", "websites", "blocked")},
            "work": "\n".join(work) or None,
            "education": "\n".join(e["raw"] for e in record.get("education") or []) or None,
            "links": "\n".join(f"{link['type']}: {link['url']}" for link in record.get("links") or []) or None}


def _fetch_about(who: Person) -> dict:
    """Read one About tab and cache it; the record as cached (without blocked)."""
    record = _call("about", fetch.person_dict(who))
    record.pop("blocked", None)
    return store.save_about(record)


def _enrich_person(who: Person, *, refresh: bool, web: bool) -> dict:
    row = _cached(who)
    if refresh or not row or not row.get("about"):
        about = _fetch_about(who)
        row = _cached(Person(about["key"], about["username"], about["numericId"]))
    return store.save_enrich(row["key"], enrich.build(row, row["about"], web=web))


@group.command("status")
@agent_output_options(rows=False)
def status_cmd(fmt: str | None, quiet: bool) -> None:
    """The signed-in Facebook account (exit 4 if signed out)."""
    info = data.call(SITE, "status") if data.identity().profile_exists() else fetch.signed_out()
    _emit_record("Facebook session", info, fmt)
    if not info["signedIn"]:
        die("Not signed in to Facebook.", EXIT_AUTH, hint=data.login_hint())
    raise SystemExit(EXIT_OK)


@group.command("sync")
@click.option("--limit", default=None, type=click.IntRange(min=1), help="Stop after N friends (default: all).")
@agent_output_options(rows=False)
def sync_cmd(limit: int | None, fmt: str | None, quiet: bool) -> None:
    """Scroll your Friends tab into the cache: name, profile URL, id/username, and the work/city line."""
    result = _call("sync", limit)
    rows = result["friends"]
    if not rows:
        die("No friends found on the Friends tab.", EXIT_NOT_FOUND, hint=fetch.SELECTOR_HINT)
    counts = store.save_friends(rows)
    summary = {"synced": len(rows), **counts, "cachedFriends": sum(p["is_friend"] for p in store.people()),
               "scrolls": result["scrolls"], "complete": result["complete"], "blocked": result["blocked"]}
    _emit_record("facebook sync", summary, fmt)
    if any("PaginationQuery" in line for line in result["blocked"]):
        note("The guard blocked a scroll query (see blocked), so the list may be cut short. Once you know that query "
             "only reads, add its name to SYNC_QUERIES in webcli/sites/facebook/endpoints.py.", quiet=quiet)
    elif not result["complete"] and not limit:
        note("The list was still growing when sync stopped; run it again to pick up the rest.", quiet=quiet)
    raise SystemExit(EXIT_OK)


@group.command("about")
@click.argument("profile", metavar="ID|URL", callback=_person_arg)
@click.option("--cached", is_flag=True, help="Print the cached About record; don't open Chrome (exit 3 if none).")
@agent_output_options(rows=False)
def about_cmd(profile: Person, cached: bool, fmt: str | None, quiet: bool) -> None:
    """A profile's About tab as you see it: work, education, current city, hometown, websites and links."""
    if cached:
        row = _cached(profile)
        if not row or not row.get("about"):
            die(f"No cached About for {profile.key}.", EXIT_NOT_FOUND,
                hint=f"Run: webcli facebook about {profile.key}")
        record = row["about"]
    else:
        fetched = _call("about", fetch.person_dict(profile))
        blocked = fetched.pop("blocked", [])
        record = {**store.save_about(fetched), "blocked": blocked}
    _emit_record(f"About {record.get('name') or record['key']}",
                 record if resolve_format(fmt) == "json" else _about_table(record), fmt)
    raise SystemExit(EXIT_OK)


@group.command("enrich")
@click.argument("profile", metavar="ID|URL", callback=_person_arg)
@click.option("--refresh", is_flag=True, help="Re-read the About tab from Facebook even if it's cached.")
@click.option("--no-web", is_flag=True, help="Skip the LinkedIn web search and the company-site check.")
@agent_output_options(rows=False)
def enrich_cmd(profile: Person, refresh: bool, no_web: bool, fmt: str | None, quiet: bool) -> None:
    """Role, company, location, and public links, each {value, source, confidence} or null."""
    result = _enrich_person(profile, refresh=refresh, web=not no_web)
    if resolve_format(fmt) == "json":
        emit_json(result)
    else:
        _kv_table(f"Enrich {result.get('name') or result['key']}",
                  {**{k: v for k, v in result.items() if k != "publicLinks"}, **result["publicLinks"]})
    raise SystemExit(EXIT_OK)


def _enrich_hits(hits: list[dict], *, web: bool, quiet: bool) -> list[dict]:
    """Enrich every hit that has no enrichment yet; read About first for those that lack it."""
    todo = [p for p in hits if not p.get("enrich")]
    missing = [{"key": p["key"], "username": p.get("username"), "numericId": p.get("numeric_id")}
               for p in todo if not p.get("about")]
    if missing:
        for item in _call("about_many", missing):
            if "error" in item:
                note(f"Skipped {item['person']['key']}: {item['error']}", quiet=quiet)
            else:
                item["record"].pop("blocked", None)
                store.save_about(item["record"])
    for p in todo:
        row = store.get(p["key"], p.get("username"), p.get("numeric_id"))
        if row and row.get("about"):
            store.save_enrich(row["key"], enrich.build(row, row["about"], web=web))
    keys = [p["key"] for p in hits]
    fresh = {p["key"]: p for p in store.people() if p["key"] in keys}
    return [fresh.get(k, p) for k, p in zip(keys, hits)]


@group.command("search")
@click.option("--name", default=None, help="Name contains this (case- and accent-insensitive).")
@click.option("--city", default=None, help="Current city, hometown, or Friends-tab line contains this.")
@click.option("--employer", default=None, help="An employer (any job, current or former) contains this.")
@click.option("--keyword", default=None, help="Anywhere in the cached record: name, subtitle, About, enrichment.")
@click.option("--limit", default=50, show_default=True, type=click.IntRange(min=1), help="Max rows.")
@click.option("--enrich", "do_enrich", is_flag=True,
              help="Enrich results that aren't enriched yet (opens Chrome for any whose About isn't cached).")
@click.option("--no-web", is_flag=True, help="With --enrich: skip the LinkedIn web search and company-site check.")
@agent_output_options()
def search_cmd(name, city, employer, keyword, limit, do_enrich, no_web, fmt, compact, select, as_csv, quiet) -> None:
    """Search the local cache (run `sync` first). Filters combine with AND. Exit 3 when nothing matches."""
    people = store.people()
    if not people:
        die("The cache is empty.", EXIT_NOT_FOUND, hint="Run: webcli facebook sync")
    hits = [p for p in people if store.matches(p, name=name, city=city, employer=employer, keyword=keyword)][:limit]
    if not hits:
        die("No cached person matches.", EXIT_NOT_FOUND,
            hint="Loosen the filters, or run `webcli facebook about ID` to cache more.")
    if do_enrich:
        hits = _enrich_hits(hits, web=not no_web, quiet=quiet)
    emit_rows([store.flat(p) for p in hits], title="facebook search", fields=SEARCH_FIELDS,
              compact_fields=SEARCH_COMPACT, columns=SEARCH_COLUMNS, noun="people", fmt=fmt, compact=compact,
              select=select, quiet=quiet, as_csv=as_csv)
    raise SystemExit(EXIT_OK)


@group.command("export")
@click.option("--format", "fmt", type=click.Choice(["json", "csv"]), default="json", show_default=True,
              help="File format.")
@click.option("--out", "out", default=None, type=click.Path(dir_okay=False, resolve_path=True),
              help="Output file (default: ~/.webcli/sites/facebook/exports/facebook-<UTC time>.<format>).")
@click.option("--quiet", "-q", is_flag=True, help="Suppress non-data messages on stderr.")
def export_cmd(fmt: str, out: str | None, quiet: bool) -> None:
    """Write the whole cache to a JSON or CSV file (mode 0600) and print {path, format, count}."""
    people = store.people()
    if not people:
        die("The cache is empty.", EXIT_NOT_FOUND, hint="Run: webcli facebook sync")
    path = Path(out) if out else store.exports_dir() / f"facebook-{store.now().replace(':', '')}.{fmt}"
    if not path.parent.is_dir():
        raise click.BadParameter(f"Directory {path.parent} doesn't exist.", param_hint="--out")
    if fmt == "json":
        records = [{**store.flat(p), "about": p.get("about"), "enrich": p.get("enrich")} for p in people]
        text = json.dumps(records, indent=2, ensure_ascii=False) + "\n"
    else:
        rows = [store.flat(p) for p in people]
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        text = buffer.getvalue()
    write_atomic(path, text)
    summary = {"path": str(path), "format": fmt, "count": len(people)}
    if resolve_format(None) == "json":
        emit_json(summary)
    else:
        click.echo(f"Wrote {len(people)} people to {path}")
    raise SystemExit(EXIT_OK)


data.add_agent_help(group, notes=NOTES, writes=WRITES)
