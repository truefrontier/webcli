# Changelog

## 0.6.0 (2026-10-21)

### Added

- `webcli messages read QUERY --image-out DIR`: saves each image in the messages read (after `--limit`) full size, as `DIR/<conversation id>-<n>.<jpg|png|gif|webp>` (n: the image's place among the conversation's images, so names don't change with `--limit`; a file of that name is replaced), and every row gains `images`, its saved paths (`[]` for none). For each image it opens the app's full-size viewer and reads the decrypted image out of the page (a phone photo comes out at its full 3024x4032, not the 450px thumbnail); if the viewer doesn't load, the thumbnail is saved. An image that doesn't load at all is left out with a warning on stderr; the exit is still 0. Without `--image-out`, `read` is unchanged (`[image]`, no `images` field).
- `webcli youtube playlist create --title T [--privacy private|unlisted|public] VIDEO...`: one new playlist of exactly those videos (ids or youtu.be / watch / shorts / embed / live links), in that order, on the signed-in channel. A dry run unless `--confirm` (alias `--yes`): the dry run prints the plan and opens no browser. A confirmed create reads the playlist back and exits 0 when it shows exactly the list, 8 (`sent_unverified`, with the playlist's id and url when YouTube gave one) when the create went out but couldn't be verified, and 5 only when nothing went out. Never retried. `--privacy` defaults to private. A bad video or one given twice exits 2.

### Security

- The messages guard passes one more request, and only on a page opened with the core guard's download permission (only `read --image-out` opens one): the app's encrypted-attachment download, a GET (no query) to exactly `https://instantmessaging-pa.googleapis.com/upload`. Without it every image stays a spinner. POSTs there (uploads) stay blocked with or without the permission, and download allows nothing else. Reading still leaves the conversation unread (MESSAGE_READ stays blocked). Saving reads `blob:` URLs of the app's own origin, which make no request.
- The YouTube guard passes one write, and only for `playlist create --confirm`: a single POST to exactly `https://www.youtube.com/youtubei/v1/playlist/create`, only while webcli's own call is in flight on a page holding the core guard's send permission, and only when its JSON body is exactly `{context, title, privacyStatus, videoIds}` with that command's title, privacy, and video list (same order; a key given twice or a NaN fails). The first one uses the scope up. Every other YouTube write (likes, subscriptions, comments, adding to or deleting a playlist, history pause or clear) stays blocked. webcli's own youtubei calls now use `redirect: 'error'`.

## 0.5.2 (2026-10-21)

### Fixed

- `webcli linkedin messages history` failed with exit 5 ("Voyager messages failed (HTTP 400) for .../messaging/conversations/<id>/events") for every thread. LinkedIn's messaging page uses two GraphQL queries both named `messengerMessages`: a sync-token one that takes only `conversationUrn`, and an anchor one that takes `deliveredAt`, `conversationUrn`, `countBefore`, `countAfter`. The queryId sniffer keyed them by name, so the anchor queryId (opened last, on the thread page) replaced the sync-token one in `query_ids.json`; called with only `conversationUrn` it answers 200 with no messages, and the fallback, the retired REST `/events` path, answers 400. The anchor query is now kept as `messengerMessagesByAnchor` and called with its own variables (newest `--limit` messages, so `--limit` above 20 works); the sync-token query and limsg's default follow; the REST `/events` fallback is gone. A bare thread id now builds the full `urn:li:msg_conversation:(<mailbox>,<thread>)` URN. A 429 exits 6. Still read-only: no guard change.
- `webcli linkedin messages list`: every `preview` was empty. A conversation now lists its last message as a reference (`messages.*elements`), not inline; the preview reads that message's text.

## 0.5.1 (2026-10-21)

### Fixed

- `webcli linkedin posts search`: results were stuck at the first ~6 cards, newest-first spam. The search page scrolls inside its own container (`main#workspace`), not the window, so scrolling the window never reached the results list's "load more" and only the first auto-loaded page (3 + 3 cards) was ever read; and the search asked LinkedIn for Latest (date posted), which for broad queries is the last few minutes of posts, mostly job ads and spam. It now scrolls the results list (the same read-only "load more" request the guard already allowed; no guard change) and defaults to LinkedIn's relevance ("Top") order.
- `posts search` is never silently empty: no rows exits 3 with the reason on stderr (LinkedIn found nothing, every result was older than `--since`, or no result cards rendered). When LinkedIn pauses search on the account ("unusual search traffic ... search activity on your account has been paused") or shows a search limit, it exits 6 and says so; a security check exits 4. Nothing is worked around.
- `webcli linkedin feed`: stopped after the first page whenever LinkedIn answered it one update short (it usually does), so `--since 48h` found ~12 posts. It now pages on (read-only Voyager GETs, paced) until `--limit` posts are in hand inside `--since`, the feed ends, or 200 feed updates were read. The 50-row cap is unchanged.

### Added

- `posts search --sort relevance|top|latest` (default relevance; `top` is relevance; `latest` is date posted, newest first).
- `posts read --comments N` (default 10, 0 for none, at most 50): `top_comments`, the post's top-level comments in LinkedIn's "Most relevant" order, each `{author_name, author_handle, author_headline, posted_at, text, reactions, replies, pinned, url}`, from the same single read-only GET.

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
