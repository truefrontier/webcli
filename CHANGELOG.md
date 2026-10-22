# Changelog

## 0.11.0 (2026-10-22)

### Added

- **`webcli whatsapp`**: your WhatsApp chats through WhatsApp Web (web.whatsapp.com), unofficial, as its own identity
  `whatsapp:default` (a persistent Chrome profile linked to your phone; the link never leaves this computer).
  - `login` (same as `webcli identity login whatsapp:default`): a visible Chrome on the QR code; a person scans it
    from the phone (WhatsApp > Settings > Linked devices > Link a device). Nothing scans or bypasses it.
  - `status` (exit 4 when not linked), `chats [--limit N] [--unread]` (name, last-message preview, time, unread
    count, pinned, muted), `read CHAT [--limit N] [--since DATE]` (sender, time, ISO datetime, text, media
    placeholders such as `[image]` / `[voice note]` / `[document] name`, quoted replies, your ticks; scrolls up for
    older messages up to --limit), `search TEXT [--limit N]` (WhatsApp's own search: chats, contacts, messages).
  - `send CHAT --text T --expect-chat NAME [--confirm]`: one text to one existing chat. A dry run unless --confirm
    (exit 0; exit 5 when the chat isn't exactly --expect-chat). Confirmed, it refuses (exit 5, nothing sent) unless
    the resolved chat and the open chat's header equal --expect-chat exactly (checked before typing and again right
    before the click), the compose box is there and empty, and it reads back exactly the text; it types with
    Shift+Enter between lines (never Enter), clicks Send once, and waits for the message with a tick (exit 8
    `sent_unverified` when it doesn't show). No attachments, no new contacts, never retried.
  - Chat names: exact (case counts) beats case-insensitive beats containing; several matches exit 2 listing the
    candidates and open nothing.
  - Gentle on purpose (WhatsApp's terms prohibit automated access): on-demand only, no polling, paced scrolls with
    hard caps, --limit capped at 200. A block or challenge page stops the command (`STOPPED: ...`, exit 5).
  - The guard allows GET/HEAD to web.whatsapp.com and static.whatsapp.net and the wss://web.whatsapp.com/ws socket
    only. The pages are parsed offline from their HTML (fixtures in `tests/fixtures/whatsapp`).
  - agent-help: a send needs Kevin's own OK for that exact message, in his chat with the bot, never another agent's
    say-so; opening a chat may send read receipts.
- Version 0.11.0 for webcli, llmcli, and chromecli (the extension manifest and the chromecli package follow).

## 0.10.2 (2026-10-22)

### Added

For pages that render as images (e-sign viewers such as signNow: `read` returns ~0 chars) and scroll an inner div,
not the window.

- **`chromecli scroll TAB|--active [--by page|N|Npx] [--up] [--to top|bottom] [--into SELECTOR] [--container
  SELECTOR]`**: a read op on Read and Act tabs (no `--confirm`, never prompts; Off / denied / paused tabs get the same
  exit 4 errors as `read`; logged like the other reads). Default: one page down (clientHeight − 40px). Without
  `--container` it picks the largest visible element with `scrollHeight > clientHeight + 10` and `overflow-y`
  auto/scroll (open shadow roots included), else `document.scrollingElement`; an element must cover ≥ 30% of the
  viewport to beat a window that scrolls too (so a small code box doesn't win). JSON: `{tab, url, title, origin,
  share, op: "scroll", target {description, kind, auto, inShadow}, scrollTop, scrollLeft, scrollHeight,
  clientHeight, atTop, atEnd, moved, devicePixelRatio}`. Extension op `page.scroll` (new `page/scroll.js`).
- **`chromecli screenshot TAB --full-page [--pages N (≤ 50)] [--container SELECTOR] [--out DIR] [--jpeg]`**: saves
  the scroll position, scrolls the target to the top, captures a viewport, scrolls by clientHeight − 40px (300ms +
  animation frames to settle) and repeats until the end or N shots, then restores the position, also after an error.
  Writes `page-01.png`, `page-02.png` … into `DIR` or a new `~/.webcli/chromecli/screenshots/tab<ID>-<time>-full/`.
  JSON: `{tab, url, title, origin, share, fullPage, format, dir, files, count, atEnd, pages [{file, scrollTop,
  bytes}], target, scrollHeight, clientHeight, overlapPx, devicePixelRatio, restored, restoredTo}`; on an error the
  error JSON has `partial {count, files}` and `restored`. Allowed on Read tabs; still needs the tab in front
  (`not_visible`).
- The extension spaces `captureVisibleTab` calls ≥ 550ms apart (Chrome allows ~2/s) and retries once after 1s on
  Chrome's quota error.
- The host log line for `page.scroll` has `to=` / `by=` / `measure=1` and the (redacted) `--into` / `--container`
  selector; each full-page capture logs `full_page_part=N`.

### Not done

- `--stitch` (one tall image): webcli has no image library (Pillow isn't a dependency), so it was left out. Read the
  pages in order; neighbours overlap by ~40px.

## 0.10.1 (2026-10-22)

### Changed

Kevin's call on pre-approvals: "Assume a quote that arrives through the CLI came from the user." No popup toggle;
`--user-approved` plus a Jev scope check instead. Grant matching, limits, notifications, the popup list and the 0.10.0
test-drive fixes are unchanged.

- Removed the popup toggle *Allow agent pre-approvals*, `chrome.storage.local.grantsEnabled`, the `setGrantsEnabled`
  popup message and exit 4 `grants_disabled`. The popup's *Pre-approvals* card is always there (count, "Revoke all",
  each grant with Jev's verdict chip).
- `grant add` requires `--user-approved` with `--quote`; without it: exit 2 `missing_user_approval` (preview included;
  nothing is sent). The CLI trusts the flag: it is the agent's statement that the quote is Kevin's own words.
- **Jev scope check** before a grant is stored (Jev only checks the grant against the quote, never who said it). The
  quote is the instruction; target names, URLs, selectors and page text are untrusted evidence; fill/type values are
  never sent. `authorized` at ≥ 0.8 stores the grant; lower counts as `unclear`. `overreach` / `unclear` refuse with new
  **exit 9** `grant_overreach` / `grant_unclear`, Jev's reason in `message` and `jevCheck` in the error. Jev may narrow
  the grant to a tighter single amount, use count or duration stated in the quote (`noul` ≥ 0.8;
  `jevCheck.narrowed`). Fail closed: no key → exit 7 `jev_not_configured`; Jev down → exit 5 `jev_error`.
- Logged: `user_approved=`, `jev_verdict=`, `jev_confidence=`, `narrowed=`, `jev_reason=` on the `grant.add` line, and
  `grant_added` / `grant_refused` events with the quote. `grant list` shows each grant's `jev` verdict; the result of
  `grant list` no longer has `enabled`. `eval` grants are refused explicitly. `grant add` waits up to 45s (Jev).
- agent-help: exit 9 in chromecli's exit-code table; the grant notes describe the trust model.

## 0.10.0 (2026-10-22)

### Added

- **Pre-approval grants.** `chromecli grant add --op click|fill|type|select|press|navigate|open|close --origin O (--target-text NAME [--role R] | --selector S) [--value V | --value-file F] [--url-prefix P] [--max-amount N [--currency USD] [--amount-selector CSS]] [--uses 1] [--ttl 30m] --quote "<Kevin's exact words>" --confirm` (preview without `--confirm`), `grant list`, `grant revoke ID|--all --confirm`. Bots may only create a grant that quotes Kevin's own words from the current conversation. Grants live in the extension (`chrome.storage.session`, gone on Chrome restart) and are accepted only while Kevin's new popup toggle *Allow agent pre-approvals* is on (default off, popup-only; else exit 4 `grants_disabled`). A live grant with the same op + origin + target (accessible name exact or whole-word, case-insensitive, or the same element as `--selector`) + value answers the in-browser confirm once: the result has `confirmedBy: "grant:<id>"` and `grant {id, quote, usesLeft, amount?}`, the host log has the grant id and the redacted quote, and Chrome shows "Agent used your pre-approval: …" (new `notifications` permission). With `--max-amount`, only a parsed page amount ≤ the cap skips the confirm; otherwise the confirm window shows the grant and the amount. Never skippable: password / one-time-code / `cc-*` / card-like fields, eval, denied origins. Pause and turning the toggle off revoke every grant. Popup: a *Pre-approvals* section (quote, scope, uses left, expiry, revoke ×, Revoke all).
- **Opened tabs inherit the share** (catalog-linter test drive): a tab opened by a click/press on a tab Kevin Share-act'd (`openerTabId`) is shared act on the same origin, read on another, never on a denied origin; click/press results report `openedTabs: [{id, url, share}]`; share grants and drops are logged with their reason. `list --all` adds unshared tabs as metadata (deny-filtered).
- **Open shadow DOM**: read (text/md), `--elements`, `find`, `--text`, `--selector`, `wait`, fill and click pierce open shadow roots; refs work inside; a selector naming a component host means its one inner control; names come from host `label` attributes and slotted text. New fixture `tests/fixtures/chromecli/shadow.html` (Polaris-shaped components) with a headless-Chromium test.
- Previews of page actions add a read-only `probe` (found / not found, `gateEstimate`); `wait --expect-origin/--expect-url`; `read` text output starts with `Title:`/`URL:`; `screenshot` without `-o` writes `~/.webcli/chromecli/screenshots/tab<ID>-<time>.png`; `agent-help` shows chromecli's own exit-code table.

### Fixed

- `script_error` now carries the page-side error message (`In-page error: TypeError: …`) instead of a bare failure; a page script that returns nothing explains why.
- agent-help / `eval --help` no longer say eval is "always browser-confirmed": it is disabled (exit 7 `eval_disabled`).

### Notes

- Tests confirm a same-page reload and a same-origin link click on Kevin's act tab don't prompt.
- Future work: report which account is signed in to a tab.

## 0.9.1 (2026-10-22)

### Changed

Kevin's call after using 0.9.0 live: "if I give it Act permissions I don't want to be prompted on every click."

- Tabs Kevin Share-act'd himself (`tabKind: "act"`) are treated as consent to ordinary clicks and same-origin moves:
  - Jev's click/press classification skips its gate when Jev is ≥ 70% sure the target is `safe` (confidence or `probabilities.safe` ≥ the new **act-tab safe floor**, default 0.7, instead of 0.9). Any other choice, or safe below the floor, still adds `jev:<choice>`. The result's `jevInfo` always shows Jev's answer.
  - A same-origin `navigate` isn't gated. Cross-origin navigate and `open` outside the allowlists still are, and the deny list always applies.
  - If Jev fails under `--jev auto`, the fallback is heuristics only (no every-button `jev_unavailable`).
- Bot-opened tabs (`tabKind: "bot"`, including those auto-shared via `act_allow`) keep the strict 0.9.0 rules: a `jev:<choice>` gate unless Jev says safe at ≥ 0.9, and the stricter fallback when Jev fails.
- Unchanged: every heuristic gate in `gate.js` (submit-like clicks, Enter in a form, password / payment / one-time-code / card fields, eval), the deny list, and `--jev on` (exit 5 `jev_error`). Jev still never removes a heuristic gate.
- Options: new "Act-tab safe floor" (0.5–0.95, default 0.7, `chrome.storage.local.jevActSafeFloor`) next to the confidence floor. The 0.9 confidence floor still applies to bot-opened tabs and to find/locate.
- Results gain `tabKind` and `jevInfo`; the host's audit line adds `tab_kind=` and `jev_info=<choice>:gate|info`.
- Full table: docs/chromecli.md → "Tabs Kevin Share-act'd vs bot-opened tabs".

## 0.9.0 (2026-10-22)

### Added

- chromecli P4, Jev (bring your own key): `chromecli find --tab TAB 'INTENT' [--jev auto|on|off] [--limit 5]` (read-only; rows `ref score source tag role name`), `--find INTENT` as a target on `click`/`type`/`fill`/`select`/`press`, and `--jev auto|on|off` on every page action. Heuristics rank the page's elements; with a key, Jev (`POST https://api.typesafe.ai/v1/systemone`) answers one `noul` question per candidate (top 12) and picks the best when it clears the confidence floor by 0.2, else exit 3 `ambiguous_target` with candidates. For `click`/`press`, Jev classifies the target (safe / submit / purchase / destructive / send_publish / credential / uncertain) and anything but a confident `safe` adds a `jev:<choice>` gate. Jev only adds gates; the heuristic gates always stay.
- Extension options page (`options.html`, same design as the popup, light/dark): "Jev (TypeSafe) API key", model (default `jev-latest`), confidence floor (default 0.9), Save (asks Chrome for the optional host permission `https://api.typesafe.ai/*`), Remove key, Test key. Popup: a Jev On/Off row with an Options button. `status` reports `jevConfigured`.
- Error codes: `jev_not_configured` (exit 7, `--jev on` without a key), `jev_error` (exit 5, `--jev on` when Jev fails). Under `--jev auto` a failed Jev call falls back to heuristics with stricter gating (`jev_unavailable`: every button and anything in a form asks Kevin) and `jev.used: false`.
- `read --elements` and `find` share the same element refs.

### Security

- The TypeSafe key is stored in `chrome.storage.local` and used only by the service worker's `fetch`; it is never sent to the native host, the CLI, logs, or pages, and the options page never shows it again. Tests assert it never appears in any port message.
- Jev sees no field values (only `valueKind` empty/filled) and at most 60 elements / 24 KB per page. Intents are logged as `<N chars sha256:8>`.

## 0.8.0 (2026-10-22)

### Added

- chromecli P2, tab actions: `chromecli open URL [--active] [--window new]`, `focus TAB`, `close TAB`, `navigate TAB URL [--expect-origin|--expect-url]`. Every one prints a `{"dryRun": true, ...}` preview and sends nothing to Chrome unless `--confirm` (alias `--yes`); `--dry-run --confirm` stays dry. Bot-opened tabs (`open`, cross-origin `navigate`) are auto-shared for act when the origin is in `act_allow`, for read when in `read_allow`, else left unshared (metadata only in `list`, `botOpened: true`). `close`/`focus`/`navigate` need Share-act or a bot-opened tab; `open`/`navigate` outside both allowlists waits for Kevin in the browser; denied origins exit 4 `origin_blocked`.
- chromecli P3, page actions in act-shared tabs: `click`, `type`, `fill`, `select`, `press`, `wait` (read-only), `eval`. Targets `--selector`, `--ref` (from `read --elements`), `--text [--role]`; `--tab` and `--expect-origin`/`--expect-url` are required for every mutating action (exit 2 `missing_expect`; a tab elsewhere exits 5 `url_mismatch`). Post-checks: `fill`/`type`/`select` read the value back; `--verify-url-prefix`/`--verify-text` wait up to 10s; a failed check after dispatch exits 8 `sent_unverified`. Values can come from `--value-file FILE|-`.
- In-browser confirm window (`confirm.html`, same design as the popup, light/dark): tab title, origin, op, element (tag, role, name, 200-char HTML), a "Gated because" list, and the value (masked for password/payment fields). Deny is the default; Allow once unlocks after 0.8s; closing the window denies; 60s (`confirm_timeout`) → exit 4 `confirm_timeout`; one at a time, others queue; Pause closes it (exit 4 `paused`). Preconditions are re-checked after Allow. Gates (pure `gate.js`, spec 6.7): eval; submit-like clicks (form submit buttons, `input[type=submit|image]`, buy/order/pay/send/post/delete/... labels); Enter in a form field; type/fill into password, `cc-*`, `current-password`/`new-password`/`one-time-code`, or card/CVV/IBAN/routing/SSN fields; open/navigate outside `read_allow ∪ act_allow`.
- Popup: a "N agent actions wait for your OK" banner (click to bring the confirm window forward) and a global `?` badge while a confirm waits; the Act level's hint now says what it does.
- Error codes: `eval_disabled` (exit 7). Test fixtures `tests/fixtures/chromecli/form.html` and `nav.html`.

### Security

- Typed / filled / selected values, `--text` targets, and eval code appear in `~/.webcli/logs/chromecli.log` only as `<N chars sha256:8>`; the extension never returns field values (read-back reports match / no match) and never puts them in its activity log.
- `eval` ships disabled: Chrome's MV3 isolated-world CSP forbids `eval`/`new Function`, and chromecli won't run bot code in the page's own world.
- The extension refuses any mutating op sent without `confirm: true`, accepts confirm answers only from its own `confirm.html` (not web-accessible) for the pending confirm's id, and drops queued confirms nobody waits for.

## 0.7.1 (2026-10-22)

### Changed

- chromecli extension 0.7.1: redesigned toolbar popup (same behavior, permissions, protocol, and safety rules). Header with the status pill (Connected / Disconnected / Paused), current-tab card with favicon and an Off / Read / Act switch, a clear "can't be shared" panel with the deny reason, Pause agents as a switch showing its shortcut, the pending policy as a banner with Accept / Reject, a list of every shared tab (level chip, unshare x, click to switch to it), Unshare all, and a collapsible activity log. Light and dark follow the system; keyboard and screen-reader friendly. New icons (16/32/48/128). The popup's `state` reply gains `shared` (the shared tabs) for the list.

## 0.7.0 (2026-10-22)

### Added

- `chromecli`, a third command from the same package (like `llmcli`): read the tabs you share from your real, signed-in Chrome. P1 is read-only: `chromecli status | list [--active] | show TAB|--active | read TAB|--active [--as text|md] [--max-chars N] [--elements] | screenshot TAB|--active -o FILE [--jpeg --quality Q] | history [--query Q] [--limit N] [--since DATE] | install-host | uninstall-host | doctor | agent-help`. Same `--format`, JSON errors, redaction, and exit codes as webcli; its error codes (`not_shared`, `paused`, `origin_blocked`, `policy_pending`, `not_visible`, `bridge_down`, ...) map onto them (2/3/4/5/7/8).
- The "chromecli" MV3 extension in `extensions/chromecli/` (Load unpacked; not in the wheel), with a stable ID `ileggcomhbnofnapippaanhimdhchhci` from the committed public key. Toolbar popup: Share-read (badge R) / Share-act (badge A) / Unshare per tab, Pause agents (badge II; also Cmd+Shift+Period), Unshare all, pending policy Accept/Reject, and recent activity. No blanket host permission: sharing a tab asks Chrome for that origin.
- `chromecli-host`, the native messaging host (`com.truefrontier.chromecli`): checks the caller is the chromecli extension, takes `run/chromecli.lock` (a second one answers `host_busy`), serves `run/chromecli.sock` (0600 in 0700), pushes the `[chromecli]` policy (re-read every 2s), and exits with Chrome. `chromecli install-host` writes its Chrome manifest (dry run unless `--confirm`).
- Origin policy, enforced by the extension: built-in deny (every non-http(s) scheme, the Chrome Web Store) that config can't remove; a default deny list (banks, sign-in/SSO, password managers, checkout/cart/oauth/login paths); `[chromecli]` config (`implicit_active`, `history`, `confirm_timeout`, `read_allow`, `act_allow`, `deny`). A config change that widens access waits for Kevin's Accept in the popup (`policy_pending`, exit 4); tightening applies at once.
- `webcli doctor` / `llmcli doctor` gain a `chromecli` row: host manifest present and pointing at an existing `chromecli-host`, allowed origin = the extension ID, socket mode, extension connected and its version.

### Security

- Every chromecli op is logged to `~/.webcli/logs/chromecli.log` through the redacting, rotating logger (URLs through `redact_url`; history queries only as `<N chars sha256:8>`). `read --elements` never reads form values (only `valueKind: empty|filled`).
- The extension's private key is not in the repo (it lives outside it, 0600); only the public key and ID are committed.

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
