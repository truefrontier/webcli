# webcli gsc: Google Search Console

`webcli gsc` reads Google Search Console as you, through the signed-in `google:default` Chrome profile (the same
one `llmcli gemini`, `webcli keep`, ... use). It needs no OAuth client, API key, or service account: every command
runs Search Console's own web-app RPCs from inside a hidden, signed-in `search.google.com/search-console` page, so
the session never leaves the machine. Unofficial: if Google changes an RPC, the command exits 5.

Signed out, every command exits 4; a person runs `webcli identity login google:default`.

## Commands

| Command | What it returns |
|---|---|
| `webcli gsc status` | signed in or not, and how many properties it sees |
| `webcli gsc properties` | your properties: `property`, `type` (domain / url-prefix), `permission` (owner / full / restricted), `displayName` |
| `webcli gsc pages PROPERTY [--examples N] [--sitemap all\|SITEMAP_URL] [--indexed]` | the Page indexing report: `filter`, `indexed`, `notIndexed`, `total`, `lastUpdated` (`asOf` is the same), `stale`, `staleReasons`, `warning`, and each not-indexed reason (`reason`, `source`, `pages`, `examples` with `lastCrawled`, `examplesAvailable`, `examplesTruncated`); with `--indexed`, also `indexedExamples`, `indexedExamplesAvailable`, `indexedExamplesTruncated`. `--examples` default 5, max 1000, 0 skips them |
| `webcli gsc inspect URL [--property P] [--check-live-sitemap]` | URL Inspection's indexed data: `onGoogle`, `verdict`, `coverageState`, `lastCrawl`, `crawledAs`, `crawlAllowed`, `pageFetch`, `userCanonical`, `googleCanonical`, `inSitemap`, `sitemaps`, `referringUrls`, `notes`; with `--check-live-sitemap`, also `submittedSitemaps`, `inLiveSitemap`, `liveSitemaps`, `liveSitemapsRead`, `liveSitemapUrls`, `liveSitemapErrors` |
| `webcli gsc performance PROPERTY [--start D] [--end D] [--dimension query\|page\|date] [--limit N]` | web search over the range: `--json` gives `{property, searchType, start, end, dimension, lastUpdated, totals: {clicks, impressions, ctr, position}, rows, rowsTruncated}`; rows hold `clicks`, `impressions`, `ctr` (0..1), `position` per query, page, or day. Default the 28 days ending yesterday, `--dimension query`, `--limit 25` (max 1000) |
| `webcli gsc sitemaps PROPERTY` | submitted sitemaps: `status`, `submitted`, `lastRead`, `discoveredPages`, `errors`, `warnings` |
| `webcli gsc sitemaps submit PROPERTY SITEMAP_URL --yes` | WRITE: submit one sitemap, then read the list back |
| `webcli gsc inspect URL --request-indexing --yes` | WRITE: URL Inspection's REQUEST INDEXING |

Every command takes `--json`; the default on a terminal is a table. PROPERTY is `sc-domain:example.com`, a bare
`example.com` (the Domain property), or a URL-prefix property such as `https://www.example.com/`. `inspect`
without `--property` uses the property that covers the URL (the longest URL-prefix one, else the Domain one).

## The Pages report lags URL Inspection

The Pages (Page indexing) report is not live. Google recomputes it every few days, so its counts often trail URL
Inspection by days: a URL the report lists as "Discovered - currently not indexed" can already be "Submitted and
indexed" in `inspect`. For one URL, trust `inspect`. The report is a trend, not the current state.

- `lastUpdated` is the report's own "Last update" date: the last day in its chart, in Search Console's reporting
  time zone (Pacific). `asOf` is the same value. Before 0.18.1, `asOf` was the UTC date, one day later.
- `stale` is true when `lastUpdated` is more than 3 days old, or when an example URL (not-indexed or, with
  `--indexed`, indexed) was crawled after `lastUpdated`. `staleReasons` says which. `warning` is one line of
  text, also printed on stderr.
- Example URLs are examples. Search Console lists at most 1000 per reason (and 1000 indexed), and webcli can't
  get a full URL list. `examplesAvailable` is how many Google listed, and `examplesTruncated` is true when fewer
  are shown than the reason's `pages`.

`--sitemap all` limits the report to pages in the submitted sitemaps (Search Console's "All submitted pages").
`--sitemap SITEMAP_URL` limits it to one submitted sitemap; a URL that isn't submitted exits 2 and lists the
submitted ones. Both use Search Console's own filter, so "X of N sitemap URLs indexed" is `indexed` of `total`
under that filter. N counts the sitemap URLs Google knows about, which can be fewer than the live sitemap holds
until Google re-reads it. With `--indexed`, `indexedExamples` lists the indexed URLs themselves.

## inspect: Google's stored data, passed through

`inSitemap`, `sitemaps`, and `referringUrls` are Google's stored data for the URL, unchanged. They can disagree
with the live sitemap. A URL can be "Submitted and indexed" with `inSitemap: false` and `sitemaps: []`, and Google
sometimes lists the sitemap itself under `referringUrls`. `notes` (always present, often `[]`) points these cases
out.

`--check-live-sitemap` also reads the property's submitted sitemaps from the site as they are now. It uses a plain
anonymous HTTPS GET, with no Google session and no cookies, and only fetches URLs inside the property. It follows
sitemap indexes (up to 50 files), reads gzip, and caps each file at 20 MB with a 20 s timeout. The result is
`inLiveSitemap` (null when no sitemap could be read), `liveSitemaps` (the files that list the URL), and
`liveSitemapErrors`.

## Performance

`totals` are Search Console's own totals for the whole range, as the Performance page's cards show them; they
include anonymized queries. `ctr` is clicks / impressions. `position` is null when there are no impressions.
`lastUpdated` is when Search Console's performance data was last updated. In a table, the totals go to stderr.

Performance rows: query and page rows are the top N by clicks, then impressions; date rows are the latest N days,
oldest first. `position` is null on a day with no impressions. Search Console's data lags about two days and
leaves out anonymized queries, so a small site can have impressions and no query rows. Then the rows are
`[]`, with a `note` and the totals. Exit 3 means no impressions at all in the range.

## What Google doesn't expose

- A full list of URLs per indexing reason: there are only example lists, capped at 1000, and no separate total
  in that answer.
- A separate "computed at" time for the Pages report: its last chart day is all there is.
- Anonymized queries as rows: they are counted in `totals` only.
- An "unsubmitted pages only" filter through this route: Google returned an error page for it.
- Live sitemap membership in `inspect`: `inSitemap` and `sitemaps` are stored data, which is why
  `--check-live-sitemap` exists.

```sh
$ webcli gsc inspect https://example.com/ --json
{"url": "https://example.com/", "property": "sc-domain:example.com", "onGoogle": true,
 "verdict": "URL is on Google", "coverageState": "Submitted and indexed", "lastCrawl": "2026-10-21T11:19:34Z",
 "crawledAs": "Googlebot smartphone", "crawlAllowed": true, "pageFetch": "Successful",
 "userCanonical": "https://example.com/", "googleCanonical": "https://example.com/", "inSitemap": true, ...}
```

## The two writes

Both are gated behind `--yes` (alias `--confirm`). Without it, a write contacts nothing: it prints the plan
(`{"dryRun": true, "write": ..., "wouldDo": ..., ...}`) and exits 2.

- `sitemaps submit PROPERTY SITEMAP_URL --yes` makes the one call the Sitemaps page's SUBMIT makes. The guard lets
  through exactly that call, for that property and that sitemap URL, once. The sitemap URL must be on the
  property. Then the command reads the sitemap list back: `{submitted, listed, row}`. If the submit went out but
  the sitemap isn't listed afterwards, it exits 8 (sent, not verified): check `sitemaps PROPERTY` before you send
  it again.
- `inspect URL --request-indexing --yes` clicks REQUEST INDEXING in Search Console's own URL Inspection page.
  Google mints the request's attestation in the page, so webcli drives the page instead of making the call
  itself, and never sees the attestation. Google runs a live test first, which takes a minute or two. The guard
  allows one indexing request per run, and blocks every other write the page tries. Google's daily quota exits 6.

Every write, refused, attempted, done, or failed, appends one line to `~/.webcli/logs/gsc.log`:

```
2026-10-30T11:02:11 write=sitemaps_submit outcome=refused identity=google:default property=sc-domain:example.com sitemap=https://example.com/sitemap.xml
```

The line holds the local time, write, outcome, property, and URL. It never holds a token or cookie.

## How it stays read-only

A fail-closed request guard (`webcli/sites/gsc/guard.py`) checks every request the page makes. The read policy
lets through:

- page loads and static assets;
- batchexecute calls that carry exactly one RPC from the read list (properties, the index report and its series,
  sitemaps, and the inspection start, poll, and result calls).

The guard checks the `rpcids` label against the RPC in the body, so neither can name a different call. The
inspection start goes through only in indexed-data mode; a live test is never a read. Search Console's own
load-time writes (the recent-properties preference, logging, analytics) are aborted. A challenge exits 5 and
webcli stops: an HTTP 403 or 429, Google's `/sorry/` "unusual traffic" page, or a captcha.

| RPC | Command |
|---|---|
| `BWF8he` | properties |
| `OLiH4d` | pages (daily indexed / not-indexed series, view 30), performance totals (view 27) |
| `nDAfwb` | pages (reasons, view 35; example and indexed URLs, view 33), performance rows (view 32) |
| `vyvePb` | sitemaps |
| `RVtklb` (mode 2), `MrNfbc`, `HZPzjb` | inspect |
| `CyYwI` | sitemaps submit (write, `--yes`) |
| `Fj3Owf` | request indexing (write, `--yes`, sent by the page) |
