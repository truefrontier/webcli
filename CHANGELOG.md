# Changelog

## 0.16.1 / llmcli 0.6.2 (2026-10-24)

### Fixed

- `webcli notebooklm source add` / `create`: a source NotebookLM refuses to import (RPC code 9, also 3 and 5; a
  paywalled or bot-blocked page, an empty or unreadable input) is now that source's failure with a plain error,
  "NotebookLM couldn't import this source (code 9). The page may be paywalled or blocking imports. Try uploading a
  PDF or pasting the text instead." (with text- and file-specific wording), instead of "NotebookLM's API may have
  changed". Unknown codes and structural errors keep that hint; auth and usage-limit codes still stop the run.
  notebooklm-py treats these the same way (a per-item SourceAddError).
- The exit-5 summary names the failed inputs: `"2 of 5 sources failed: URL1, URL2."` (the first 10, then "and N
  more"); the JSON error has all of them in `failed`.

### Changed

- Version 0.16.1 for webcli, the chromecli package (and its webcli pin), and the chromecli extension manifest;
  truefrontier-llmcli / llmcli 0.6.1 -> 0.6.2, pinned to truefrontier-webcli==0.16.1.

## 0.16.0 / llmcli 0.6.1 (2026-10-24)

`webcli notebooklm create` and `webcli notebooklm source add`: make a notebook and fill it with files, URLs, and
text, so agents can build a notebook to ask. Both are dry runs (exit 2) unless `--confirm`. Verified live: `create`
of an empty notebook and the `source add` dry runs; adding sources (including the file upload) is not yet run live.
The request shapes follow notebooklm-py 0.8.5 (see docs/notebooklm-site.md, "To confirm live").

### Added

- **`create TITLE [--file ...] [--url ...] [--urls-file F] [--text T --title T] --confirm [--wait]`**: creates the
  notebook, adds any sources, and prints `notebook_id`, `url`, and each source. Without `--confirm` it prints the
  plan without opening a browser.
- **`source add NOTEBOOK (--file PATH ... | --url URL ... | --urls-file FILE | --text T --title T) --confirm
  [--wait]`**: files `.md`, `.markdown`, `.txt`, `.pdf` (up to 50 MB), web and YouTube URLs, and pasted text, many
  per call; each source's id and status. The dry run checks the notebook (read-only). `--wait` polls until each
  source is ready or errors (`--timeout`, `--interval`); exit 5 if any failed or the wait timed out.

### Guard

- A write policy per command, budgeted from its command line: one `CCqFvf` (create), one `izAoDd` per URL or
  text, one `o4cbdc` per file, and two `/upload/_/` POSTs per file. Rename, delete, share, source delete/rename/
  refresh, note writes, chat, and generate stay blocked. The file upload runs inside the page; its one-time URL is
  checked and never returned.
- `llmcli notebooklm` is unchanged: it still runs only `status`, `notebooks`, and `ask`.

### Changed

- Version 0.16.0 for webcli, the chromecli package (and its webcli pin), and the chromecli extension manifest.
- truefrontier-llmcli / llmcli packages 0.6.0 -> 0.6.1, pinned to truefrontier-webcli==0.16.0.

## 0.15.0 / llmcli 0.6.0 (2026-10-24)

`llmcli notebooklm`: NotebookLM's ask as an llmcli chat site, the same shape as gemini, chatgpt, and muse, so agents
and tmuxlet can use a notebook as a chat backend. It reuses `webcli notebooklm`'s fetch code; nothing is duplicated.
Merged after amazon 0.14.0, so webcli goes to 0.15.0.

### Added

- **`llmcli notebooklm ask -n NOTEBOOK PROMPT`** and **`llmcli notebooklm -p -n NOTEBOOK [PROMPT]`** (stdin, `-`,
  `--prompt-file`, `--output-file`, `--output-format json`, `--quiet`, `--timeout`), plus `notebooks`, `status`,
  `models` (always `[]`), `login`, `serve`, and `agent-help`. `--source` (repeatable) narrows the sources.
- **Notebook by id, URL, or exact title** (`-n`, or env `LLMCLI_NOTEBOOKLM_NOTEBOOK`). `webcli notebooklm ask` takes
  a title too. Two notebooks with the same title exit 2 naming both ids; none exits 3.
- **Multi-turn:** every answer's conversation id is `chatId`; `--chat ID` sends into it, `-p -c` continues the last
  ask's notebook and conversation.
- **Uniform ask JSON** (answer, chatId, null model fields, images, canvas) with NotebookLM's citations (`number`,
  `title`, `url: null`, `source_id`, `source_title`, `excerpt`).
- `Site.llm_cli`: a data site can offer an llmcli chat surface; `webcli sites` shows it as `alsoLlmcli`.

### Scope

- `llmcli notebooklm` runs only the `status`, `notebooks`, and `ask` ops (checked before every call); `ask` runs
  under the ASK guard policy (read rpcids plus GenerateFreeFormStreamed). No generate, download, or edits.

### Changed

- Version 0.15.0 for webcli, the chromecli package (and its webcli pin), and the chromecli extension manifest.
- truefrontier-llmcli / llmcli packages 0.5.0 -> 0.6.0, pinned to truefrontier-webcli==0.15.0 (the release with `llmcli notebooklm`).

## 0.14.0 (2026-10-24)

New site: `webcli amazon`, your Amazon.com orders, payment transactions, tracking, and wish lists, read-only, so
agents can tie a card charge to the order behind it (a $33.90 charge that was two books). Its own identity,
`amazon:default`; sign in once with `webcli amazon login`. See docs/amazon-site.md.

### Added

- **`orders [--year Y | --since DATE] [--limit N] [--details]`**: order id, date, total, ship-to name, status, and
  items; `--details` adds each order's invoice (item prices, tax, total, payment).
- **`order ORDER_ID`**: items with quantities and prices, subtotal, shipping, discounts, tax, gift card, total,
  shipments, and payment as card brand + last 4 only.
- **`transactions [--since DATE] [--limit N]`**: Your Payments > Transactions: date, amount, charge or refund, card
  (brand + last 4), order ids, merchant, pending or completed. Pages with the page's own Next button.
- **`match AMOUNT [--date DATE] [--window DAYS] [--no-orders]`**: transactions (then order totals) of that amount
  within DAYS of the date (default 7; without a date, the last 60 days), each with its orders' items.
- **`track ORDER_ID`**: each shipment's carrier, tracking number, status, and delivery date.
- **`wishlists`** and **`wishlist LIST [--limit N]`** (by id or name): items with prices and availability.
- **`status`** and **`login`**; every read has `--json` and the usual agent output flags.
- **Guard (hard read-only):** GETs only to amazon.com's order, invoice, tracking, transactions, and wish list
  paths and Amazon's image and static hosts; paths with cart, buy, checkout, return, cancel, subscribe, delete,
  remove, edit, move, add-to, purchase, or sign-out are refused even under those. The only POST allowed is the
  transactions page's own page-navigation form (every field `ppw-`, only Default/Next/Previous page events).
  Product pages, the cart, Buy Now, returns, cancellations, subscriptions, and account settings are all blocked.
- **Card numbers never leave as digits:** cards are reported as brand + last 4, and any card-number-shaped digit
  run in any field is masked to its last 4. Addresses are not reported (ship-to name only).
- **Captchas:** a captcha or bot check exits 4 with a message saying so; webcli never solves or skips one.

### Changed

- Version 0.14.0 for webcli, the chromecli package (and its webcli pin), and the chromecli extension manifest.

## 0.13.0 (2026-10-23)

New site: `webcli notebooklm`, NotebookLM through the `google:default` identity (no new sign-in). The wire format
was learned from [teng-lin/notebooklm-py](https://github.com/teng-lin/notebooklm-py) (v0.8.5, MIT License,
Copyright (c) Teng Lin), the unofficial NotebookLM CLI over the same Playwright session; re-implemented on webcli's
core, no code copied. See docs/notebooklm-site.md.

### Added

- **Reads:** `status [ARTIFACT]`, `notebooks`, `sources NOTEBOOK`, `source get SOURCE [--guide]` (full indexed
  text, or summary and keywords), `notes NOTEBOOK`, and `artifacts NOTEBOOK`, all with `--json`.
- **`ask NOTEBOOK "question"`**: NotebookLM's answer with citations (source id, title, quoted passage).
- **`generate KIND NOTEBOOK`** for every Studio output: `audio` (deep-dive, brief, critique, debate; short, default,
  long), `video` (explainer, brief, cinematic, short; ten visual styles or a custom prompt), `report` (briefing doc,
  study guide, blog post, custom), `quiz` and `flashcards` (quantity, difficulty), `mind-map`, `infographic`
  (orientation, detail, style), `slide-deck` (detailed or presenter; default or short), and `data-table`, with
  `--source`, `--language`, and `--instructions`. Nothing is created without `--confirm`: the default is a dry run
  that prints the plan and exits 2. `--wait` polls until it completes (exit 5 if it fails or times out).
  `generate kinds` lists them; FAQ and timeline report as unavailable (NotebookLM no longer has them).
- **`download ARTIFACT -o PATH`**: audio `.m4a`, video `.mp4`, infographic `.png`, slides `.pdf`, report `.md`, and
  quiz, flashcards, mind map, and data table as `.json`. Media come only from Google media hosts.
- **Guard:** three fail-closed policies: reads allow only read rpcids (one per call, matching the URL label); `ask`
  adds only the chat endpoint; `generate --confirm` adds only CreateArtifact. Source, notebook, note, and artifact
  edits and deletes stay blocked. The daemon treats `generate` as a write op.

### Changed

- Version 0.13.0 for webcli, the chromecli package (and its webcli pin), and the chromecli extension manifest.

## 0.12.4 (2026-10-23)

`webcli linkedin posts search` reads results again when LinkedIn serves its search cards without their
`data-view-name` tags, and dates the newest posts so `--sort latest` orders them.

### Fixed

- **`posts search` exited 3 "search page showed no result cards" though the page showed them.** Once another
  linkedin command (`feed`, say) had run in the daemon's browser, LinkedIn served the search page's cards with no
  `data-view-name` attributes at all, and every card selector keyed on them: the cards-ready wait timed out, no card
  had an author, and none was kept. The card scripts (`CARD_PARTS_JS` in `webcli/sites/linkedin/fetch.py`) still use
  the tags when they are there and otherwise read the author from the card's profile link (outside the text, the
  one beside the avatar), the text from `[data-testid="expandable-text-box"]`, and the counts from the action
  buttons' aria-labels. Both markups give the same rows (tests/fixtures/linkedin, made-up names).
- **Cards rendered but unreadable now say so.** The exit-3 message names the card markup (`CARD_PARTS_JS`) instead
  of "no result cards" when cards rendered without a readable author.
- **"Just now" posts had no `posted_at`**, so `--sort latest` put the newest last. "Just now", "now", and
  "with 2 others • Just now" are now read as times, and no longer taken for a headline.
- **Fewer null urns.** Urns are also read from `ugcPost`/`share` updates in the page's payload, and from the page's
  own streamed responses (`/flagship-web/rsc-action/actions/server-stream-request`, read when they finish, never
  waited for). Article cards linked by `feed-article-image`/`feed-article-description` are `kind: article`.

## 0.12.3 (2026-10-23)

`webcli messages` fixes from its first real sends: group sends start, a number finds its contact's conversation,
a new conversation titled with the contact's name is checked by its number, a sent text with a line break verifies,
and `read`/`search` show which number a conversation uses.

### Fixed

- **Group send exited 5 "Start chat shows no group button".** Live, the button (still
  `button[data-e2e-start-group-chat-button]`) is removed while the picker's input holds text and comes back ~250 ms
  after it is emptied; webcli clicked at once. It now waits for it (8 s). The `--new-ok` dry run now stages the
  recipients in Start chat as a confirmed send would (group mode, each picked, chips exactly the recipients, Next
  shown) without the click that opens or creates the conversation: `staged`, `chips`.
- **`search NUMBER` (and `read`/`send --to NUMBER`) missed a contact's existing 1:1.** Messages' picker matches a
  contact by its number as saved, from the start, so one saved as "+1 ..." shows only when "+1..." is typed; webcli
  typed the national digits, found no contact, and couldn't tell that the conversation titled with the contact's
  name was the number's. It now types national digits, then +1..., then (xxx) xxx-xxxx until a contact shows, loads
  up to 20 pages once the picker names the contact (an explicit --pages still caps it), and checks the contact's
  1:1s by their Details (below), loading further for another one when the first uses another of its numbers. A full
  name that several group names contain no longer exits 2 from the newest 25: the lookup goes on to the 1:1.
- **`send --new-ok` to a number exited 5 though the right conversation opened.** It opened titled with the
  contact's name, not the number. The new conversation is now checked by its Details' numbers: exactly the
  recipients' numbers passes; another number refuses, whatever the title; unreadable falls back to the title check.
  An existing conversation found for a number (or with a number `--expect-recipient`) is checked the same way, dry
  run included (output `numbers`): another number or unreadable Details exit 5.
- **Real sends exited 8 (sent, unverified) though they went out once.** The three had a line break; a bubble's line
  breaks are `<br>` elements, which the text read (`textContent`) dropped, so it never equaled the text sent. The
  bubble is now read with `<br>` as "\n" and without a link preview's text, and compared whitespace runs aside. A
  send is still never retried.

### Added

- **Which number(s) a conversation uses**, from the conversation menu's Details dialog (only Details is clicked; it
  is closed with Escape, else Cancel; nothing new passes the guard): `read` prints `Conversation: NAME (ID,
  +1...)` on stderr in every mode; `search` rows gain `numbers` (and a 1:1's `number`) for the first 6
  conversations found, 1:1s first, and a contact row's `id` is the 1:1 that uses that number. `search
  --no-numbers` skips opening them.

## 0.12.2 (2026-10-23)

`webcli recorder` reads every page of the recording list, not just the newest 10.

### Fixed

- **Recordings past the first page were invisible.** Recorder's app sends GetRecordingList 10 recordings at a time
  and asks for the next page only when its list is scrolled to the end; webcli kept the first answer, so `list`,
  `search`, and `sync` saw only the newest 10, and `info`, `transcript`, and `download` said "No recording" for
  anything older. webcli now keeps every page the app answers and scrolls the list (in the app's shadow DOM) to its
  end until a page comes back short, two scrolls in a row bring nothing, or 300s pass; recordings are listed once
  each, newest first. Live: 86 recordings (8 pages of 10 and one of 6) where 0.12.1 listed 10. Ported from
  CodefiLabs/google-recorder-cli#2 (its first half). That PR's second half, a 3s settle timer that raced the
  GetTranscription answer, doesn't apply: webcli waits up to 30s for a recording page's first GetTranscription
  answer, and a recording's page sends exactly one, full-size (checked live on 4 recordings).

## 0.12.1 (2026-10-22)

chromecli records a tab Kevin shared (`record start|stop|status`), and screenshots it while it's covered or in the
background. Recording needs Kevin's click in Chrome every time: `--confirm` alone never starts one.

### Added

- **`chromecli record start TAB [--out FILE.webm|.mp4] [--fps 30] [--max 10m] [--audio] [--wait 2m] [--force]
  --confirm`.** Only on a tab Kevin shared (Read or Act; not bot-opened, inherited, denied, or while paused). Without
  `--confirm` it's a preview. With it, the confirm window says "Start recording this tab: <title> (<origin>)"; Kevin's
  Start click starts it (Deny, closing the window, or the timeout don't). Capture is `chrome.tabs.tabCapture`
  `getMediaStreamId` + an offscreen document running `MediaRecorder` (WebM VP9, VP8 fallback): only the tab's page
  pixels, iframes included, never the rest of the screen or Chrome's UI. No `debugger` permission.
- **Chrome's own gate.** Chrome lets an extension capture a tab only after Kevin clicked its icon on that tab since it
  last changed site (sharing from the popup counts; reloading the extension resets it). Without it the request
  turns `awaiting_popup`: stderr says "click the chromecli icon, press Record", the badge shows `REC?`, a
  notification shows, and the popup's row for that tab gets a **Record** button; the CLI waits (`--wait`, 5s-15m)
  and returns once recording, else exit 4 `confirm_timeout` (Ctrl-C cancels it).
- **While recording** the badge is red `REC` and the popup's row has **Stop**. It stops and finalizes by itself on
  Pause, Unshare, the tab closing, a top-frame navigation to another origin, or `--max` (1s-4h, default 10m).
- **`record stop TAB|--all`** finalizes: `{stopped: [...], finished: [...]}` with path, bytesWritten, durationMs,
  reason. **`record status [TAB]`** lists this host session's recordings (awaiting_popup / starting / recording /
  stopping / stopped / failed / cancelled / expired).
- **Files.** Default `~/.webcli/chromecli/recordings/tab<ID> <YYYY-MM-DD> at <HH.MM.SS>.webm` (0600, dir 0700). The
  host owns the path: the extension only ever sees a recording ID. Chunks of at most 512 KiB (base64 ~683 KiB; under
  Chrome's 1 MB host-to-extension and 64 MiB extension-to-host native-messaging limits) are written and flushed as
  they arrive, in order; a gap or a bad chunk stops writing (`writeError`). Existing files are refused without
  `--force`.
- **`.mp4`.** `--out x.mp4` records `x.webm`, then `record stop` converts it with ffmpeg if it's on PATH (H.264
  libx264 or VideoToolbox, yuv420p, even dimensions, faststart; AAC with `--audio`); the .webm is kept. Without ffmpeg
  the result says `converted: false` and a note ("brew install ffmpeg"), and the .webm is the recording.
- **`--audio`** (off by default) records the tab's sound and plays it back through the offscreen document, so the
  tab isn't muted for Kevin.
- **Covered screenshots.** `screenshot TAB --covered`, or a plain `screenshot` of a tab that isn't in front, uses the
  newest frame of that tab's recording (`source: "stream"`, `frameAgeMs`, `frames`). Not recording: exit 4
  `not_visible` with a hint to record it. A frame older than 2s adds `warning` (and a stderr line): its window is
  probably minimized.
- **Throttling, measured** (Chrome 153, `tests/fixtures/chromecli/animated.html`, Playwright's anti-throttling flags
  removed): a captured tab covered by another window or in a background tab kept painting at ~120 rAF/s with fresh
  frames and `visibilityState` "visible". A minimized window dropped to ~1 rAF/s and the stream froze (frameAgeMs
  6-9s). Advice in agent-help: the recorded tab in its own window, behind others is fine, never minimized.
- **Log.** `event=record_start` / `record_stop` lines: tab, origin, file name (quoted, never the directory), short
  recording ID, size, fps, audio, duration_s, bytes, chunks, reason; plus `record_waiting` and `rec_chunk_dropped`.
  The usual redaction applies to the whole line (an origin with digits, like `http://127.0.0.1:8765`, shows as
  `<redacted>`).
- New exit code: 5 `capture_failed` (Chrome refused the capture or the recorder failed).
- Tests: `extensions/chromecli/test/record.test.mjs` (19: the gesture, refusals, awaiting_popup + popup Record,
  expiry, chunk order/size, auto-stops on Pause/Unshare/origin/port loss, covered screenshots),
  `tests/test_chromecli_record.py` (13: CLI preview, incremental 0600 writes, dropped chunks, mp4 with and without
  ffmpeg, --force, waits, refusals, covered and stale screenshots, agent-help catches), and the by-hand
  `tests/e2e/chromecli_record_e2e.py` (isolated Chrome for Testing; 14 checks).

### Permissions

- New: `tabCapture` (Chrome's warning for it is "Read and change all your data on all websites"; an unpacked
  extension gets no prompt on reload) and `offscreen` (no warning). `webNavigation` was already there.
- After updating, reload the extension in chrome://extensions. That clears `chrome.storage.session`, so Kevin
  re-shares his tabs, which also gives Chrome's capture grant.

## 0.12.0 (2026-10-22)

`webcli messages` finds older conversations, can start a new 1:1 or group conversation (only with `--new-ok`), and
keeps Google Messages to one web session. A send needs Kevin's own OK for that exact message.

### Added

- **Deeper lookups.** `search`, `read`, and `send --to` look past the newest 25 conversations. They use Messages' own
  Start chat picker (the phone's contacts and its group search by member, both reads the guard already passed),
  then paced, capped "Load more" pages (`--pages`, default 8, max 40, 750 ms apart). They stop once the contacts the
  query names have their conversations listed. Phone numbers are normalized to E.164 (punctuation ignored, US +1 by
  default). `search` rows gain `kind` (`conversation` or `contact`) and `number`, and `matched` can now say `group`
  or `contact`. Ambiguous matches still exit 2 and list the candidates. `send --to` is stricter: a name that fits
  two contacts exits 2.
- **New conversations: `send --to NUMBER|CONTACT [--to ...] --text T --new-ok [--expect-recipient X ...]
  [--confirm]`.**
  - The dry run resolves each recipient (a number, or exactly one contact with exactly one number) and prints
    `{dryRun, new, group, recipients, expectRecipients, expectRecipientsMatch, text}`. It types only into the
    contact picker.
  - `--confirm` needs `--expect-recipient` (alias `--expect-recipients`) for each recipient, matching exactly. A
    mismatch exits 5 (`REFUSED (dry run)` without `--confirm`).
  - Confirmed, Start chat picks each recipient by its exact number, and in a group the chips must be exactly the
    recipients. Only then does the guard pass one `GET_OR_CREATE_CONVERSATION` (a new `SendScope(new_ok=True)`
    slot, 10 s). The conversation that opens must be titled with exactly the recipients before anything is typed.
    Then the usual send checks, verify, exit 8 if unverified, never retried.
  - Without `--new-ok`: several `--to` exit 2, and no conversation exits 3. Nothing creates a conversation.
- **One Messages session.** Every messages command holds `run/messages.session.lock` (an exclusive flock across
  identities, daemon or not) while it has the app loaded. A second waits `--lock-wait`, then exits 5
  `messages_busy`. `ProfileBusy` errors now carry an error name through the daemon.

### Changed

- **"Use here" is never clicked.** When Messages is open in another window or tab (its "Use here" button, or a
  banner or dialog saying so), commands exit 5 `messages_open_elsewhere` instead of taking the session over.
- The conversation title selector is now the live one (`[data-e2e-conversation-header] [data-e2e-header-title]`), so
  send's title check runs for real.
- agent-help and docs/messages-site.md: the new syntax and exit codes, and that a send needs Kevin's own OK for
  that exact message.

### Tests

- `tests/test_messages_new.py`: normalization, deep search, ambiguity, new 1:1 and group dry runs and confirmed runs
  (scripted page), recipient-mismatch refusal, no create without `--new-ok`, the guard's create slot, CLI exit
  codes, and lock contention (in-process and another process).
- `tests/test_messages_dom.py`: the page scripts on sanitized DOM fixtures (`tests/fixtures/messages`, made-up names
  and 555-01xx numbers) in headless Chromium.

## 0.11.5 (2026-10-22)

chromecli pre-approvals for a quote that lists several steps. QA round 4c: Kevin's "Yes, pre-approve chromecli
clicks on as-described-review for the next 45 minutes: Install once, Approve on the $0 Starter plan once, Scan
products once, Use the title or Use the size once, Use the description for all 3 once, Apply to 3 products once, and
Undo 3 times." got every grant refused (Install unclear 0.06, Approve overreach 0.11, the rest unclear 0.18–0.41).

### Root cause

- Jev's single scope question asked whether the **whole** multi-item quote "authorizes exactly this grant", so each
  one-step grant looked like a fraction of the approval (unclear) or the other steps looked like extra scope.
- Jev saw only `site: https://admin.shopify.com`: no tab title, path or store, so "as-described-review" matched
  nothing.
- `--max-amount` had a 0.01 minimum (CLI and extension), so "$0 Starter plan" could only get a $0.01 cap, which is
  more than $0 (overreach); quote amounts of `$0` were dropped.
- Uses were read from the whole quote, so "once" also limited "Undo 3 times".

### Added

- **Clause-aware scope check** (`extensions/chromecli/clauses.js`). The quote is split into a preamble (site, store,
  time window) and items, each with its own uses and amount. One Jev call maps every grant to its best item (`none`
  or < 0.8 → refused unclear); deterministic limits make a grant over its item's uses (counted across batches under
  the same quote), over its amount, or with a `--ttl` longer than the window **overreach**; one more Jev call scores
  each remaining grant against the preamble plus only its item (authorized ≥ 0.8, unchanged). Single-item quotes keep
  the 0.10.1 check (narrowing included). Fail-closed as before; the vague "i approve screencast clicks" still gets
  nothing.
- **`chromecli grant add-batch`**: `--file grants.json` and/or repeated `--grant` (JSON or plain target text),
  defaults from `--op/--origin/--frame-origin`, 1–20 grants, one quote, `--user-approved`, preview unless
  `--confirm`. All or nothing (exit 9 with per-grant `error.results`); `--partial` stores the authorized ones.
  Returns `{added, refused, preamble, windowSec, page, results: [{index, verdict, confidence, reason, clause, stored,
  grant}]}`.
- **Page context for Jev**: each grant's view has `page {url, store, tabTitle, onlyPagesUnder}` (title labelled
  untrusted page content) plus the frame origin, target text, cap, uses and minutes. `--tab TAB` on `grant add` /
  `add-batch` (shared or bot-opened, on the origin), else the one visible tab on the origin. A Shopify admin store
  pins the grant to `/store/NAME`.
- Stored grants keep `clause` and `context`; `grant list` shows `item="..."`; the host log has `clause=` on
  `op=grant.add` and `gN=verdict:conf grantN=id clauseN="..."` per grant on `op=grant.add_batch`; `grant_added` /
  `grant_refused` events carry the clause.
- Tests: `extensions/chromecli/test/grant-batch.test.mjs` (18, mocked Jev, fixture
  `test/fixtures/grant-batch-as-described.json`, hand-made in the recorded TypeSafe response shape: quote (b) → 7
  authorized, Undo uses 4 overreach, unmatched grant, quote (a) refused, all-or-nothing vs `--partial`, TTL = 45m,
  `$0` cap, store pin, fail-closed) and `tests/test_chromecli_grant_batch.py` (CLI parsing, validation, preview,
  request, exit 9, log lines, agent-help).

### Changed

- `--max-amount 0` is allowed (free only). `--ttl` defaults to the quote's time window, else 30m.

## 0.11.4 (2026-10-22)

Hardening after 0.11.3: when `config.toml` gained `deny_except` and `frame_act_allow`, the still-running 0.10.2
chromecli-host refused them (`configError` "[chromecli] has unknown keys") and every command but `status` exited 7
until the extension was reloaded.

### Changed

- **Unknown `[chromecli]` keys are warnings, not errors.** They're ignored (the policy is built from the known keys
  only, so ignoring one never loosens anything), logged by the host (`event=config_warning`, key names only, never
  values) and listed in `chromecli status` as `configWarnings` (also in `chromecli doctor`). A known key with an
  invalid value is still fatal (exit 7 `config_error`).

### Added

- **Version mismatch says what to do.** `chromecli status` (and `doctor`) report `versions` (`cli`, `host`,
  `extension`) and `versionMismatch`; when they differ, a hint (also on stderr in table/text format) says "reload the
  chromecli extension in chrome://extensions". Every exit-7 error says the same when the running host's version
  differs from the installed CLI's or the extension's: 0.11.4+ hosts put `hostVersion` / `extVersion` in
  `config_error`; for older hosts the CLI asks `status`.
- Tests: `tests/test_chromecli_config_warnings.py` (warnings, never-loosening, invalid known keys stay fatal, no
  values in warnings, the host's log and status, the reload wording for status and exit 7, including an older host)
  and a node test that a pushed policy's unknown keys are dropped by the extension.

## 0.11.3 (2026-10-22)

Two narrow, Kevin-accepted loosenings in chromecli: one URL shape through a path deny (the Shopify OAuth install
page), and acting inside a listed app's iframe (As Described inside Shopify admin). Nothing here removes a gate.

### Added

- **`deny_except`** in `[chromecli]`: full `https://host/path` patterns, `*` within one path segment, query ignored
  (e.g. `https://admin.shopify.com/store/*/oauth/install`). A matching URL is not denied for that URL only, and only
  when every deny hit is a path deny (`path:/oauth`, `host/path`); host/origin denies and the built-in deny always win.
  Refused: http, wildcard hosts, ports, whole origins, `**`, all-wildcard paths, the Chrome Web Store. Adding or
  changing a pattern is loosening (pending until Accept; the popup says it in plain words); removing applies at once.
  Replies, the host log (`deny_except=`) and the popup activity ("via deny exception") say when an exception allowed
  access.
- **`frame_act_allow`** in `[chromecli]`: exact https origins. click/fill/type/select/press on a frame ref (`f5:e3`)
  work only when the tab is Share-act'd by Kevin (or inherits his Act share), the frame's origin is listed and not
  denied, and Chrome grants chromecli host access to it (asked in the popup on Accept, or when setting Act; never
  silently; else exit 4 `permission_needed`). Everything else still exits 2 `bad_target` `frame_ref`. Same gates as
  the top frame (heuristics, Jev ≥ 0.7 safe, confirm window, `--confirm`); the confirm window shows the frame;
  results carry `frameId` / `frameOrigin`, the log `frame=N frame_origin=`. Listed frames on act tabs are readable.
  Adding is loosening (pending until Accept); removing applies at once.
- **`grant add --frame-origin ORIGIN`**: a pre-approval for an element inside a frame from that origin (and only
  there; grants without it never cover a frame). Jev's scope check sees the frame. Batch approvals ("approved to click
  Scan, Review fixes, Apply and Undo in As Described ...") are one grant per target, `--uses N` for repeats.

### Changed

- Gate words now include `install`, `uninstall`, `authorize`/`authorise` (an app's Install button always asks).
- Pending policy changes in the popup show plain-words descriptions for deny exceptions and frame origins.

## 0.11.2 (2026-10-22)

`webcli whatsapp read` answered `[]` with exit 0 for a chat full of messages (field report: a three-person group).

### Fixed

- **WhatsApp `read` on today's WhatsApp Web.** Message rows now carry a bare `data-id` (no `true_` / `false_`
  prefix) and no `message-in` / `message-out` classes, so every row was skipped as a date label. Rows are now found
  by `data-testid="conv-msg-*"` / `[role=row] [data-id]` as well; direction comes from the hidden `You:` label (and
  the tails); the sender from `data-pre-plain-text`, the hidden `<Name>:` label, the group author, or the run it
  continues (in a 1:1, the chat's name); the date from `data-pre-plain-text` (a quoted line's `> ` prefix is
  skipped), else the date separators, which are no longer rows. Quotes, voice notes, stickers, images, documents
  (file name), reactions and link previews parse in the new shape.
- **No more silent empty reads.** After opening a chat, `read` waits up to 15s for message rows; when none render, or
  rows render and none parses, it exits 5 with a hint instead of printing `[]`. An empty result now only comes from
  `--since` and says so on stderr.
- **History past the first screen.** WhatsApp renders only the rows near the viewport, so jumping to the top lost
  everything in between. `read` now scrolls up about a screen at a time (still 1.2s apart, at most 40 steps, stopping
  once `--limit` / `--since` is met) and merges each screen by message id, in order; rows in the returned span that
  never rendered are warned about.
- **Chat list and search order.** Rows are put in their on-screen order (their `translateY`), not the recycled DOM
  order, so `chats` and search sections no longer shuffle.

### Added

- `read` rows carry `id` (WhatsApp's message id).
- Sanitized live fixtures `tests/fixtures/whatsapp/thread_live_*.html` (placeholders only; see its README) and
  tests for the live shape, the scroll-and-merge, and the no-rows / unreadable-rows errors.

## 0.11.1 (2026-10-22)

chromecli on pages whose content lives in iframes or closed shadow roots (field report: a signNow agreement, where
`read` gave 0 chars, `scroll` and `--full-page` picked the window at 1042/1042, and `--container` failed with a bare
`script_error`).

### Added

- **Iframes**: `read`, `--elements`, `find`, `wait`, `scroll` and `screenshot --full-page` also run in the tab's
  iframes that are on the tab's origin, or on the same site (scheme + registrable domain) when Chrome already grants
  chromecli host access to that origin. Other sites, denied origins, opaque origins and frames inside a skipped frame
  are never read (no script runs there) and are listed as skipped. New extension permission `webNavigation` (lists
  frames; no page access). No `debugger` permission.
  - `read`: the top frame's content, then each frame's after `[frame N: url WxH]` (md: an HTML comment); `frames
    [{frameId, origin, url, chars, rect, offset}]`; subframe element refs are `f<frameId>:e<n>` with `frame: N`.
  - `find` ranks elements from every readable frame; `wait --selector/--text` matches in any readable frame. Page
    actions stay top-frame only: a frame ref exits 2 `bad_target` (`reason: "frame_ref"`).
  - `scroll` auto-detect works across frames: an iframe whose document scrolls (or a scroller inside it) covering
    ≥ 30% of the viewport wins. `target` adds `frameId`, `frameOrigin`, `frameUrl`; `--full-page` passes `frameId`
    on every scroll (host log `frame=N`). `--container` / `--into` search every readable frame.
- **Closed shadow roots** are read through `chrome.dom.openOrClosedShadowRoot` (`--elements` marks them
  `closedShadow: true`).
- **`skipped`** in `read`, `find`, `scroll` (when nothing scrolls or frames were left out) and `--full-page`:
  `[{kind iframe|canvas|plugin|closed_shadow_host, frameId?, origin?, url?, rect, reason, detail?}]`, reasons
  `other_site`, `no_host_permission`, `denied`, `opaque_origin`, `parent_skipped`, `load_error`, `not_loaded`,
  `frame_unreachable`, `too_many_frames`, `canvas_pixels`, `plugin_content`, `closed_shadow_root`. `read` text output
  prints one `[skipped ...]` line each. `scroll` adds `scrollable: false` when nothing scrolls.
- **Error reasons**: errors keep their code/exit and add `reason` plus the underlying message: `no_match`
  (element_not_found; `matches`), `multiple_matches` (ambiguous_target; `count`, `candidates`), `not_scrollable`
  (script_error; `scrollHeight`, `clientHeight`), `frame_unreachable`, `frame_ref`, `bad_selector`, `target_gone`,
  `page_exception`, `inject_failed`, `no_result`.

### Fixed

- A page script's error lost its code and message on the way out of the extension (the reply was spread with
  `code: undefined, message: undefined`), so e.g. `scroll --container '[class*=scroll]'` with no match printed
  `{"error": "script_error", "message": "script_error"}`. It now prints `element_not_found` / `no_match` (exit 3) with
  the message.

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
