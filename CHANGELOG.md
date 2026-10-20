# Changelog

## 0.5.0 (2026-10-20)

### Added

- `webcli linkedin feed [--since] [--limit] [--ranked]`: your home feed's posts, read-only. LinkedIn's feed API offers no Recent order, so the posts read are sorted newest first here; `--ranked` keeps LinkedIn's order. Ads, promos, and carousels are left out.
- `webcli linkedin posts search QUERY [--since] [--limit]`: the latest posts matching a query (LinkedIn's date-posted sort, narrowed to past-24h / past-week / past-month when `--since` fits, then cut at `--since`), newest first.
- `webcli linkedin posts from HANDLE_OR_URL [--since] [--limit]`: one member's recent posts and reposts (handle or `linkedin.com/in/` URL), or a company page's first rendered posts (`company/NAME` or its URL).
- `webcli linkedin posts read URL_OR_URN`: one post as an object.
- Post rows: `url, urn, author_name, author_handle, author_headline, posted_at, text, reactions, comments, reposts, kind, original_url`. Counts are null when LinkedIn doesn't show them, never guessed. `posted_at` is ISO 8601 UTC, from the post id.
- All four take `--format table|md|csv|json`, `--compact`, `--select`, `--csv`, `-q`. `--limit` defaults to 20 and is clamped to 50; no command reads more than 50 posts.
- `--since` (and `--until` where it exists) on every LinkedIn command now also takes relative times: `30m`, `48h`, `2d`, `1w`.

### Security

- The new commands are read-only and run under the existing fail-closed guard: GET/HEAD on LinkedIn hosts, no Rest.li `?action=`, no write scope. `posts search` alone also passes its page's own "next results" request, bound to the exact pagination path, query keys, and body shape for the content-search pager. Each run logs one redacted guard line (`writes allowed 0; blocked N`).
- The lists of writes and of things webcli never does are unchanged.

## 0.4.1

- Earlier releases: see the git history.
