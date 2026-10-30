"""Every URL and rpcid webcli gsc touches, in one table. Fix this file first when a capture disagrees.

Recon 2026-10-30 on google:default, in a hidden Chrome behind a fail-closed guard. Search Console
(search.google.com/search-console) is a WIZ app. Its report pages are server-rendered (AF_initDataCallback chunks,
whose AF_dataServiceRequests name the rpcid and payload each chunk came from), and every one of those chunks can be
asked for again as one batchexecute RPC:

  POST /_/SearchConsoleAggReportUi/data/batchexecute?rpcids=<id>&source-path=/search-console&bl=..&f.sid=..
  form body f.req=[[[rpcid, "<payload json>", null, "generic"]]] & at=<token>

The `at` token (WIZ_global_data.SNlM0e), build label (cfb2h), and session id (FdrFJe) are read inside the page by
fetch.py's JS, as gemini and notebooklm do; Python only ever sees response bodies. So webcli gsc loads one page,
then makes exactly the RPCs below, one at a time. The page's own load-time RPCs (callouts, message counts, and
SM7Bqb, which saves the recent-properties list) are aborted: nothing it needs.

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

# Performance --dimension -> the report's dimension id (Search Console's own: 1 date, 2 query, 3 page, 4 country).
DIMENSIONS = {"date": 1, "query": 2, "page": 3}
VIEW_PERFORMANCE = 32
VIEW_INDEX_REASONS = 35
VIEW_REASON_URLS = 33
VIEW_INDEX_SERIES = 30
INDEX_FILTERS = [[12, None, None, None, None, None, None, None, None, None, None, 3], [9, ""]]  # all known pages
NOT_INDEXED = [26, None, None, None, None, None, None, None, None, None, 4]

DEFAULT_LIMIT = 25
MAX_LIMIT = 1000
DEFAULT_EXAMPLES = 5
MAX_EXAMPLES = 100
POLL_SECONDS = 1.5
INSPECT_TIMEOUT_S = 60
REQUEST_INDEXING_TIMEOUT_S = 240  # the live test alone can take "a minute or two"
