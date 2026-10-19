# webcli — consolidation design proposal

*Prepared for Kevin K to review. Mon Oct 19, 2026 (America/Chicago). **Design only — nothing is being built, migrated, moved, or deleted here.** Grounded in a read-only survey of the current CLIs on Kevin's M4 MacBook (user `kk`): repos under `~/Sites/truefrontier/*` and `~/Sites/CodefiLabs/google-recorder-cli`, binaries in `~/.local/bin`, and `~/.<tool>-cli` data dirs. No site/browser commands were run; no cookies, tokens, or chrome-profile contents were read.*

Name is final: **`webcli`** (free on PyPI and Homebrew; npm has only a stale 2022 placeholder, which we don't need). Data home `~/.webcli/`, umbrella command `webcli <site> <verb>`, Python package `webcli`.

---

## 0. What exists today (verified inventory)

Thirteen tools, two languages, each with its own `~/.<tool>-cli/chrome-profile/` and its own copies of the same machinery (browser launch, request allowlist, token redaction, output formatting, exit codes).

| Binary(ies) | Package / repo | Lang | Data dir (size) | Shape |
|---|---|---|---|---|
| `cgpt` | `chatgpt_web_cli` / chatgpt-web-cli | Python (pipx) | `~/.chatgpt-web-cli` (**4.2 G**) | headless daemon (`serve`) + launchd `com.truefrontier.cgpt` |
| `gweb` | `gemini_web_cli` / gemini-web-cli | Python (pipx) | `~/.gemini-web-cli` (524 M) | per-run Chrome |
| `musecli` | `muse_cli` / muse-cli | Python (pipx) | `~/.muse-cli` (36 M) | per-run Chrome |
| `aanalysis` | `aanalysis` / artificial-analysis-cli | Python (pipx) | — (public API) | read-only, no browser |
| `fbc` | `fbc_cli` / facebook-contacts-cli | Python (pipx) | `~/.facebook-contacts-cli` (255 M) | per-run Chrome + cache |
| `gcontacts` | `gcontacts_cli` / google-contacts-cli | Python (pipx) | `~/.google-contacts-cli` (44 M) | per-run Chrome |
| `gmail` | `gmail_cli` / google-gmail-cli | Python (pipx) | `~/.gmail-cli` (305 M) | per-run Chrome |
| `gmsg` | google-messages-cli | **Node** (`dist/cli.js`) | `~/.google-messages-cli` (49 M) | per-run Chrome |
| `keep` | `keep_cli` / google-keep-cli | Python (pipx) | `~/.keep-cli` (188 M) | per-run Chrome |
| `limsg`, `linkedin-messages` | `limsg_cli` / linkedin-messages-cli | Python (pipx) | `~/.linkedin-messages-cli` (828 M) | per-run Chrome + capture/ |
| `loom` | `loom_cli` / loom-cli | Python (pipx) | `~/.loom-cli` (93 M) | per-run Chrome |
| `photos` | `photos_cli` / google-photos-cli | Python (pipx) | `~/.photos-cli` (567 M) | per-run Chrome |
| `recorder`, `recorder-mcp` | `recorder_cli` / google-recorder-cli | Python (pipx) | `~/.recorder-cli` (96 M) | per-run Chrome + MCP server |
| `ytcli` | `youtube_cli` / youtube-cli | Python (pipx) | `~/.youtube-cli` (73 M) | per-run Chrome |
| `walkie` | `walkie_cli` / walkie-cli | Python (pipx) | `~/.wp-cli` (41 M) | **local SQLite only, no browser** |
| `chtabs` | `press_chrome` / press-chrome | Python (pipx) | `~/.press-chrome` | separate: tabs bridge (out of scope, see note) |

Two patterns already point the way: **cgpt's `serve` daemon** (one hidden headless Chrome on a unix socket `~/.chatgpt-web-cli/serve.sock`, 0600 in a 0700 dir, kept alive by launchd `com.truefrontier.cgpt`) and the shared files every repo re-implements — `endpoints.py` (a fail-closed request **allowlist**), `redact.py` (token **redaction**), `browser.py` (`request_allowed` guard + profile lock), `agent_ux.py` (`agent-help`, exit codes). webcli turns those per-tool copies into one shared core.

The richest conventions already in the fleet (we keep all of these):
- **cgpt/gweb/musecli** expose `-p`/`--print` ("like `claude -p`"), `ask`, `chats`, `models`, `status`, `login`, plus `--file`, `--image-out` (full-size originals), `--format text|json`, `--timeout`, `-m/--model` with **`model_slug` fallback reporting** (`servedModel`, `--strict-model`).
- **gweb** adds `--canvas`/`--canvas-out` and `--no-footnotes` (citation footnotes on by default).
- **cgpt exit codes**: `0` ok, `2` usage, `3` not found, `4` sign-in/Cloudflare, `5` site/network, **`6` rate limited with JSON `{error:"rate_limited", message, resets_at}`**, `129/130/143` signals.
- **limsg** has the **author safety check** (`--expect-author`, refuses on mismatch, exit 4; re-reads `/me` just before a write) and dry-run-by-default writes (`--confirm`/`--yes`).
- **gcontacts** `label add|remove` is dry-run unless `--confirm`.
- **gmsg** (Node) already has `--json/--compact/--select/--csv/--quiet/--profile` and dry-run `messages send --yes`.

*Note on `chtabs`/press-chrome:* it's a separate "read Kevin's real Chrome tabs" bridge, not a logged-in-site CLI. It is **out of scope** for webcli and stays as-is.

---

## 1. One data home: `~/.webcli/`

```
~/.webcli/
├── config.toml                 # global + per-site + per-identity settings
├── identities/                 # logged-in browser profiles, keyed by identity
│   ├── google-personal/        #   chrome-profile/ + meta.toml  (identity "google:personal")
│   ├── google-work/            #   chrome-profile/ + meta.toml  (identity "google:work")
│   ├── gemini-personal/        #   a Gemini account can share google:personal OR be its own identity
│   ├── gemini-work/
│   ├── meta/                   #   Facebook + Meta Muse (one Meta login)
│   ├── linkedin/
│   └── loom/
├── sites/                      # per-site state (NOT credentials)
│   ├── chatgpt/                #   last_chat, caches, logs
│   ├── gemini/
│   ├── facebook/               #   friends cache (today ~/Projects/TRUE-fb-contacts)
│   ├── linkedin/               #   capture/, boardy-watch/
│   └── ...
├── run/                        # daemon sockets + locks, 0700
│   └── <identity>.sock         # one headless-Chrome daemon per identity (0600)
└── logs/                       # rotated, redacted
```

### Identity model (the core new idea, borrowed from OpenCLI's `--profile` + Google's `/u/<n>`)

- An **identity** = one logged-in browser profile = one `identities/<id>/chrome-profile/`. Named `provider:account`, e.g. `google:personal`, `google:work`, `meta:default`, `linkedin:default`.
- Sites declare which **provider** they log in through. All Google sites (Gemini, Gmail, Keep, Photos, Contacts, Messages, Recorder, YouTube) can share **one Google identity**, which is exactly Kevin's "one shared Google login" goal. Facebook + Muse share the **Meta** identity.
- **Two axes of multi-account**, matching how the sites actually work:
  1. **`--identity google:work`** — a *different browser profile* (separate cookies, separate login). Use when accounts are truly separate logins, or to keep personal/work isolated. This is OpenCLI's `--profile`.
  2. **`--account <n>`** — Google's in-profile `/u/<n>/` account index, when one Google identity is signed into several accounts in the same profile (Gmail/Contacts already do this today). Cheap; no second login.
- **Two Gemini accounts**, concretely, either way:
  - `webcli gemini ask --identity google:personal "…"` and `webcli gemini ask --identity google:work "…"` (two profiles), **or**
  - `webcli gemini ask --account 0` / `--account 1` if both live under one Google identity.
- Resolution order for identity/account: explicit `--identity`/`--account` → `$WEBCLI_IDENTITY`/`$WEBCLI_ACCOUNT` → per-site default in `config.toml` → the site's provider default identity, `--account 0`.

`config.toml` sketch:
```toml
[core]
default_format = "json"          # tmux panes are TTYs; agents want json
lock_wait = 30.0
daemon = true                    # share a headless Chrome per identity

[identities.'google:personal']
provider = "google"
[identities.'google:work']
provider = "google"
[identities.'meta:default']
provider = "meta"

[sites.gemini]
identity = "google:personal"
[sites.gmail]
identity = "google:personal"
account = 0
[sites.linkedin]
identity = "linkedin:default"
expect_author = "kevnk"          # limsg's author safety check, preserved
```

---

## 2. One umbrella command + shared core

### Command shape
`webcli <site> <verb> [args] [--identity id] [--account n] [--format …] [--dry-run|--confirm]`
plus top-level management verbs:
- `webcli sites` — list installed site adapters, coverage, which identity each uses.
- `webcli sites add <name> [--from-capture FILE]` — scaffold a new adapter from a HAR/capture (see §2.4).
- `webcli identity list|login|status` — `webcli identity login google:work` opens a **headful** Chrome once for sign-in; everything else runs hidden.
- `webcli serve` / `webcli daemon` — manage the per-identity headless daemons and launchd agents.
- `webcli doctor` — health: daemons, identities signed-in state, profile locks, keychain reachability.
- `webcli agent-help [--format json]` — the compact agent reference, fleet-wide.

### 2.1 Adapter layout (Python, borrowing OpenCLI's plugin model)

- Adapters are Python modules discovered two ways:
  1. **Built-in**, shipped in the `webcli.sites` package (`webcli/sites/chatgpt/…`).
  2. **Plugins**, via Python **entry points** (`[project.entry-points."webcli.sites"]`) so a separate pip/pipx package can add a site, **and** a lightweight drop-in dir `~/.webcli/plugins/<name>/` scanned at startup (OpenCLI-style, for quick local adapters without packaging).
- An adapter declares: `site` name, `provider`/identity, verbs, each verb's **strategy**, args, output `columns`, and whether it writes.

```python
# webcli/sites/gemini/__init__.py  (illustrative)
from webcli.adapter import site, verb, Strategy

@site(name="gemini", provider="google")
class Gemini:
    @verb(strategy=Strategy.UI, writes=True)            # DOM composer
    def ask(self, ctx, prompt, model=None, thinking=None,
            file=(), image_out=None, canvas=False, no_footnotes=False): ...

    @verb(strategy=Strategy.INTERCEPT)                  # read the site's own API
    def chats(self, ctx, limit=50): ...
```

### 2.2 Adapter strategies (OpenCLI's four, matched to what our tools already do)

| Strategy | Meaning | Who uses it today |
|---|---|---|
| `PUBLIC` | plain HTTP, no login (Node fetch) | aanalysis; walkie is a local-DB variant |
| `COOKIE` | in-page `fetch(..., {credentials:"include"})` against the site's internal API | cgpt/gweb reads (`/backend-api/…`) |
| `INTERCEPT` | capture the site's own XHR/fetch responses | gmail attachments, limsg capture |
| `UI` | drive the DOM/composer | cgpt/gweb/muse `ask`, limsg `post`, gcontacts `label` |

Adapters pick per-verb, exactly like today (a `chats` read is COOKIE/INTERCEPT; an `ask` write is UI).

### 2.3 Shared core (one implementation, replacing N copies)

- **Headless browser daemon** — generalize cgpt's `serve`: **one hidden headless Chrome per identity**, on a unix socket in `~/.webcli/run/<identity>.sock` (0600 in a 0700 dir), one JSON line per request, profile-locked, auto-recycled (`WEBCLI_SERVE_RECYCLE_HOURS`), launchd agents `com.truefrontier.webcli.<identity>`. Commands sharing an identity queue on its lock (today's `--lock-wait`). `WEBCLI_DAEMON=0` runs in-process like cgpt. **Hidden by default; only `identity login` and `--headed` show a window** (directly answers Kevin's "cgpt popping windows" complaint, fleet-wide).
- **Capture/replay** — one HAR capture + endpoint-inference path feeding `sites add` (today's per-repo `scripts/probe.py`).
- **RPC allowlists** — one `request_allowed` guard; each adapter ships an `endpoints.py`-style **fail-closed** table (hosts, read paths, the few read-POSTs, and the explicit write requests). Anything unlisted is aborted. This is cgpt's model, generalized.
- **Token redaction** — one `redact.py` for all tools (JWTs, cookies, signed URLs, device ids). Every log/error/capture passes through it.
- **Output** — unified `--format json|table|md|csv` (+ `--compact`, `--select`, `--quiet`), JSON default when piped; `{ "ok": true, "data": … }` / `{ "ok": false, "error": {code,message,hint,…} }` envelope (OpenCLI v2 shape, superset of what gmsg already emits).
- **Writes** — unified `--dry-run` (default for writes) / `--confirm` (alias `--yes`), plus the **author/identity safety check** generalized from limsg: a write re-reads "who am I" and refuses on mismatch against `expect_author`/expected account (exit 4), never retries a possibly-live write.

### 2.4 `webcli sites add` scaffolding
`webcli sites add muse --from-capture muse.har` → infers hosts/paths/auth, writes a starter adapter under `~/.webcli/plugins/muse/` with a fail-closed allowlist and a `status`/`ask` stub, then `webcli sites test muse` validates structure offline. (Mirrors OpenCLI's `adapter init/test` + Printing-Press sniffing.)

### 2.5 Unified exit-code table (superset of cgpt's, incl. rate limit + resets_at)

| Code | Meaning |
|---|---|
| 0 | ok |
| 2 | usage error (bad option/arg/id/format) |
| 3 | not found / no results |
| 4 | sign-in needed, Cloudflare visible check, or **author/identity mismatch** on a write |
| 5 | site or network error (profile busy, timeout, site changed, site returned an error) |
| 6 | **rate limited** — JSON `{error:"rate_limited", message, resets_at}` (ISO-8601, America/Chicago or null); also `model_fallback` under `--strict-model` |
| 7 | config error (missing/invalid config or identity) |
| 129/130/143 | SIGHUP / Ctrl-C / SIGTERM (command cancelled) |

*(New vs today: a dedicated `7` for config, so `4` stays "auth/identity". Everything else matches cgpt's existing table. Flagged for Kevin.)*

---

## 3. Full command list, per site — every current command mapped to `webcli`

Legend: **[keep]** same behavior, new spelling · **[new]** not in today's tool (flagged for Kevin's approval) · flags shown are the notable ones; all inherit shared `--identity/--account/--format/--quiet`.

### ChatGPT (`cgpt` → `webcli chatgpt`)
| Today | webcli | Notes |
|---|---|---|
| `cgpt -p …` | `webcli chatgpt -p …` | [keep] print mode; `-c/--continue`, `--chat`, `-m`, `--strict-model`, `--file`, `--image-out`, `--prompt-file`, `--output-file`, `--output-format` |
| `cgpt ask …` | `webcli chatgpt ask …` | [keep] the writing verb; `--file`, `--image-out` (full-size), `-m`, `--strict-model` |
| `cgpt chats list/show` | `webcli chatgpt chats list/show` | [keep] |
| `cgpt models` | `webcli chatgpt models` | [keep] `model_slug` reporting |
| `cgpt status` | `webcli chatgpt status` | [keep] |
| `cgpt login` | `webcli identity login google:personal` | [keep] moves to shared identity login (ChatGPT uses its own `chatgpt:default` identity — **not** Google; see open decision D3) |
| `cgpt serve --install-launchd …` | `webcli serve …` / `webcli chatgpt serve` | [keep] per-identity daemon |
| `cgpt agent-help` | `webcli chatgpt agent-help` / `webcli agent-help` | [keep] |

### Gemini (`gweb` → `webcli gemini`)
| Today | webcli | Notes |
|---|---|---|
| `gweb -p` / `ask` | `webcli gemini -p` / `ask` | [keep] incl. `--thinking`, `--canvas`, `--canvas-out`, `--no-footnotes` (citations on by default), `--image-out`, `--file`, `-m` |
| `gweb chats` | `webcli gemini chats` | [keep] |
| `gweb models` | `webcli gemini models` | [keep] |
| `gweb status` | `webcli gemini status` | [keep] |
| `gweb login` | `webcli identity login google:personal` | [keep] shared Google identity |

### Meta Muse (`musecli` → `webcli muse`)
| `musecli -p` / `ask` / `chats` / `models` / `status` / `login` | `webcli muse …` | [keep] (models is intentionally empty); login via `meta:default` identity |

### Artificial Analysis (`aanalysis` → `webcli aa`)
| `aanalysis models list` | `webcli aa models` | [keep] PUBLIC, no browser |
| `aanalysis digest smartest/smart-fast/smart-cheap/open/coding/pick/all` | `webcli aa digest <kind>` | [keep] |
| `aanalysis tts models/digest` | `webcli aa tts …` | [keep] |
| `aanalysis config` | `webcli aa config` | [keep] |

### Facebook (`fbc` → `webcli facebook`)
| `fbc sync / search / about / enrich / export / status / login` | `webcli facebook …` | [keep]; friends cache moves under `~/.webcli/sites/facebook/` (today `~/Projects/TRUE-fb-contacts` — **open decision D4**) |

### LinkedIn (`limsg` → `webcli linkedin`)
| `limsg messages list/history/read/send` | `webcli linkedin messages …` | [keep] `send` dry-run default, `--yes/--send` |
| `limsg activity comments/reactions/posts/export` | `webcli linkedin activity …` | [keep] |
| `limsg post create` | `webcli linkedin post create` | [keep] dry-run default; **author safety check** (`--expect-author`, exit 4 on mismatch) |
| `limsg boost list/stats` | `webcli linkedin boost …` | [keep] |
| `limsg status/login` | `webcli linkedin status` / `webcli identity login linkedin:default` | [keep] |

### Gmail (`gmail` → `webcli gmail`)
| `gmail filters list/create/delete` | `webcli gmail filters …` | [keep] filters-first, writes stay `--confirm`/dry-run per today |
| `gmail login` | `webcli identity login google:personal` | [keep] |

### Google Messages (`gmsg`, Node → `webcli messages`)
| `gmsg conversations list/show` | `webcli messages conversations …` | [keep] |
| `gmsg messages send --yes` | `webcli messages send --yes` | [keep] dry-run default |
| `gmsg status / login(pair)` | `webcli messages status` / `webcli identity login google:personal` | [keep] **port Node → Python** (open decision D2) |

### Google Keep (`keep` → `webcli keep`)
| `keep list/get/info/search/login` | `webcli keep …` | [keep] read-only |

### Google Photos (`photos` → `webcli photos`)
| `photos list/search/info/albums/download/login` | `webcli photos …` | [keep] `download` = original file |

### Google Contacts (`gcontacts` → `webcli contacts`)
| `gcontacts list/get/search/other/labels/export/status` | `webcli contacts …` | [keep] |
| `gcontacts label add/remove --confirm` | `webcli contacts label add/remove --confirm` | [keep] batch label, dry-run default |

### Google Recorder (`recorder` → `webcli recorder`)
| `recorder list/info/search/download/transcript/sync/login` | `webcli recorder …` | [keep] |
| `recorder mcp install/serve/status/uninstall` + `recorder-mcp` | `webcli recorder mcp …` / `webcli-recorder-mcp` | [keep] MCP server kept (open decision D5: fleet-wide MCP) |
| `recorder skill …` | `webcli recorder skill …` | [keep] |

### YouTube (`ytcli` → `webcli youtube`)
| `ytcli history/status/login` | `webcli youtube …` | [keep] read-only watch history |

### Loom (`loom` → `webcli loom`)
| `loom list/info/transcript/login` | `webcli loom …` | [keep] read-only |

### Walkie (`walkie` → `webcli walkie`)
| `walkie meetings/dictations/status` (+ `--db`, `--account`, `--format`) | `webcli walkie …` | [keep] local SQLite only, no browser/identity |

**New things flagged (not requested by Kevin):** the only genuinely new surface is (a) the top-level management verbs `webcli sites / identity / serve / doctor / agent-help`, (b) exit code `7` for config, and (c) `sites add --from-capture` scaffolding. Everything per-site is a 1:1 keep. No per-site command is being dropped.

---

## 4. Back-compat

- **Old binary names stay as thin aliases.** Each old entry point (`cgpt`, `gweb`, `musecli`, `aanalysis`, `fbc`, `gcontacts`, `gmail`, `gmsg`, `keep`, `limsg`/`linkedin-messages`, `loom`, `photos`, `recorder`/`recorder-mcp`, `walkie`, `ytcli`) is reimplemented as a 2-line shim that calls `webcli <site> …` with the same args/flags/exit codes. Kevin's muscle memory and any scripts keep working. (Shims can live in `~/.local/bin`; the pipx venvs stay until Kevin retires them.)
- **tmuxlet targets keep working.** The tmuxlet chat backends invoke `cgpt -p` / `gweb -p` / `musecli -p` shaped commands; since those aliases forward unchanged (and the `{claude -p}` print contract is preserved verbatim), tmuxlet needs no change on day one. (Grep found no hard-coded tool paths in the tmuxlet repos, so forwarding aliases are sufficient; a later cleanup can point tmuxlet at `webcli … -p` directly.)
- **Launchd:** keep `com.truefrontier.cgpt` working during transition; webcli installs `com.truefrontier.webcli.<identity>` agents and, once verified, the old `cgpt` agent can be unloaded (Kevin's call).
- **Docs/skills that reference old names and need updating** (found in the repos): each repo's `README.md`, `AGENTS.md`/`CLAUDE.md`; the `recorder skill` (Claude Code skill) and `recorder mcp` client registrations; any `second-opinion-mcp` / agent-os skill text mentioning `cgpt`/`gweb`; tmuxlet-chat-backends docs; and the `agent-help` payloads themselves. These are **doc edits**, listed for the build phase, not changed here.

---

## 5. Storage hardening (menu for Kevin — pick per item)

From the prior-art report §5. None of this is applied now; each is a yes/no for Kevin.

| # | Option | What it buys | Effort | Kevin's call |
|---|---|---|---|---|
| H1 | **Retire cgpt's 3.3 G full-profile copy** (the `~/.chatgpt-web-cli` profile is 4.2 G today) — replace with a minimal ChatGPT-only profile | Shrinks blast radius from "whole browsing identity + password DB" to one site; frees ~3 G | Low | **Needs Kevin's OK before anything is deleted** |
| H2 | **Real Keychain instead of mock keychain** — launch Chrome without `--use-mock-keychain` so cookies key off "Chrome Safe Storage", not the public `mock_password` | Cookies no longer effectively plaintext on disk | Low–Med | ☐ |
| H3 | **Encrypted APFS sparse bundle** for `~/.webcli/identities/`, mounted only while a daemon runs | Real at-rest boundary when idle | Med | ☐ |
| H4 | **`chmod 700` + FileVault check + `tmutil addexclusion` + `.nosync`/Spotlight exclusion** on `~/.webcli` | Cheap hygiene; keeps profiles out of backups/sync/index | Low | ☐ (recommend all) |
| H5 | **Credential proxy for HTTP-replay (COOKIE/PUBLIC) sites** — loopback Agent-Vault/mitmproxy injects the Cookie/Bearer so the CLI process never holds it | Secrets out of process for API-replay verbs | Med | ☐ (not for UI/browser verbs — see §5.8 of prior-art) |
| H6 | **Capture-once + Keychain token store** (press-auth/latchkey model) for sites that allow pure replay | Removes standing browser for those sites | Med | ☐ |

Default recommendation if Kevin wants a single answer: **H1 + H2 + H4 now; H3 for the identity profiles; H5/H6 later for the pure-replay sites.** DBSC caveat applies to Google (keep a live headless profile rather than exporting cookies).

---

## 6. Phased migration plan (design only — this is the plan, not the doing)

Each phase ends with a concrete verification gate. Effort is rough calendar estimate for one engineer.

**Phase 0 — Scaffolding (no behavior change).** Create the `webcli` package skeleton, shared core modules (browser daemon, allowlist, redact, output, exit codes, identity resolver) lifted from cgpt. No sites yet. *Verify:* `webcli --help`, `webcli doctor` run; unit tests for redaction/allowlist pass. **~2–3 days.**

**Phase 1 — First site + daemon-per-identity (ChatGPT).** Port `cgpt` onto the core as `webcli chatgpt`; stand up `com.truefrontier.webcli.chatgpt`. Ship the `cgpt` alias. *Verify:* side-by-side `cgpt -p` vs `webcli chatgpt -p` identical output + exit codes; tmuxlet backend still answers; launchd agent restarts. **~2–3 days.**

**Phase 2 — Google identity + the Google sites.** Introduce `google:personal`/`google:work` identities and `--account`; port gweb, gmail, keep, photos, contacts, youtube, recorder (incl. MCP). *Verify:* one `identity login google:personal` logs in all seven; `--account 0/1` works on Gmail/Contacts; recorder MCP still registers; per-site `status` green. **~1–1.5 weeks.**

**Phase 3 — Meta + LinkedIn + Loom + AA + Walkie.** Port muse+facebook (Meta identity), limsg (author safety intact), loom, aanalysis, walkie. *Verify:* limsg dry-run author-mismatch still exits 4; AA/walkie need no identity. **~1 week.**

**Phase 4 — Node → Python Messages.** Reimplement `gmsg` as `webcli messages` (open decision D2); keep the `gmsg` alias. *Verify:* conversations list/show + dry-run send parity. **~2–4 days.**

**Phase 5 — Hardening + cleanup.** Apply chosen §5 items; retire old launchd agent; update docs/skills/agent-help; optionally retire old pipx venvs. *Verify:* `doctor` clean; storage choices in place; docs reference `webcli`. **~3–5 days.**

**Total rough effort: ~4–6 weeks.**

### Risks
- **Google re-login / DBSC.** Consolidating onto one Google identity may force a fresh sign-in, and DBSC (now on macOS Chrome, gradual) can expire exported cookies within hours — so keep a **live headless profile** per identity rather than moving cookies around; expect an interactive `identity login` per provider. *Mitigation:* do the login during Phase 2 with Kevin present; never automate the Google login form.
- **Shared-profile contention.** Many Google sites behind one headless Chrome means a hot lock. *Mitigation:* the per-identity daemon + queue (today's `--lock-wait`) and `WEBCLI_SERVE_RECYCLE_HOURS`; allow `--identity` splits for heavy users.
- **Cloudflare visible checks** (ChatGPT) still need a human occasionally (exit 4). Unchanged from today.
- **Author/account safety during writes** must survive the port exactly (limsg). Covered by Phase 3 verification.
- **Node port risk** (Messages) — behavior drift. *Mitigation:* keep the Node tool and alias until parity is proven.
- **Nothing deleted without Kevin's OK** (esp. H1's 3.3 G copy and old venvs).

---

## 7. Open decisions for Kevin
- **D1 — Identity granularity for Google.** One shared `google:personal` for all Google sites with `--account <n>` for extra accounts (simplest), **or** separate identities per account (`google:personal`, `google:work`) for stronger isolation? (Can support both; need the default.)
- **D2 — Port Google Messages from Node to Python now, or keep the Node tool behind a `webcli messages` shim** for the first release?
- **D3 — ChatGPT & Muse identities.** ChatGPT (`chatgpt:default`) and Muse (`meta:default`) are non-Google logins — confirm they're separate identities (they are today). Does Muse really share the Facebook Meta login, or is it its own?
- **D4 — Relocate caches into `~/.webcli/sites/`?** e.g. Facebook's `~/Projects/TRUE-fb-contacts` and LinkedIn's capture dir. Move (clean) or leave in place (less churn)?
- **D5 — MCP scope.** Keep MCP only for Recorder (today), or expose the whole fleet over one `webcli-mcp` server too? (OpenCLI has none; our recorder already does.)
- **D6 — Storage hardening choices** (§5 H1–H6), especially **H1 approval to retire the 3.3 G profile copy**.
- **D7 — Alias lifetime.** How long do old binary names live before Kevin retires the pipx venvs — indefinitely, or a deprecation window?
