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
| `webcli gsc pages PROPERTY [--examples N]` | the Page indexing report: `indexed`, `notIndexed`, `asOf`, and each not-indexed reason (`reason`, `source`, `pages`, `examples` with `lastCrawled`); `--examples` default 5, max 100, 0 skips them |
| `webcli gsc inspect URL [--property P]` | URL Inspection's indexed data: `onGoogle`, `verdict`, `coverageState`, `lastCrawl`, `crawledAs`, `crawlAllowed`, `pageFetch`, `userCanonical`, `googleCanonical`, `inSitemap`, `sitemaps`, `referringUrls` |
| `webcli gsc performance PROPERTY [--start D] [--end D] [--dimension query\|page\|date] [--limit N]` | web-search `clicks`, `impressions`, `ctr` (0..1), `position` per query, page, or day; default the 28 days ending yesterday, `--dimension query`, `--limit 25` (max 1000) |
| `webcli gsc sitemaps PROPERTY` | submitted sitemaps: `status`, `submitted`, `lastRead`, `discoveredPages`, `errors`, `warnings` |
| `webcli gsc sitemaps submit PROPERTY SITEMAP_URL --yes` | WRITE: submit one sitemap, then read the list back |
| `webcli gsc inspect URL --request-indexing --yes` | WRITE: URL Inspection's REQUEST INDEXING |

Every command takes `--json`; the default on a terminal is a table. PROPERTY is `sc-domain:example.com`, a bare
`example.com` (the Domain property), or a URL-prefix property such as `https://www.example.com/`. `inspect`
without `--property` uses the property that covers the URL (the longest URL-prefix one, else the Domain one).

Performance rows: query and page rows are the top N by clicks, then impressions; date rows are the latest N days,
oldest first. `position` is null on a day with no impressions. Search Console's data lags about two days and
leaves out anonymized queries, so a small site can have impressions and no query rows (exit 3, no results).

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
| `OLiH4d` | pages (daily indexed / not-indexed series) |
| `nDAfwb` | pages (reasons, view 35; example URLs, view 33), performance (view 32) |
| `vyvePb` | sitemaps |
| `RVtklb` (mode 2), `MrNfbc`, `HZPzjb` | inspect |
| `CyYwI` | sitemaps submit (write, `--yes`) |
| `Fj3Owf` | request indexing (write, `--yes`, sent by the page) |
