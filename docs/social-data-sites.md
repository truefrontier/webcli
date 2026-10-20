# Social data sites: linkedin, facebook, loom

All three sites are built, tested offline, and registered. Each has its own identity, and a person signs each one in once. LinkedIn has run live (read-only); Facebook and Loom wait on a new sign-in.

Only LinkedIn can write, and only two verbs: `messages send` and `post create`. Both are dry runs unless `--confirm`, and both refuse to write unless the signed-in account is the expected author (`kevnk` by default). Facebook and Loom are read-only.

## What ships

| Site | Verbs | Source | Identity | How it reads |
|---|---|---|---|---|
| `webcli linkedin` | `messages list`, `messages history`, `messages read`, `activity comments`, `activity reactions`, `activity posts`, `activity export`, `feed`, `posts search`, `posts from`, `posts read`, `boost list`, `boost stats`, `status`; writes: `messages send`, `post create` | linkedin-messages-cli (limsg) | `linkedin:default` | In-page Voyager and Campaign Manager GETs; Recent activity, post search, and company pages read from the rendered cards |
| `webcli facebook` | `sync`, `search`, `about`, `enrich`, `export`, `status` | facebook-contacts-cli (fbc) | `facebook:default` | Full page loads read from the DOM; scrolling the Friends tab |
| `webcli loom` | `list`, `info`, `transcript`, `status` | loom-cli | `loom:default` | Loom's own GraphQL queries in the page; the transcript file the share page fetches |

All three:

- are headless only: no `--headed` flag, and an op called with `headed=True` still opens headless Chrome;
- exit 4 without starting Chrome when their identity has no profile yet;
- sit behind a fail-closed request guard that lists only their own hosts;
- add `agent-help` (commands, flags, exit codes, notes).

## Sign in (a person, once)

```sh
webcli identity login linkedin:default   # sign in as kevnk: writes refuse any other account
webcli identity login facebook:default
webcli identity login loom:default
```

Each login opens a visible Chrome window with no request guard, like Muse's: sign-in may cross Google, Apple, Atlassian, SSO, a checkpoint, or two-factor. Each finishes only once the site's own session works:

- **LinkedIn:** `GET /voyager/api/me` returns a profile.
- **Facebook:** Facebook's session cookies `c_user` and `xs` are set (checked by name only), and a tab off the login, checkpoint, and two-factor pages reads as signed in from `/me`. Then the window closes, and a fresh hidden Chrome on the profile must read as signed in too.
- **Loom:** a tab shows the library or settings, and Loom's API answers the library query (`GetLoomsForLibrary`, what `list` runs) as signed in. A tab can reach `/looms` before Loom has finished signing in, so the URL alone doesn't count, and neither does a public share page. Then the window closes, and a fresh hidden Chrome on the profile must be signed in too.

If the fresh hidden Chrome disagrees, login exits 4 and says what it saw, instead of reporting a sign-in that every later command would reject.

Facebook used to share Muse's `meta:default`. That profile holds Facebook's device cookies (`datr`, `sb`) from Muse's Meta sign-in, but never `c_user` or `xs`, so `webcli facebook status` stayed signed out. Muse stays on `meta:default`.

## LinkedIn writes: the author check

LinkedIn's send (`createMessage`) and post (`normShares`) calls name no author. They go out as whichever account the Chrome profile is signed in to. So both writes check that account first, exactly as limsg's `post create` did. limsg checked only posts; webcli checks sends too.

**The expected author** is the first of these that is set:

1. `--expect-author HANDLE`
2. `WEBCLI_LINKEDIN_EXPECT_AUTHOR`
3. `expect_author` under `[sites.linkedin]` in `~/.webcli/config.toml`
4. `kevnk`

The signed-in handle (the `HANDLE` in `linkedin.com/in/HANDLE`) must equal it, ignoring case. The author is the profile `GET /voyager/api/me` points to (`data["*miniProfile"]`), never just the first profile in the response.

**What each run does:**

| Run | Reads | Writes | Exit |
|---|---|---|---|
| No flag, or `--dry-run`, or `--dry-run --confirm` | thread (send), `/me` | none | 0; 4 with a `WARNING: AUTHOR MISMATCH` if the account is wrong |
| `--confirm`, wrong account | thread (send), `/me` | none (`sent`/`posted` is false) | 4, `REFUSED: ...` |
| `--confirm`, expected author | `/me`, then `/me` again right before the last write | send: one `createMessage`; post: register, upload, `normShares` | 0 |
| `--confirm`, account changed before the last write | as above | everything before the last write; never the send or the create | 4, `REFUSED: the signed-in account changed ...` |

The re-check compares the whole account: handle, profile urn, and member urn.

**The guard binds each confirmed write.** Reads may only GET. A confirmed command's page also gets a one-command write scope, used only while that page holds the guard's send permission:

- send: one `POST createMessage` whose `conversationUrn`, `mailboxUrn` (the sender), and text are the confirmed ones, to an existing thread;
- post: one register (`IMAGE_SHARING`, the image's size and name), one `PUT` (and its CORS preflight) to exactly the upload URL register returned, and one `normShares` whose text is the confirmed one and whose only image is the asset register returned.

No slot opens until the author check has passed, and each slot passes once. Anything else the page sends is blocked, and a write the guard blocks says "Nothing was sent/posted." Writes are sent once, never retried, and never follow a redirect. Both run on `https://www.linkedin.com/robots.txt`, so no LinkedIn app script runs beside them.

**When a write fails**, the message says whether anything can be live:

- refused by the author check, blocked by the guard, or a 4xx: nothing was sent or posted;
- a 5xx or no response on the last write: it may be live; check the thread or your profile before retrying;
- a post accepted (2xx) with no post urn: it is live; exit 0 with a warning not to re-run.

## Read-only guarantees

**Facebook** never clicks, types, or submits. `tests/test_facebook.py` keeps fbc's static check: no click, type, press, fill, submit, focus, hover, or dispatch call anywhere in `webcli/sites/facebook/` or the core modules its commands run through, and no selector names an action control. The guard allows:

- GET/HEAD over https on `facebook.com`, its subdomains, and `*.fbcdn.net` (its static and image CDN), never GraphQL by GET and never sign-out;
- `POST www.facebook.com/api/graphql/` only for the running command's read queries, named like a query: `sync` gets the Friends-tab pagination query, About pages get none.

Every other request is aborted: friending, reactions, comments, messages, "seen" markers, telemetry, and every other host. WebSockets are closed.

**Loom** lets a GraphQL request through only when every operation in it is a query. The guard reads the document's top-level definitions (skipping strings and comments); a mutation, a subscription, a persisted query sent without its text, or a document it can't read is blocked. Everything else is GET-only on `loom.com` and its subdomains.

**LinkedIn reads** may only GET on `linkedin.com`, its subdomains, and `*.licdn.com`, never with a Rest.li `?action=` and never to sign-out. Opening a thread (`messages history`) is read-only: the page's own mark-as-read is blocked.

**LinkedIn feed and posts** (`feed`, `posts search`, `posts from`, `posts read`) run under the same read-only guard; no write scope ever opens for them. `posts search` adds one exception, and only to its own page: the search page's request for the next results, `POST www.linkedin.com/flagship-web/rsc-action/actions/pagination`, with no query keys but `parentSpanId`/`sduiid` and a JSON body of exactly `{pagerId, clientArguments, paginationRequest}` naming the content-search pager on the search-results screen. Every other POST the page sends (reactions, follows, telemetry, its other server actions) is blocked. Each run logs one redacted line to the daemon log: `guard linkedin.<op>: writes allowed 0; blocked N (M non-GET)`, plus the number of pagination reads let through for search.

| Command | Source | Order | Notes |
|---|---|---|---|
| `feed [--since] [--limit] [--ranked]` | Voyager GraphQL main feed (`sortOrder:MEMBER_SETTING`) | Newest first, sorted here | LinkedIn's feed API has no Recent order (only ranked). The posts read (limit + 10, or 50 with `--since`) are sorted by time; `--ranked` keeps LinkedIn's order. Ads, promos, and carousels are left out. |
| `posts search QUERY` | The search page's rendered cards, plus the urns in the page's own payload | Newest first | LinkedIn's `sortBy=date_posted`, narrowed to past-24h / past-week / past-month when `--since` fits, then cut at `--since` here. Counts only as the card shows them (else null). A card whose urn can't be matched keeps url/urn null and an approximate posted_at from its "3h" label. |
| `posts from HANDLE_OR_URL` | Members: the profile's Voyager share feed (REST fallback). Companies: the posts page's cards | Newest first | Company pages give only their first rendered cards (2-3): their pagination is a POST the guard blocks. |
| `posts read URL_OR_URN` | Voyager `feed/updatesV2` | One object | `urn:li:activity|share|ugcPost:N`, or a `/feed/update/` or `/posts/` URL. |

Rows: `url, urn, author_name, author_handle (vanity, or the company page URL), author_headline, posted_at (ISO 8601 UTC from the post id; null if unknown), text, reactions, comments, reposts (null when not shown: never a guess), kind (post|repost|article), original_url (a repost's original)`. `--limit` defaults to 20 and is clamped to 50; no command reads more than 50 posts, and scrolling pauses about 1.5 s between pages. `--since` takes ISO 8601 or a relative time (`30m`, `48h`, `2d`, `1w`); `activity` and `boost` take relative times too.

## Known limits of the guards

These are deliberate trade-offs. Each one needs a script running in the command's own page, or a second process signing the profile in or out mid-command:

- **Facebook trusts the operation label.** Facebook's server runs the `doc_id`; the guard reads the `fb_api_req_friendly_name` that Facebook's own client puts next to it. A hostile page script could label a write as an allowed read. fbc made the same call: doc_ids rotate, so they can't be listed.
- **The LinkedIn upload is bound to its URL, not its bytes.** The register body, the upload URL, and the post's asset and text are all bound. Both writes run on robots.txt, where no page script runs.
- **The author is re-read, not pinned.** A session swap after the last `/me` read and before the write would still go out as the new account (limsg's limit too). The daemon runs one command at a time on a locked profile, so nothing else can sign that profile in or out mid-command.
- **Redirects.** Playwright routes only the first URL of a redirect chain. LinkedIn's writes use `redirect: 'error'`, so they never follow one.

## What changed from the source tools, and why

- **Per-site `login` and `--headed`:** dropped. Use `webcli identity login <identity>`.
- **Profiles and caches:** start fresh under `~/.webcli/`. Nothing is copied from `~/.linkedin-messages-cli`, `~/.facebook-contacts-cli`, `~/Projects/TRUE-fb-contacts`, or `~/.loom-cli`. The Facebook cache is `~/.webcli/sites/facebook/friends.sqlite`; exports go to `~/.webcli/sites/facebook/exports/` (0600).
- **Environment and config names:** `LIMSG_EXPECT_AUTHOR` is now `WEBCLI_LINKEDIN_EXPECT_AUTHOR`; limsg's `~/.linkedin-messages-cli/config.toml` key is now `[sites.linkedin] expect_author` in `~/.webcli/config.toml`. `FBC_TVLY` is now `WEBCLI_TVLY`. `FBC_DATA_DIR` and `FBC_LOCK_WAIT` are gone (`WEBCLI_HOME`, `--lock-wait`).
- **`messages send` checks the author** (limsg's send did not), and its flags are `--confirm`, `--yes`, or `--send`. A dry run's stdout is only JSON; the notes go to stderr.
- **`messages send` text** must not be empty (limsg sent an empty string).
- **The DM sender (`mailboxUrn`)** is the signed-in account from `/me`, not the most frequent profile id in the page's HTML.
- **Voyager 401/403 in messaging** exits 4 (sign-in), not 5.
- **A bad `--since` on `activity`** exits 2 (limsg crashed).
- **Conversation `time`** is ISO 8601 UTC, like message times (limsg printed epoch milliseconds).
- **Thread ids keep their `==` padding** in `messages list` (limsg cut it off there but kept it elsewhere).
- **limsg's scrubbed response captures** (`~/.linkedin-messages-cli/capture/`) are gone. Only the messaging queryIds the page uses are kept, in `~/.webcli/sites/linkedin/query_ids.json`. `python -m webcli.core.capture linkedin` records a page's traffic when something breaks.
- **`boardy-watch/`** was never written by limsg's code; nothing replaces it.
- **Config errors** (an unreadable `config.toml`) exit 7, webcli's code, not limsg's 2.
- **Facebook GETs** are limited to Facebook's own hosts. fbc allowed GET to any host.
- **Loom's GraphQL** is now guarded (loom-cli had no guard).

## Unverified until the first live run

Checked live on 2026-10-19 (read-only, headless, under `window_check`): `linkedin:default` is signed in. `webcli linkedin status` and `webcli linkedin messages list` both exit 0, with 0 windows and no focus change. Before the fix, `status` exited 4 whenever it was the first command on a fresh Chrome. The cause: LinkedIn's csrf token is the JSESSIONID cookie, a session cookie, so a fresh Chrome has none, and robots.txt (where `status`, `boost`, and both writes run) never sets one. Those commands now load the messaging page first when the cookie is missing, as every messaging read already did.

Checked live on 2026-10-20 (read-only, headless, `linkedin:default`): `feed --since 48h --limit 5`, `posts search "AI agents" --since 48h --limit 5`, `posts from <handle> --limit 3`, a company page, and `posts read` on a search result's URL all returned rows with every field; each run's guard line read `writes allowed 0`. LinkedIn's feed, search, and activity pages are server-driven (their own data calls are POSTs the guard blocks), and its Voyager API has no Recent feed order and no content search: hence the client-side feed sort, and search read from its cards and its page payload.

Checked live on 2026-10-19 (read-only, headless): `meta:default` holds a Muse session but no facebook.com session, which is why facebook moved to `facebook:default`. A person runs `webcli identity login facebook:default` before the first `sync`.

Loom, checked 2026-10-19 (headless, read-only; cookie names only): after a Google sign-in through Atlassian, `loom:default` holds Loom's `connect.sid` and `cloud.session.token`, yet `webcli loom list` is still sent to `/login`. Loom's OAuth state cookies were still there, unused, so the sign-in most likely stopped before Loom's callback finished: the old login closed Chrome as soon as a tab's URL started with `/looms`. `loom status` now runs the same library query `list` does, and Loom's GraphQL `UNAUTHENTICATED` exits 4 like a redirect to `/login`. A person runs `webcli identity login loom:default` again.

These come from the source tools and were never checked under webcli's guard:

- LinkedIn: whether the post's upload URL needs a CORS preflight (allowed either way);
- Facebook: whether any page needs a host beyond `facebook.com` and `*.fbcdn.net`;
- Loom: which host serves the transcript file (assumed `*.loom.com`), and whether the share page fetches it through GraphQL queries only.

Each guess fails closed: a blocked request is named in the error (or in `sync`'s `blocked` list), and nothing is written.
