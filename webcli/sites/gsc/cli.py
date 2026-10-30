"""`webcli gsc`: Google Search Console from your own signed-in, hidden Chrome (google:default). Reads: status,
properties, pages, inspect, performance, sitemaps. Two writes, each refused (exit 2, nothing sent) without --yes:
`sitemaps submit PROPERTY SITEMAP_URL` and `inspect URL --request-indexing`. Every write attempt, refused or not,
leaves one line in ~/.webcli/logs/gsc.log."""

from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urlsplit

import click

from webcli.core.output import (
    EXIT_AUTH,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_SENT_UNVERIFIED,
    EXIT_USAGE,
    agent_output_options,
    die,
    emit_json,
    emit_rows,
    json_flag,
    log,
    resolve_format,
    rows_table,
)
from webcli.sites import data
from webcli.sites.gsc import ADAPTER as SITE
from webcli.sites.gsc import parse
from webcli.sites.gsc.endpoints import DEFAULT_EXAMPLES, DEFAULT_LIMIT, DIMENSIONS, MAX_EXAMPLES, MAX_LIMIT

AUDIT_LOG = "gsc"  # ~/.webcli/logs/gsc.log (0600, rotated, redacted)
PROPERTY_HELP = "sc-domain:example.com, a bare example.com (a Domain property), or https://www.example.com/."
NOTES = [
    "Identity google:default (signed in through gemini's login; webcli never signs in). Signed out: exit 4, and "
    "a person runs `webcli identity login google:default`. Commands run one at a time in that identity's daemon, a "
    "hidden headless Chrome.",
    "How: Search Console's own batchexecute RPCs, made from inside a signed-in search.google.com/search-console "
    "page (the session token never leaves the page). A fail-closed guard lets through only the read RPCs each "
    "command needs; the page's own load-time writes are aborted. Unofficial: an RPC change exits 5.",
    f"PROPERTY: {PROPERTY_HELP} `properties` lists them (type domain | url-prefix, permission owner | full | "
    "restricted).",
    f"`pages PROPERTY [--examples N]`: the Page indexing report: indexed and notIndexed (the last day with data, "
    f"asOf), and each not-indexed reason (reason, source Website | Google systems, pages) with up to N example "
    f"URLs and their last crawl (default {DEFAULT_EXAMPLES}, max {MAX_EXAMPLES}, 0 skips them).",
    "`inspect URL [--property P]`: URL Inspection's indexed data (never a live test): onGoogle, verdict, "
    "coverageState, lastCrawl, crawledAs, crawlAllowed, pageFetch, userCanonical, googleCanonical, inSitemap, "
    "sitemaps, referringUrls. Without --property, the property that covers the URL (the longest URL-prefix one, "
    "else the Domain one).",
    "`performance PROPERTY --start YYYY-MM-DD --end YYYY-MM-DD --dimension query|page|date [--limit N]`: web "
    "search clicks, impressions, ctr (clicks / impressions, 0..1), and average position. Default range: the 28 days "
    f"ending yesterday; --limit default {DEFAULT_LIMIT}, max {MAX_LIMIT}; query and page rows by clicks then impressions "
    "(the top N), date rows oldest first (the latest N days). Search Console's own data lags about two days and "
    "leaves out anonymized queries.",
    "`sitemaps PROPERTY`: submitted sitemaps: status, submitted, lastRead, discoveredPages, errors, warnings.",
    "Times are ISO 8601 UTC. A challenge or block (HTTP 403/429, Google's \"unusual traffic\" page) exits 5: webcli "
    "stops and never works around it.",
]
WRITES = [
    "`sitemaps submit PROPERTY SITEMAP_URL --yes` (alias --confirm): one sitemap submit, as the Sitemaps page's "
    "SUBMIT does (the guard allows exactly that call, once), then reads the list back ({submitted, listed, row}). "
    "Exit 8 when it went out but isn't listed afterwards.",
    "`inspect URL --request-indexing --yes`: URL Inspection's REQUEST INDEXING, clicked in Search Console's own "
    "page (Google's live test runs first, a minute or two; the guard allows one indexing request). Exit 6 on "
    "Google's daily quota.",
    "Without --yes both print what they would do ({dryRun: true, wouldDo, ...}), contact nothing, and exit 2. "
    "Every write, refused or done, appends one line to ~/.webcli/logs/gsc.log (time, write, outcome, property, "
    "URL; never a token).",
]

group = data.site_group(SITE, "Google Search Console: properties, page indexing, URL inspection, performance, "
                              "sitemaps (unofficial; writes only with --yes). Identity google:default.")


def audit(write: str, outcome: str, **fields) -> None:
    """One line in logs/gsc.log: `write=... outcome=refused|attempt|done|failed key=value ...`. Never raises."""
    extra = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    log(f"write={write} outcome={outcome} identity={data.identity().id} {extra}".rstrip(), name=AUDIT_LOG)


def _property(value: str) -> str:
    try:
        return parse.normalize_property(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="PROPERTY") from None


def _url(value: str) -> str:
    parts = urlsplit((value or "").strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise click.BadParameter(f"Not a URL: {value!r} (use https://...).", param_hint="URL")
    return value.strip()


def _refuse(write: str, would: str, plan: dict) -> None:
    audit(write, "refused", **{k: v for k, v in plan.items() if isinstance(v, str)})
    emit_json({"dryRun": True, "write": write, "wouldDo": would, **plan,
               "hint": "Nothing was sent to Google. Re-run with --yes to do it."})
    die(f"Not done without --yes. Would: {would}", EXIT_USAGE, hint="re-run with --yes")


def _write(write: str, op: str, *args, plan: dict) -> dict:
    fields = {k: v for k, v in plan.items() if isinstance(v, str)}
    audit(write, "attempt", **fields)
    try:
        result = data.call(SITE, op, *args, confirm=True)
    except SystemExit as exc:
        audit(write, "failed", exit=exc.code, **fields)
        raise
    audit(write, "done", **fields)
    return result


@group.command("status")
@json_flag
@agent_output_options(rows=False)
def status_cmd(fmt, quiet) -> None:
    """Whether google:default is signed in to Search Console, and how many properties it sees (exit 4 if not)."""
    info = data.call(SITE, "status")
    data.emit_record("Search Console session", info, fmt)
    if not info["signedIn"]:
        die("Not signed in to Search Console.", EXIT_AUTH, hint=data.login_hint())
    raise SystemExit(EXIT_OK)


@group.command("properties")
@json_flag
@agent_output_options()
def properties_cmd(fmt, compact, select, quiet, as_csv) -> None:
    """Your verified properties (Domain and URL-prefix) with your permission level."""
    rows = data.call(SITE, "properties")
    if not rows:
        die("This account has no Search Console properties.", EXIT_NOT_FOUND)
    emit_rows(rows, title="Search Console properties", fields=("property", "type", "permission", "permissionCode",
                                                               "displayName"),
              compact_fields=("property", "type", "permission"), columns=["property", "type", "permission"],
              noun="properties", fmt=fmt, compact=compact, select=select, quiet=quiet, as_csv=as_csv)
    raise SystemExit(EXIT_OK)


@group.command("pages")
@click.argument("prop", metavar="PROPERTY")
@click.option("--examples", default=DEFAULT_EXAMPLES, show_default=True, type=click.IntRange(0, MAX_EXAMPLES),
              help="Example URLs per not-indexed reason (0: none).")
@json_flag
@agent_output_options(rows=False)
def pages_cmd(prop, examples, fmt, quiet) -> None:
    """The Page indexing report: indexed / not indexed counts and each not-indexed reason with example URLs."""
    report = data.call(SITE, "pages", _property(prop), examples)
    if resolve_format(fmt) == "json":
        emit_json(report)
        raise SystemExit(EXIT_OK)
    title = (f"{report['property']}: {report['indexed']} indexed, {report['notIndexed']} not indexed"
             + (f" (as of {report['asOf']})" if report.get("asOf") else ""))
    rows = [{"reason": r["reason"], "source": r["source"] or "", "pages": r["pages"]} for r in report["reasons"]]
    rows_table(title, rows, ["reason", "source", "pages"])
    for r in report["reasons"]:
        for ex in r.get("examples") or []:
            click.echo(f"  {r['reason']}: {ex['url']}  (last crawled {ex['lastCrawled'] or 'n/a'})")
    raise SystemExit(EXIT_OK)


@group.command("inspect")
@click.argument("url")
@click.option("--property", "prop", default=None, help=f"The property: {PROPERTY_HELP} Default: the one that "
                                                        "covers URL.")
@click.option("--request-indexing", is_flag=True, help="WRITE: ask Google to recrawl URL (needs --yes).")
@click.option("--yes", "--confirm", "confirm", is_flag=True, help="Do the --request-indexing write.")
@json_flag
@agent_output_options(rows=False)
def inspect_cmd(url, prop, request_indexing, confirm, fmt, quiet) -> None:
    """URL Inspection (indexed data): coverage, last crawl, canonicals, sitemaps. --request-indexing --yes asks
    Google to index URL (without --yes: prints the plan, exit 2)."""
    url = _url(url)
    prop = _property(prop) if prop else None
    if confirm and not request_indexing:
        raise click.UsageError("--yes goes with --request-indexing; inspect alone is read-only.")
    if request_indexing:
        plan = {"url": url, "property": prop}
        would = (f"run URL Inspection's REQUEST INDEXING for {url} in {prop or 'the property that covers it'}: a "
                 "live test, then one indexing request to Google")
        if not confirm:
            _refuse("request_indexing", would, plan)
        result = _write("request_indexing", "request_indexing", url, prop, plan=plan)
        data.emit_record("Indexing requested", result, fmt)
        raise SystemExit(EXIT_OK)
    info = data.call(SITE, "inspect", url, prop)
    if resolve_format(fmt) == "json":
        emit_json(info)
    else:
        data.emit_record(f"URL Inspection: {url}", {k: (", ".join(v) if isinstance(v, list) else v)
                                                    for k, v in info.items()}, fmt)
    raise SystemExit(EXIT_OK)


@group.command("performance")
@click.argument("prop", metavar="PROPERTY")
@click.option("--start", default=None, help="YYYY-MM-DD (default: 28 days before --end).")
@click.option("--end", default=None, help="YYYY-MM-DD (default: yesterday).")
@click.option("--dimension", type=click.Choice(list(DIMENSIONS)), default="query", show_default=True)
@click.option("--limit", default=DEFAULT_LIMIT, show_default=True, type=click.IntRange(1, MAX_LIMIT))
@json_flag
@agent_output_options()
def performance_cmd(prop, start, end, dimension, limit, fmt, compact, select, quiet, as_csv) -> None:
    """Web search performance by query, page, or date: clicks, impressions, CTR, average position."""
    try:
        last = parse.check_date(end, "--end") if end else date.today() - timedelta(days=1)
        first = parse.check_date(start, "--start") if start else last - timedelta(days=27)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from None
    if first > last:
        raise click.UsageError("--start is after --end.")
    rows = data.call(SITE, "performance", _property(prop), first.isoformat(), last.isoformat(), dimension, limit)
    if not rows:
        die(f"No {dimension} data in {first} .. {last}.", EXIT_NOT_FOUND)
    cols = [dimension, "clicks", "impressions", "ctr", "position"]
    emit_rows(rows, title=f"{prop}: {dimension} ({first} .. {last})", fields=tuple(cols), compact_fields=tuple(cols),
              columns=cols, noun="rows", fmt=fmt, compact=compact, select=select, quiet=quiet, as_csv=as_csv)
    raise SystemExit(EXIT_OK)


@group.command("sitemaps")
@click.argument("args", nargs=-1, metavar="PROPERTY | submit PROPERTY SITEMAP_URL")
@click.option("--yes", "--confirm", "confirm", is_flag=True, help="Do the `submit` write.")
@json_flag
@agent_output_options()
def sitemaps_cmd(args, confirm, fmt, compact, select, quiet, as_csv) -> None:
    """`sitemaps PROPERTY`: submitted sitemaps, status, discovered URLs. `sitemaps submit PROPERTY SITEMAP_URL
    --yes`: submit one (without --yes: prints the plan, exit 2)."""
    if args and args[0] == "submit":
        if len(args) != 3:
            raise click.UsageError("Usage: webcli gsc sitemaps submit PROPERTY SITEMAP_URL [--yes]")
        prop = _property(args[1])
        try:
            sitemap = parse.check_sitemap_url(args[2], prop)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="SITEMAP_URL") from None
        plan = {"property": prop, "sitemap": sitemap}
        would = f"submit sitemap {sitemap} to {prop} (one Search Console sitemap submit)"
        if not confirm:
            _refuse("sitemaps_submit", would, plan)
        result = _write("sitemaps_submit", "submit_sitemap", prop, sitemap, plan=plan)
        if resolve_format(fmt) == "json":
            emit_json(result)
        else:
            data.emit_record("Sitemap submitted", {k: v for k, v in result.items() if k != "row"}, fmt)
        if not result.get("listed"):
            die(f"Submitted {sitemap}, but Search Console doesn't list it yet.", EXIT_SENT_UNVERIFIED,
                error="sent_unverified", hint=f"check: webcli gsc sitemaps {prop}; don't submit it again")
        raise SystemExit(EXIT_OK)
    if confirm:
        raise click.UsageError("--yes goes with `sitemaps submit`; `sitemaps PROPERTY` is read-only.")
    if len(args) != 1:
        raise click.UsageError("Usage: webcli gsc sitemaps PROPERTY  |  webcli gsc sitemaps submit PROPERTY URL")
    prop = _property(args[0])
    rows = data.call(SITE, "sitemaps", prop)
    if not rows:
        die(f"{prop} has no submitted sitemaps.", EXIT_NOT_FOUND)
    emit_rows(rows, title=f"{prop}: sitemaps",
              fields=("sitemap", "type", "submitted", "lastRead", "status", "statusCode", "discoveredPages",
                      "discoveredVideos", "errors", "warnings"),
              compact_fields=("sitemap", "status", "discoveredPages"),
              columns=["sitemap", "status", "lastRead", "discoveredPages", "errors", "warnings"], noun="sitemaps",
              fmt=fmt, compact=compact, select=select, quiet=quiet, as_csv=as_csv)
    raise SystemExit(EXIT_OK)


data.add_agent_help(group, notes=NOTES, writes=WRITES)
