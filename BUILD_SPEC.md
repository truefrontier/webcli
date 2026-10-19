# webcli / llmcli — Phase 1 build spec (authoritative)

> You are a coding agent. Build **Phase 1** of the consolidated tool exactly as specified here.
> Read `docs/consolidation-design.md` for background, but **this file wins** where they differ.
> Work only inside this repo (`~/Sites/truefrontier/webcli`). Commit and push to `main` as you go.
> **Do NOT modify or delete the old tools** (`gweb`, `cgpt`, `gemini-web-cli`, `chatgpt-web-cli`, etc.) or any `~/.gemini-web-cli` / `~/.chatgpt-web-cli` data. Treat them as **read-only reference** to port from.
> **Never log in. Never run `identity login`. Never open a visible browser window.** Nothing in this build or its tests may show or focus a Chrome window.

## 0. Goal of Phase 1

One Python project, one shared core, **one data home `~/.webcli/`**, installing **two CLI binaries**:

- **`llmcli`** — chat AIs. Phase 1 ships two sites: **`llmcli gemini`** (full `gweb` parity) and **`llmcli chatgpt`** (full `cgpt` parity).
- **`webcli`** — data sites umbrella. Phase 1 ships **no site adapters yet**: only `webcli identity`, `webcli doctor`, `webcli sites` (lists installed adapters; shows none yet).

Both binaries expose the shared **identity** commands. Muse and all data-site adapters come later — just leave clean extension points.

Port the real logic from:
- `../gemini-web-cli/gemini_web_cli/` (gweb): `cli.py`, `browser.py`, `fetch.py`, `parse.py`, `images.py`, `endpoints.py`, `redact.py`, `selectors.py`, `agent_ux.py`
- `../chatgpt-web-cli/chatgpt_web_cli/` (cgpt): same modules plus `daemon.py`, and `scripts/window_check.py`, `scripts/probe.py`

Rewriting/refactoring into the shared core is expected and fine. Preserve every behavior and flag below.

## 1. Package layout

Single project, package name `webcli`, built with hatchling. Suggested layout (adjust names if cleaner, keep the shape):

```
webcli/
  pyproject.toml
  webcli/
    __init__.py
    core/
      config.py         # ~/.webcli paths, config.toml load/save
      identities.py     # identity model + resolution + login (login is interactive, never auto-run)
      browser.py        # launch_persistent_context: headless, channel="chrome", H2 keychain policy
      daemon.py         # one headless Chrome per identity on a unix socket (generalize cgpt daemon.py)
      guard.py          # fail-closed request allowlist engine (per-site endpoint tables plug in)
      redact.py         # the redaction module (port cgpt/gweb redact.py; superset)
      output.py         # agent_ux: exit codes, --format json|table|md|csv, agent-help, emit_json/csv, die()
      capture.py        # capture/replay helpers (port scripts/sniff.py + probe.py essentials)
      hardening.py      # H4: chmod 700, tmutil addexclusion, Spotlight/.nosync exclusions (apply + check)
    sites/
      __init__.py       # adapter registry via entry points (group "webcli.sites")
      base.py           # Site/verb base classes, Strategy enum (PUBLIC/COOKIE/INTERCEPT/UI)
      gemini/           # port gweb: fetch.py, parse.py, images.py, endpoints.py, selectors.py
      chatgpt/          # port cgpt: fetch.py, parse.py, images.py, endpoints.py, selectors.py, daemon glue
    apps/
      llmcli.py         # `llmcli` entry point (chat umbrella + identity)
      webcli.py         # `webcli` entry point (data umbrella + identity + doctor + sites)
    scripts/
      window_check.py   # ported from cgpt scripts; proves no visible window (live verification)
  tests/
    ...
```

`pyproject.toml` must declare:
```toml
[project.scripts]
webcli = "webcli.apps.webcli:main"
llmcli = "webcli.apps.llmcli:main"
[project.entry-points."webcli.sites"]
gemini = "webcli.sites.gemini:ADAPTER"
chatgpt = "webcli.sites.chatgpt:ADAPTER"
```
Dependencies: `playwright>=1.40`, `click>=8.1`, `rich>=13`. Python >=3.10.

## 2. Data home `~/.webcli/`

```
~/.webcli/
  config.toml
  identities/
    google-default/chrome-profile/      # identity id "google:default"  -> dir "google-default"
    chatgpt-default/chrome-profile/     # identity id "chatgpt:default" -> dir "chatgpt-default"
  run/                                   # 0700; <identity>.sock daemons, 0600; locks
  logs/                                  # redacted, rotated
  sites/                                 # per-site state (last_chat, caches) — keyed by site
```
Create lazily. On first creation apply **H4 hardening** (see 6). **Fresh profiles only** — never copy or read the old `~/.gemini-web-cli` / `~/.chatgpt-web-cli` profiles. Kevin will log in fresh.

### Identity model
- An identity id is `provider:account`; Phase 1 uses `google:default` and `chatgpt:default`. Map `:` to `-` for the dir name.
- `llmcli gemini` uses identity `google:default`. `llmcli chatgpt` uses identity `chatgpt:default`.
- Keep the model able to add more accounts later (`google:work`, `--account <n>` for Google `/u/<n>`), but **do not build or test multi-account now**. Just don't hard-code "default" so deeply that adding later is painful.
- Resolution: explicit `--identity` > `$WEBCLI_IDENTITY` > per-site default in config > the site's provider default (`<provider>:default`).

## 3. Shared core requirements

### 3.1 Browser (core/browser.py) — port from cgpt browser.py (it is the stricter of the two)
- Launch the **installed Google Chrome** (`channel="chrome"`), `launch_persistent_context(user_data_dir=<identity>/chrome-profile, headless=<mode>)`.
- **Headless is the default for every command.** Only `identity login` and an explicit `--headed` use a visible context. The hidden-browser policy from cgpt (no window, no Dock icon, no focus steal) must hold for every non-login command.
- **H2 (approved): drop `--use-mock-keychain`.** `ignore_default_args` must include BOTH `--enable-automation` AND `--use-mock-keychain`, so cookies key off the real macOS Keychain ("Chrome Safe Storage"), not Playwright's fixed `mock_password`. (cgpt already does this; gemini did NOT — fix it.)
- Keep cgpt's realistic user-agent handling for headless (strip "HeadlessChrome") so Cloudflare behaves.
- Profile lock + `--lock-wait` (env `WEBCLI_LOCK_WAIT`, default 30s) per identity, as gweb/cgpt do.
- When a Cloudflare / sign-in check can't be passed headless, raise the equivalent of cgpt's `Challenged` → exit 4 with error `hidden_browser_blocked` / `sign-in needed`. Never fall back to opening a window.

### 3.2 Daemon (core/daemon.py) — generalize cgpt daemon.py
- **One long-lived headless Chrome per identity**, shared by every command on that identity.
- Unix socket at `~/.webcli/run/<identity>.sock` (0600) inside `~/.webcli/run` (0700); one JSON line each way per request; profile-locked; auto-recycle (env `WEBCLI_SERVE_RECYCLE_HOURS`).
- launchd label **`com.truefrontier.webcli.<identity-dir>`** (e.g. `com.truefrontier.webcli.google-default`). Commands start the daemon on demand (spawn `llmcli <site> serve` or kickstart launchd when installed). `WEBCLI_DAEMON=0` runs in-process.
- Expose management: `llmcli <site> serve [--status|--stop|--install-launchd|--uninstall-launchd]`, and a core `webcli serve`/`llmcli serve` that can target `--identity`.
- The daemon and all commands **never show or focus a window**.

### 3.3 Guard (core/guard.py) — fail-closed allowlist
- Each site ships an `endpoints.py`-style table (hosts, read paths/rpcids, the few read-POSTs, explicit send/write requests). Port gemini's (`READ_RPCIDS`, `SEND_REQUESTS`, `SESSION_POSTS`, `SIDE_EFFECT_GETS`, image hosts) and chatgpt's tables verbatim.
- Anything not listed is aborted. `ask`/send requests are allowed only in send mode. Download rpcids only with `--image-out`.

### 3.4 Redaction (core/redact.py) — port cgpt redact.py (superset) 
- Must redact: Authorization/Cookie/Set-Cookie headers, bearer/basic tokens, JWTs (`eyJ...`), `sk-` API keys, session tokens, `oai-*`/`openai-sentinel-*`, `cf_clearance`/`__cf*`, device/account ids under their names, signed download URLs (Azure SAS params, `*.oaiusercontent.com`, `*.blob.core.windows.net`), and any bare token-looking string.
- **Every** line written to logs, every error printed, and every capture/sniff output MUST pass through `redact_text` / `redact_url` / `redact_headers` / `shape`. This is a hard requirement and is unit-tested (see 5).

### 3.5 Output & exit codes (core/output.py) — port agent_ux.py
- `--format json|table|md|csv` (add `md`; keep `text` as an accepted alias for the human format on the chat answer). Default: human on a TTY, **json when piped**; env `LLMCLI_FORMAT` / `WEBCLI_FORMAT` overrides the default (values json|table|text|md).
- Errors: JSON on stderr when stderr is not a TTY or the format env is json.
- `-p`/`--print` top-level print mode like `claude -p` (the `AgentGroup` trick from agent_ux.py).
- `agent-help` command on every umbrella and site: machine reference (commands, flags, exit codes, env) as JSON or markdown.
- **Unified exit-code table (every site, both binaries):**
  | code | meaning |
  |---|---|
  | 0 | ok (incl. model fallback without `--strict-model`: JSON `fallback:true`; image reply without `--image-out`: JSON `imageHint`) |
  | 2 | usage error (bad option/arg/id/format; `--file` over the site's upload limits) |
  | 3 | not found / no results (chat id, model name) |
  | 4 | sign-in needed, or Cloudflare needs a visible check (`hidden_browser_blocked`) |
  | 5 | site or network error (profile busy, timeout, site changed, site returned an error, `--image-out` got/saved no image) |
  | 6 | **rate limited**: JSON `{"error":"rate_limited","code":6,"message":...,"resets_at":<ISO-8601 or null>}`; also `--strict-model` fallback → `{"error":"model_fallback","code":6,"requestedModel","servedModel"}` |
  | 7 | config error (missing/invalid config or identity) — NEW vs today |
  | 128+N | killed by signal N (129 SIGHUP, 130 Ctrl-C, 143 SIGTERM) |

### 3.6 Writes: `--dry-run` / `--confirm`
- Provide the shared `--dry-run`/`--confirm` plumbing in core for future write verbs. Phase 1 llm sites have no gated writes (the `ask`/`-p` send is the core function, not gated). Just leave the plumbing ready.

## 4. The two llm sites — uniform contract

### 4.1 `llmcli` umbrella + uniform tmuxlet backend contract
- `llmcli <site> -p PROMPT ...` (and `llmcli <site> ask PROMPT ...`) returns the **answer on stdout** like `claude -p`.
- Support the **chat-backend contract tmuxlet already uses**: `--prompt-file PATH` (read PROMPT from file) and `--output-file PATH` (write the answer there), honoring format env. (gweb/cgpt already accept these; keep them so tmuxlet can drive llmcli with the same wrapper.) `-` means stdin.
- **Consistent JSON result fields across every llm site** (null/empty when a site lacks one):
  `answer`, `chatId`, `requestedModel`, `servedModel`, `servedModelDisplay`, `fallback` (bool), `images` (list of saved paths), `citations` (list), `canvas` (object or null).
- **Same exit codes** everywhere (table in 3.5). Rate limit → 6 with `resets_at`.

### 4.2 `llmcli gemini` — full gweb parity (port gweb cli.py + fetch/parse/images)
Commands & flags (match gweb exactly):
- `llmcli gemini -p PROMPT` / `llmcli gemini ask PROMPT` with: `--model NAME`, `--thinking [low|medium|high|l|m|h]`, `--new`, `--chat ID`, `--file FILE` (repeatable), `--image-out DIR|PATH` (full-size originals via the `c8o8Fe` full_size_image rpc), `--canvas`, `--canvas-out PATH`, `--no-footnotes` (citation footnotes ON by default; JSON `citations` always lists sources), `--timeout`, `--headed`, `--format`, `-q`.
- `llmcli gemini chats list` and `chats show ID`.
- `llmcli gemini models`, `llmcli gemini status`.
- Identity: `google:default`. (`llmcli gemini login` should delegate to `identity login google:default`.)

### 4.3 `llmcli chatgpt` — full cgpt parity (port cgpt cli.py + daemon + fetch/parse/images)
Commands & flags (match cgpt exactly):
- `llmcli chatgpt -p PROMPT` / `ask PROMPT` with: `-m/--model SLUG`, `--strict-model` (exit 6 `model_fallback`), `--new`, `--chat ID`, `--file FILE` (repeatable), `--image-out DIR|PATH` (full-size originals), `--timeout`, `--headed`, `--format`, `-q`. JSON reports `requestedModel`/`servedModel`/`servedModelDisplay`/`fallback`.
- `llmcli chatgpt chats list` / `chats show ID`.
- `llmcli chatgpt models`, `llmcli chatgpt status`.
- `llmcli chatgpt serve [--status|--stop|--install-launchd|--uninstall-launchd]` (the per-identity daemon).
- Exit 4 on Cloudflare/login (`hidden_browser_blocked`). Identity: `chatgpt:default`.

## 5. Tests (required)

Use pytest. Tests must run **offline, with no browser launch and no network** (mock/patch the browser layer). Point `$WEBCLI_HOME` (or equivalent) at a temp dir so nothing touches real `~/.webcli`. Required coverage:

1. **Redaction runs** (hard requirement from Kevin):
   - Unit tests of `redact_text`/`redact_url`/`redact_headers`/`shape` on cookies, JWTs, `sk-` keys, bearer tokens, `oai-did`, `cf_clearance`, Azure-SAS signed URLs, `__Secure-next-auth.session-token` — assert the secret does not survive.
   - An **integration-style test that the logging/error path redacts**: feed a fake error/log line containing a token through the code that writes logs / prints errors and assert `<redacted>` and no raw token in the output.
2. **No visible window** (hard requirement):
   - Assert the browser launch options for every non-login command are `headless=True`, `channel="chrome"`, and `ignore_default_args` contains BOTH `--use-mock-keychain` and `--enable-automation`; assert no code path for `ask/-p/chats/models/status/serve` ever requests a headed/visible context.
   - Assert only `identity login` and explicit `--headed` can produce a visible context.
   - Wire `scripts/window_check.py` as the **live** no-window check for the post-login verification phase (document the command; it needs a login so it is not run in CI).
3. **Exit-code table**: `agent-help --format json` lists all codes incl. 6 with `resets_at` semantics and 7 for config; a simulated rate-limit path returns 6 with a JSON `resets_at` field.
4. **Guard fail-closed**: an unlisted rpcid/host/POST is blocked; a listed read passes; send requests blocked unless in send mode.
5. **Output**: json emitted when stdout is piped; uniform llm JSON field set present (answer/requestedModel/servedModel/images/citations/canvas) with a mocked site response.
6. **Identity resolution**: `google:default`/`chatgpt:default` resolve to the right dirs; `--identity` and env override.

All tests must pass: `python -m pytest -q`.

## 6. Storage hardening (apply the approved subset)

- **H2** — handled in browser (drop `--use-mock-keychain`), above.
- **H4** — in `core/hardening.py`, applied when `~/.webcli` is first created and re-checked by `webcli doctor`:
  - `chmod 700 ~/.webcli` (and 0700 `run/`, 0600 sockets).
  - `tmutil addexclusion ~/.webcli` (Time Machine).
  - Spotlight exclusion: drop a `.metadata_never_index` file in `~/.webcli` (and/or `.nosync`).
  - `webcli doctor` reports FileVault status (read-only check), each exclusion's state, perms, daemon/identity status, and whether each identity is signed in.
- **H1 / H3 / H5 / H6**: not in this phase. **Do NOT delete the old `~/.chatgpt-web-cli` profile** — H1 deletion happens later, only after live verification, and is Kevin's call.

## 7. Install & verify (you do this, no login)
- Install editable so both binaries land in `~/.local/bin`: `pipx install -e .` (or `pipx install --force -e .`). If pipx conflicts, a dedicated venv + `pipx`-style shim is fine, but the two binaries must be on PATH.
- `playwright install chrome` is NOT needed (we use the system Google Chrome via `channel="chrome"`).
- Verify WITHOUT logging in:
  - `webcli --help`, `llmcli --help`, `webcli doctor`, `webcli sites` (shows gemini/chatgpt registered under llmcli, none under webcli data sites), `llmcli gemini agent-help --format json`, `llmcli chatgpt agent-help --format json`.
  - `python -m pytest -q` all green.
  - A signed-out `llmcli gemini status` / `llmcli chatgpt status` must exit 4 cleanly (NOT open a window). This is the proof the hidden-browser policy holds before login.
- **Stop there.** Do not run `identity login`. Report that Kevin must run the login commands.

## 8. Git
- Commit in logical chunks with clear messages; push to `origin main` as you go (remote is already set to the private GitHub repo `truefrontier/webcli`).
- Final commit must leave the tree clean and tests green.

## 9. Out of scope for Phase 1 (do not build)
- Muse and any data-site adapters (gmail, keep, photos, contacts, linkedin, facebook, loom, youtube, recorder, aa, walkie). Google Messages is **excluded entirely** for now.
- MCP of any kind (dropped; a future `webmcp` may come later).
- Multi-account (`--account`, `google:work`) beyond leaving the extension point.
- Back-compat alias shims for old binary names (clean rewrite; none).
- tmuxlet target wiring (done later, after login + live verification).
