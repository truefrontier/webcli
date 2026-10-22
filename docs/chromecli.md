# chromecli (P1: read-only)

`chromecli` lets Kevin's agents read the Chrome tabs he explicitly shares, in his real, signed-in Chrome (Default
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
chromecli install-host [--extension-id ID] [--confirm]    # dry run without --confirm
chromecli uninstall-host [--confirm]
chromecli doctor                    # same row as `webcli doctor` -> "chromecli"
chromecli agent-help [--format json]
```

## Safety model (P1)

- Kevin shares a tab from the toolbar popup: **Share-read** (badge R, blue) or **Share-act** (badge A, orange; acting
  is P3, reading works). Sharing asks Chrome for host access to that origin (`optional_host_permissions`; no blanket
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

## Config

```toml
[chromecli]
implicit_active = "off"   # "off" | "read": the active tab counts as read-shared (only where host access was granted)
history = true            # `chromecli history` (deny-origins always filtered)
confirm_timeout = 60      # for later phases' in-browser confirm
read_allow = []           # origins auto Share-read when bot-opened (P2)
act_allow  = []           # origins auto Share-act when bot-opened (P2)
deny = [ ...defaults... ] # replaces the default list; the built-in deny always applies
```

Patterns: `host`, `*.host` (includes the bare host), `scheme://host[:port]`, `host/path` (path prefix),
`path:/prefix` (any origin whose path contains `/prefix`). Invalid config: every op but `status` exits 7.
Env: `CHROMECLI_TIMEOUT` (seconds, default 30), `CHROMECLI_FORMAT`, `WEBCLI_HOME`.

## Exit codes

2 bad_request/too_large/bad_target; 3 tab_not_found/no_history; 4 not_shared/not_act_shared/origin_blocked/paused/
not_visible/permission_needed/policy_pending; 5 bridge_down/host_busy/timeout/url_mismatch/script_error;
7 config_error/host_not_installed/history_disabled; 8 sent_unverified (later phases).

## Install (Kevin)

1. `pipx install --force ~/Sites/truefrontier/webcli` (puts `webcli`, `llmcli`, `chromecli`, `chromecli-host` on PATH).
2. `chrome://extensions` → Developer mode → Load unpacked → `~/Sites/truefrontier/webcli/extensions/chromecli`.
   Check the ID shown is `ileggcomhbnofnapippaanhimdhchhci`.
3. `chromecli install-host` (preview), then `chromecli install-host --confirm`.
4. Reload the extension (circular arrow on its card), then `chromecli status`.

## Not in P1

Tab actions (open/focus/close/navigate), page actions, in-browser confirm, Jev. `chtabs` (press-chrome) is untouched.
