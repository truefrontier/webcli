"""Every URL and rpcid webcli gsc touches, in one table. Fix this file first when a capture disagrees.

Recon 2026-10-30 on google:default, in a hidden Chrome behind a fail-closed guard. Search Console
(search.google.com/search-console) is a WIZ app. Its report pages are server-rendered (AF_initDataCallback chunks,
whose AF_dataServiceRequests name the rpcid and payload each chunk came from), and every one of those chunks can be
asked for again as one batchexecute RPC:

  POST /_/SearchConsoleAggReportUi/data/batchexecute?rpcids=<id>&source-path=/search-console&bl=..&f.sid=..
  form body f.req=[[[rpcid, "<payload json>", null, "generic"]]] & at=<token>

The `at` token (WIZ_global_data.SNlM0e), build label (cfb2h), and session id (FdrFJe) are read inside the page by
fetch.py's JS, as gemini and notebooklm do; Python only ever sees response bodies. So webcli gsc loads one page,
then makes exactly the RPCs below, one at a time. The page's own load-time RPCs (oGVhvf callouts, gydQ5d message
counts, and SM7Bqb, which saves the recent-properties list) are aborted: nothing it needs.

Payload shapes are Search Console's own, copied from AF_dataServiceRequests and the page's own calls.
"""

from __future__ import annotations

APP_HOST = "search.google.com"
ORIGIN = f"https://{APP_HOST}"
APP_URL = f"{ORIGIN}/search-console"
SOURCE_PATH = "/search-console"
RPC_PREFIX = "/_/SearchConsoleAggReportUi/data/"
BATCH_PATH = RPC_PREFIX + "batchexecute"
HOSTS = (APP_HOST,)

# The reads webcli gsc calls, with what each returns.
RPC = {
    "properties": "BWF8he",  # [] -> [[[siteUrl, kind 5 domain | 1 url-prefix, permission, favicon, name, short]...]]
    "report": "nDAfwb",  # [prop, view, filters, ...] -> a table: 35 index reasons, 33 a reason's URLs, 32 performance
    "series": "OLiH4d",  # [prop, view, filters] -> a daily series: 30 = (not indexed, indexed, impressions)
    "sitemaps": "vyvePb",  # [prop, prop] -> [[[url, type, submittedMs, lastReadMs, [[kind, n]..], errors, warnings,..]]]
    "inspect_start": "RVtklb",  # [url, null, 4, MODE, prop] -> [jobId, 1, [60]]; MODE 2 index status, 1 live test
    "inspect_poll": "MrNfbc",  # [jobId, prop] -> [jobId, 1 running | 2 done, [url, startedMs, "", 0], MODE]
    "inspect_index": "HZPzjb",  # [jobId, prop] -> the Page indexing block (see parse.inspection)
}
INSPECT_MODE_INDEX = 2  # RVtklb's 4th field: the indexed data ("URL is on Google")
INSPECT_LIVE_MODE = 1  # "TEST LIVE URL" (and the first half of REQUEST INDEXING); never in READ

# The two writes, each only with --yes and only in its own policy, at most once per command.
WRITE_RPC = {
    "sitemap_submit": "CyYwI",  # [prop, sitemapUrl]: the Sitemaps page's SUBMIT
    "request_indexing": "Fj3Owf",  # [liveJobId, <attestation the page's own JS mints>, null, prop]: REQUEST INDEXING
}

# What the URL Inspection page itself loads for a result (and its REQUEST INDEXING flow's live test): allowed only in
# the request-indexing policy, where Search Console's own UI drives the page. Captured 2026-10-30.
INSPECT_PAGE_RPCIDS = frozenset({"yol87d", "DETOSe", "HZPzjb", "C4lTm", "zUbeBb", "oUNqRb", "ueowNe", "OFNzWe",
                                 "czrWJf", "YDPhmb", "pPDvCb", "RVtklb", "MrNfbc"})

# Two more reads Search Console's page makes on every load, allowed only in the request-indexing policy (READ and
# submit still abort them). Each takes exactly [property]. Recon 2026-11-06, read-only (the page loaded, the URL
# inspected, REQUEST INDEXING never clicked): both fire at page load, before any click, so neither is part of the
# REQUEST INDEXING flow; 0.18.1 aborted them and the flow doesn't need them, but the page loads as it does for a person
# when they answer. Search Console's own JS marks both read-only (their RPC descriptors have Ob:!0, as every read
# above; the writes Fj3Owf and SM7Bqb have Ob:!1). What they are, from that JS:
#   oGVhvf  the property's callouts: the "New! ..." feature tips Search Console shows ([property] -> a list; a
#           dismissed tip is saved through SM7Bqb, which stays aborted)
#   gydQ5d  the unread Search Console messages count: the bell's badge ("Unread messages: N")
REQUEST_INDEXING_FLOW_RPCIDS = frozenset({"oGVhvf", "gydQ5d"})

# Search Console's own reCAPTCHA v3 attestation for REQUEST INDEXING, allowed only in the request-indexing policy
# (READ and submit still abort them). Its REQUEST INDEXING module (read 2026-11-06) loads
# www.google.com/recaptcha/api.js with Search Console's site key and calls grecaptcha.execute(key, {action:
# "submit"}); the token goes into Fj3Owf. reCAPTCHA's own frame then POSTs these two paths on www.google.com (https,
# default port; any query). With them aborted, execute still resolved, but with an unscored fallback token, and Google
# refused Fj3Owf with code 7 (PERMISSION_DENIED). webcli never reads, handles, or stores the token: the page's JS
# mints it and the page sends it. This is not a workaround: Google's check runs as it does for a person, and Google
# decides. It may still refuse a headless browser (code 7); then request indexing by hand (manual_url).
RECAPTCHA_HOST = "www.google.com"
RECAPTCHA_POSTS = frozenset({"/recaptcha/api2/reload", "/recaptcha/api2/clr"})

# What Search Console's REQUEST INDEXING does after the click (its JS module with Fj3Owf, read 2026-11-06): a modal
# "Testing if live URL can be indexed" (the live test: RVtklb mode 1, then polls), then "Submitting request" (Fj3Owf),
# then one result dialog. "Indexing requested" shows only after Fj3Owf succeeds. Any failure in that chain shows
# "Oops! Something went wrong / We had a problem submitting your indexing request", error code 8 shows "Quota
# Exceeded", and a failed live test shows "Indexing request rejected" (Fj3Owf never sent). The dialogs are
# role=dialog|alertdialog with aria-modal=true; the nav drawer is role=dialog with aria-modal=false, and other panels
# print their own "Something went wrong", so fetch.py reads the result from the modal dialog only.
RESULT_DIALOG = "[role=dialog][aria-modal=true], [role=alertdialog]"
# URL Inspection for one URL in Search Console, for a person to open (request indexing by hand, or check it).
INSPECT_PAGE_URL = f"{APP_URL}/inspect"

# Telemetry every gsc policy aborts without listing it in errors (guard.quiet): Google Tag Manager, Google Analytics,
# Google's client log, and Search Console's own JS error, client log, and browser-info beacons.
QUIET_HOSTS = frozenset({"www.googletagmanager.com", "www.google-analytics.com"})
QUIET_PATHS = {"play.google.com": frozenset({"/log"}),
               APP_HOST: frozenset({f"/_/SearchConsoleAggReportUi/{p}" for p in ("jserror", "log", "browserinfo")})}

# Performance --dimension -> the report's dimension id (Search Console's own: 1 date, 2 query, 3 page, 4 country).
DIMENSIONS = {"date": 1, "query": 2, "page": 3}
VIEW_PERFORMANCE = 32
VIEW_INDEX_REASONS = 35
VIEW_REASON_URLS = 33
VIEW_INDEX_SERIES = 30
VIEW_PERFORMANCE_SERIES = 27  # OLiH4d: the Performance chart, with the range's totals and the data's last update
# The Pages report's page set (its "All known pages" menu): filter 12 picks the set, filter 9 names a sitemap.
PAGES_ALL_KNOWN, PAGES_SUBMITTED, PAGES_SITEMAP = 3, 2, 1
PAGE_SETS = {PAGES_ALL_KNOWN: "all-known", PAGES_SUBMITTED: "all-submitted", PAGES_SITEMAP: "sitemap"}


def index_filters(pages: int = PAGES_ALL_KNOWN, sitemap: str = "") -> list:
    """The Pages report's filters: all known pages (3), all submitted pages (2), or one sitemap's pages (1, url)."""
    return [[12, None, None, None, None, None, None, None, None, None, None, pages], [9, sitemap]]


INDEX_FILTERS = index_filters()  # all known pages
NOT_INDEXED = [26, None, None, None, None, None, None, None, None, None, 4]
INDEXED_KEY = "valid"  # view 33's key for the indexed pages' list ("View data about indexed pages")

DEFAULT_LIMIT = 25
MAX_LIMIT = 1000
DEFAULT_EXAMPLES = 5
MAX_EXAMPLES = 1000  # Search Console itself lists at most 1000 example URLs per reason
STALE_DAYS = 3  # the Pages report is stale when its last update is older than this many days
REPORT_TZ = "America/Los_Angeles"  # Search Console's reporting days (its "Last update" date)
LIVE_SITEMAP_MAX_FILES = 50  # --check-live-sitemap: sitemap files followed (an index counts)
LIVE_SITEMAP_MAX_BYTES = 20 * 1024 * 1024  # per file, after gzip
LIVE_SITEMAP_TIMEOUT_S = 20
POLL_SECONDS = 1.5
INSPECT_TIMEOUT_S = 60
REQUEST_INDEXING_TIMEOUT_S = 240  # the live test alone can take "a minute or two"
