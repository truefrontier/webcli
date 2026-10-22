# chromecli (P1 read, P2 tab actions, P3 page actions)

`chromecli` lets Kevin's agents read, and act in, the Chrome tabs he explicitly shares, in his real, signed-in Chrome (Default
profile), through an MV3 extension and a native messaging host. It is its own command, built from the webcli repo and
package (like `llmcli`), and reuses webcli's core: output formats, exit codes, `die`/JSON errors, redaction, the data
home `~/.webcli/`, and `agent-help`.

Spec: `TRUE-explore/webcli-tabs/SPEC.md` (written as `webcli tabs`); this doc records the as-built names.

## Pieces

| Piece | Where |
|---|---|
| CLI | `chromecli` (`webcli/chromecli/cli.py`, entry `webcli.apps.chromecli:main`) |
| Native host | `chromecli-host` console script (`webcli/chromecli/host.py`), host name `com.truefrontier.chromecli` |
| Host manifest | `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.truefrontier.chromecli.json` |
| Extension | `extensions/chromecli/` in the repo (Load unpacked; not in the wheel). Name "chromecli". |
| Extension ID | `ileggcomhbnofnapippaanhimdhchhci` (from the public key in `manifest.json` "key" / `extension-key-pub.txt`) |
| Private key | `~/.webcli/secrets/chromecli-extension-key.pem` (0600, never committed; only needed to pack a .crx) |
| Socket | `~/.webcli/run/chromecli.sock` (0600; `run/` 0700); lock `run/chromecli.lock` |
| Audit log | `~/.webcli/logs/chromecli.log` (redacted, rotated, 0600) |
| Config | `[chromecli]` in `~/.webcli/config.toml` |

```
chromecli CMD --1 JSON line--> ~/.webcli/run/chromecli.sock --> chromecli-host <--native framing--> extension SW
```

Chrome spawns the host when the extension calls `connectNative("com.truefrontier.chromecli")` (at every service-worker
start, plus a 1-minute alarm and the popup re-connect). The host exits and unlinks the socket when Chrome closes its
stdin. No CDP, no TCP port, no launchd.

## Commands

```sh
chromecli status                    # connected, ext version + ID, paused, shares, policy (exit 5 bridge_down if not)
chromecli list [--active]           # shared tabs: id,title,url,origin,share,active,windowId,botOpened
chromecli show TAB | --active
chromecli read TAB | --active [--as text|md] [--max-chars N] [--elements]
chromecli screenshot TAB | --active -o FILE [--jpeg --quality 80]
chromecli history [--query Q] [--limit 50] [--since 2026-10-01]
chromecli wait --tab TAB (--selector S | --text T | --url-prefix U) [--timeout 15]   # read-only

# P2 tab actions: preview unless --confirm (--dry-run wins)
chromecli open URL [--active] [--window new] --confirm   # no share needed; bot-opened auto-share rules below
chromecli focus TAB --confirm                            # Share-act or bot-opened; enables screenshot
chromecli close TAB --confirm                            # Share-act or bot-opened only (else exit 4)
chromecli navigate TAB URL [--expect-origin O | --expect-url U] --confirm   # Share-act or bot-opened

# P3 page actions: Share-act + --tab + --expect-origin/--expect-url; preview unless --confirm
chromecli click  --tab TAB TARGET --expect-origin O [--verify-url-prefix U] [--verify-text T] --confirm
chromecli type   --tab TAB TARGET (--value V | --value-file F|-) --expect-origin O --confirm   # appends, key by key
chromecli fill   --tab TAB TARGET (--value V | --value-file F|-) --expect-origin O --confirm   # replaces
chromecli select --tab TAB TARGET --value VALUE_OR_LABEL --expect-origin O --confirm
chromecli press KEY --tab TAB [TARGET] --expect-origin O --confirm   # no TARGET: the focused element
chromecli eval   --tab TAB (--code JS | --file F) --expect-origin O --confirm   # DISABLED: exit 7 eval_disabled
#   TARGET = --selector CSS | --ref eN (from read --elements) | --text NAME [--role ROLE]   (--find: P4, exit 2)

chromecli install-host [--extension-id ID] [--confirm]    # dry run without --confirm
chromecli uninstall-host [--confirm]
chromecli doctor                    # same row as `webcli doctor` -> "chromecli"
chromecli agent-help [--format json]
```

## Safety model

- Kevin shares a tab from the toolbar popup's Off / Read / Act switch: **Read** (Share-read, badge R, blue) or **Act**
  (Share-act, badge A, orange: read and act); **Off** unshares. The popup also lists every shared tab
  (unshare one with its x). Sharing asks Chrome for host access to that origin (`optional_host_permissions`; no blanket
  host permission). Shares are (tab, origin, level) in `chrome.storage.session`: cleared when Chrome restarts, revoked
  when the tab navigates to another origin or a denied URL, or is replaced/closed.
- **Pause agents** (popup or Cmd+Shift+Period): badge II; every op but `status` exits 4 `paused`. Persists across restarts
  until resumed. **Unshare all** in the popup.
- **Deny**: built-in (any non-http(s) scheme, Chrome Web Store) can't be removed; the default list (banks, sign-in,
  password managers, checkout/cart/oauth/login paths) is editable in config. Denied origins can't be shared; history
  leaves them out.
- **Loosening needs Kevin**: a pushed policy that adds to `read_allow`/`act_allow`, removes a deny, turns
  `implicit_active` on, or turns `history` on is held pending (global badge "!", popup Accept/Reject); ops on affected
  origins exit 4 `policy_pending`. Tightening applies at once.
- The host and CLI are untrusted callers; the extension enforces everything. Popup messages are accepted only from the
  extension's own pages, never from a tab.

## Acting (P2, P3)

- **Two confirm layers.** (a) CLI: without `--confirm` every action prints `{"dryRun": true, ...}` and sends nothing to
  Chrome; `--dry-run --confirm` stays dry. The extension also refuses any mutating op that arrives without
  `confirm: true`. (b) **In-browser confirm** for gated actions: a `chrome.windows.create({type: "popup"})` window
  (`confirm.html`, popup design, light/dark) showing the tab title, origin, op, element (tag, role, accessible name,
  200 chars of outerHTML with field values blanked), the *Gated because* list, and the value (masked for
  credential/payment fields). **Deny** has the default focus (Enter / Escape deny); *Allow once* is enabled after
  0.8s. Closing the window = Deny. `confirm_timeout` (default 60s) → `confirm_timeout`. One confirm at a time, the
  rest queue (popup banner "N agent actions wait for your OK", global badge `?`); a queued confirm whose CLI stopped
  waiting is dropped. **Pause** closes the pending confirm and cancels the queue (`paused`, exit 4). After Allow,
  every precondition is checked again (tab, share, pause, origin, `--expect-*`, the element's fingerprint).
- **Always gated** (`extensions/chromecli/gate.js`, spec 6.7): `eval`; clicks on submit-like elements (`button`
  default-submit in a form, `input[type=submit|image]`, or name/value/title matching
  `\b(buy|purchase|order|pay|checkout|send|post|publish|tweet|reply|submit|confirm|delete|remove|destroy|transfer|withdraw|subscribe|unsubscribe|accept|agree|approve|sign ?out|log ?out|merge|deploy)\b`);
  Enter (or Space) on such a button; `press Enter` in a form field; `type`/`fill`/`select` into `input[type=password]`,
  `autocomplete` `cc-*` / `current-password` / `new-password` / `one-time-code`, or name/id/label matching
  `card|cvv|cvc|iban|routing|ssn`; `open`/`navigate` to an origin outside `read_allow ∪ act_allow`. Unknown ops fail
  closed. Reasons: `submit_button`, `submit_input`, `submit_word:<w>`, `enter_in_form`, `password_field`,
  `payment_field`, `credential_field`, `sensitive_name`, `origin_not_allowlisted`, `eval`.
- **Bot-opened tabs** (`open`, cross-origin `navigate`) are marked `botOpened`: auto Share-act if the origin is in
  `act_allow`, else auto Share-read if in `read_allow`, else **unshared** (metadata only in `list`). Auto-share also
  needs Chrome's host permission for the origin (granted when Kevin Accepts the allowlist in the popup); without it
  the result says `shareSkipped: "permission_needed"`. `focus`/`close`/`navigate` work on act-shared or bot-opened
  tabs; `close` on a human-opened read-shared or unshared tab exits 4. Denied origins exit 4 `origin_blocked`.
- **Page actions** need Share-act, `--tab`, and `--expect-origin`/`--expect-url` (else exit 2 `missing_expect`); a
  tab elsewhere exits 5 `url_mismatch`. Targets resolve to exactly one visible, enabled element (none: exit 3
  `element_not_found`; several: exit 3 `ambiguous_target` with candidates). Injected functions run in the
  extension's ISOLATED world with synthetic events (`isTrusted=false`): Enter in a form field calls
  `form.requestSubmit()`, Enter/Space on a button calls `click()`. Values are set through the native value setter
  plus `input`/`change` events (React-friendly).
- **Post-checks**: `fill`/`type`/`select` read the value back (only match / no match leaves the page);
  `--verify-url-prefix` / `--verify-text` wait up to 10s. A failed check after dispatch → exit 8 `sent_unverified`
  (don't repeat). Result JSON: `{tab, origin, op, element, gated, reasons, confirmedInBrowser, dispatched, verified,
  checks}`.
- **eval is disabled** (exit 7 `eval_disabled`): Chrome applies a fixed CSP to extension isolated worlds
  (`script-src 'self' 'wasm-unsafe-eval' ...`, no `unsafe-eval`), so `new Function(code)` can't run there, and
  chromecli won't run bot code in the page's MAIN world. The command, its preview, and its gate exist so it can be
  enabled later (e.g. a vetted expression set or the userScripts API).
- **Audit**: the host logs one line per op with the target (`selector`/`ref`/`role` as given, `--text` as a digest),
  `value=<N chars sha256:8>` for typed/filled/selected values, `code=<...>` for eval, redacted URLs, `gated=...`,
  `confirmed=...`, `confirm=declined|confirm_timeout`, outcome, and exit. The extension's popup ring buffer gets the
  op, reasons, and outcome; never values.

## Config

```toml
[chromecli]
implicit_active = "off"   # "off" | "read": the active tab counts as read-shared (only where host access was granted)
history = true            # `chromecli history` (deny-origins always filtered)
confirm_timeout = 60      # seconds for the in-browser confirm (10-600)
read_allow = []           # bot-opened tabs here are auto Share-read; open/navigate here isn't browser-confirmed
act_allow  = []           # bot-opened tabs here are auto Share-act;  open/navigate here isn't browser-confirmed
deny = [ ...defaults... ] # replaces the default list; the built-in deny always applies
```

Patterns: `host`, `*.host` (includes the bare host), `scheme://host[:port]`, `host/path` (path prefix),
`path:/prefix` (any origin whose path contains `/prefix`). Invalid config: every op but `status` exits 7.
Env: `CHROMECLI_TIMEOUT` (seconds, default 30), `CHROMECLI_FORMAT`, `WEBCLI_HOME`.

## Exit codes

2 bad_request/too_large/bad_target/missing_expect; 3 tab_not_found/element_not_found/ambiguous_target/no_history;
4 not_shared/not_act_shared/origin_blocked/paused/declined/confirm_timeout/not_visible/permission_needed/
policy_pending; 5 bridge_down/host_busy/timeout/url_mismatch/script_error; 7 config_error/host_not_installed/
history_disabled/eval_disabled; 8 sent_unverified.

## Install (Kevin)

1. `pipx install --force ~/Sites/truefrontier/webcli` (puts `webcli`, `llmcli`, `chromecli`, `chromecli-host` on PATH).
2. `chrome://extensions` → Developer mode → Load unpacked → `~/Sites/truefrontier/webcli/extensions/chromecli`.
   Check the ID shown is `ileggcomhbnofnapippaanhimdhchhci`.
3. `chromecli install-host` (preview), then `chromecli install-host --confirm`.
4. Reload the extension (circular arrow on its card), then `chromecli status`.

## Test fixtures

`tests/fixtures/chromecli/form.html` (email, password, country select, "Next" link, "Place order" submit that shows
"submitted", "Delete account" button) and `nav.html`. For E2E: `python3 -m http.server 8765 -d tests/fixtures/chromecli`
and `act_allow = ["http://127.0.0.1:8765"]` (Accept it in the popup).

## Not yet

Jev (`find`, `--find`, `--jev`; P4), chtabs retirement (P5). `chtabs` (press-chrome) is untouched.
