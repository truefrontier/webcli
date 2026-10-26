# Feeds: `webcli perplexity`, `webcli gnews`, `webcli youtube subs|uploads|home`, and `webcli feed`

Read-only adapters for the feeds you read, plus `webcli feed`, which pulls them all into one normalized file on a
schedule and can push that file to a server you read it from.

## Signed in first, public fallback (the shared default)

Every feed adapter reads as its signed-in identity first. It falls back to that site's public, signed-out feed only
when the identity is missing or expired, and when it does:

- every row says `auth: "public"` (otherwise `"signed_in"`);
- stderr says how a person signs in (`a person runs: webcli perplexity login`);
- in a `webcli feed` file, that site's `sources` entry says `auth: "public"`, `needs_login: true`, and its `hint`.

A site with no public feed (YouTube subscriptions and home, LinkedIn) exits 4 when signed out, and `webcli feed`
records it as `{site, status: "needs_login", hint}`. webcli never signs in by itself: sign-in opens a visible
Chrome window for a person.

| Site | Identity | Signed out |
|---|---|---|
| perplexity | `perplexity:default` (`webcli perplexity login`) | public Discover feed (`auth: "public"`) |
| gnews | `google:default` (`webcli identity login google:default`) | public Google News home / section pages |
| youtube subs, uploads, home | `google:default` | exit 4, needs_login |
| linkedin feed | `linkedin:default` | exit 4, needs_login |

A challenge (Cloudflare, Google's "unusual traffic" page, HTTP 403/429/503) stops the command with exit 5. webcli
does not work around it.

## `webcli perplexity status | discover | login`

```sh
webcli perplexity status --json                 # {signedIn, auth, name, email, discover, topics}
webcli perplexity discover --limit 30 --json    # Discover's Top feed
webcli perplexity discover --topic tech --limit 10
webcli perplexity login                         # a person, once: visible window
```

- Endpoints (GET only; the guard refuses everything else, so no query, like, follow, or bookmark can leave):
  `/discover` (the page), `/rest/discover/feed?limit=20&offset=N&topic=<top|uuid>&version=2.18&source=default`,
  `/rest/discover/topics` (topic slug to uuid), `/api/auth/session` (signed in or not).
- Topics: `top`, `tech`, `science` (Perplexity files it under Tech & Science, so it reads `tech`), `finance`,
  `arts`, `sports`. Pages of 20, 1.5 s apart, deduped; `--limit` 1 to 100 (default 30).
- Row: `source` "perplexity", `id`, `url` (the first source article; else the card image), `title`, `author`,
  `publishedAt` (UTC), `snippet`, `thumbnail`, `metrics` {views, likes, forks, sources}, `perplexityUrl`, `topic`,
  `auth`. Signed out, the social counts are 0.

## `webcli gnews status | foryou`

```sh
webcli gnews status --json                      # {signedIn, email}
webcli gnews foryou --limit 30 --json           # For you, in page order
webcli gnews foryou --section technology --limit 10
```

- Reads news.google.com/foryou as rendered (server-side) in the hidden Chrome. The guard passes GETs only, so the
  page's RPCs (follow, hide, "fewer like this", save) never leave; stories render anyway.
- Sections: `foryou`, `us`, `world`, `local`, `business`, `technology`, `entertainment`, `sports`, `science`,
  `health` (each the page's own nav link).
- Row: `source` "google_news", `id` (the story token), `url` / `newsUrl` (the news.google.com/read link: the
  publisher URL is not resolved, which would cost a request per story), `title`, `author`, `publisher`,
  `publishedAt` (UTC), `snippet` (null), `thumbnail`, `metrics` {}, `section`, `auth`. `--limit` 1 to 60.

## `webcli youtube subs | uploads | home`

```sh
webcli youtube subs --json                      # every channel you subscribe to
webcli youtube uploads --since 48h --limit 50   # new videos from your subscriptions, newest first
webcli youtube home --limit 50                  # your home recommendations
```

- Shorts are left out by default (Shorts shelves, Short items, /shorts/ URLs); `--include-shorts` keeps them.
- Read-only InnerTube `browse` calls (FEchannels, FEsubscriptions, FEwhat_to_watch, and their continuations, 1 s
  apart, at most 10 pages). No like, subscribe, "not interested", or history write.
- uploads skips YouTube's "Most relevant" shelf (its videos are in the newest-first grid too) and stops at the
  first video older than `--since` (48h, 3d, 1w, or an ISO date).
- `publishedAt` is approximate (from "2 days ago"); `publishedText` keeps YouTube's words. Live and upcoming
  videos have none.

## `webcli feed pull | show | schedule | push`

```sh
webcli feed pull                                # all configured sites -> ~/.webcli/feeds/YYYY-MM-DD-HHMM.json
webcli feed pull --sites youtube,gnews --limit-per-site 10 --out /tmp/feed.json
webcli feed pull --x-file ~/path/x-2026-01-02.json   # merge an X export (never read live)
webcli feed show                                # the latest file's time, count, and sources
webcli feed schedule install --at 05:30,12:30 --no-load   # write the LaunchAgent only
webcli feed schedule status
webcli feed push --to https://feeds.example.ts.net/api/feed
```

`pull` runs each site's own read command (`webcli <site> <read> --format json`) one site at a time, as a child
process, and writes one file atomically (0600, in a 0700 directory):

| Site | Reads |
|---|---|
| youtube | `uploads --since 48h` + `home` (no Shorts), merged |
| gnews | `foryou` |
| perplexity | `discover` (Top) |
| linkedin | `feed` (at most 50) |
| x | `--x-file` only: a `newsfeed.items/1` file (missing: skipped and logged) |

A site that fails, or is signed out with no public feed, is skipped and logged; the file is still written with what
came back, and `pull` exits 5 (partial). A public fallback returned rows, so it is not partial (exit 0).

The file:

```json
{
  "schema": "newsfeed.items/1",
  "generated_at": "2026-01-02T11:30:00Z",
  "source": "webcli feed",
  "sources": [
    {"site": "youtube", "status": "ok", "auth": "signed_in", "needs_login": false, "count": 40, "reads": [...]},
    {"site": "perplexity", "status": "ok", "auth": "public", "needs_login": true, "count": 20,
     "hint": "webcli perplexity login", "reads": [...]},
    {"site": "linkedin", "status": "needs_login", "auth": null, "needs_login": true, "count": 0,
     "hint": "webcli identity login linkedin:default", "reads": [...]}
  ],
  "items": [
    {"source": "youtube", "native_id": "abc", "url": "https://www.youtube.com/watch?v=abc", "title": "...",
     "text": null, "author": {"name": "...", "handle": null, "profile_url": "https://www.youtube.com/@..."},
     "published_at": "2026-01-02T09:00:00Z", "metrics": {"views": 1234, "durationSec": 600}, "links": [],
     "media": [{"type": "image", "url": "..."}],
     "meta": {"via": "webcli youtube uploads", "network": "youtube", "pick_slot": null, "why_it_fits": null,
              "auth": "signed_in", "reads": ["uploads", "home"]}}
  ]
}
```

`status` is `ok`, `partial` (one of a site's reads failed), `needs_login`, `error`, or `skipped`. `source` on items
is `youtube`, `google_news`, `perplexity`, `linkedin`, or `x`. The same (source, native_id) appears once.

Config, `~/.webcli/feed.toml` (flags override it):

```toml
sites = ["youtube", "gnews", "perplexity", "linkedin"]
limit_per_site = 20

[youtube]
since = "48h"

[x]
file = "~/path/to/x-latest.json"
```

`schedule install` writes `~/Library/LaunchAgents/ai.truefrontier.webcli.feed.plist` (StartCalendarInterval at
`--at`, default 05:30 and 12:30 local; runs `webcli feed pull --quiet`) and loads it unless `--no-load`. `remove`
unloads and deletes it.

`push` HTTP PUTs a file (the latest by default; 5 MB cap; https, or http to localhost). If the server wants a
bearer token, `--token-env NAME` reads it from `$NAME`; it is never printed. Behind Tailscale Serve none is needed:
Serve tells the server which tailnet user is calling.
