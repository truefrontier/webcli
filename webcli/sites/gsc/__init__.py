"""search.google.com/search-console (webcli gsc): Google Search Console over the signed-in google:default session.
Reads: properties, the Page indexing report, URL Inspection, Performance, Sitemaps. Two writes, each refused (exit 2)
without --yes: `sitemaps submit` and `inspect --request-indexing`. Identity provider "google" (the google:default
profile gemini's login signs in)."""

from __future__ import annotations

from webcli.sites.base import Site, Strategy


def _cli():
    from webcli.sites.gsc.cli import group

    return group


ADAPTER = Site(
    name="gsc",
    kind="data",
    provider="google",
    summary="Google Search Console: properties, pages (indexing), inspect, performance, sitemaps; "
            "sitemaps submit / inspect --request-indexing only with --yes",
    cli=_cli,
    fetch="webcli.sites.gsc.fetch",
    ops=("status", "properties", "pages", "inspect", "performance", "sitemaps", "submit_sitemap",
         "request_indexing"),
    verbs={"status": Strategy.COOKIE, "properties": Strategy.COOKIE, "pages": Strategy.COOKIE,
           "inspect": Strategy.COOKIE, "performance": Strategy.COOKIE, "sitemaps": Strategy.COOKIE,
           "submit_sitemap": Strategy.COOKIE, "request_indexing": Strategy.UI},
    status_op="status",
)
