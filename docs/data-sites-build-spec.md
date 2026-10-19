# webcli data sites — Phase 2 build spec (authoritative)

Build the `webcli` data-site adapters as a CLEAN REWRITE on the existing shared core,
porting behavior from the old standalone CLIs. The three `llmcli` sites
(gemini, chatgpt, muse) are DONE and are the reference pattern — mirror their
structure exactly.

## Hard constraints (do not violate)
- Clean rewrite. NO backward-compat shims, NO alias binaries for old tool names
  (no `gcontacts`, `limsg`, etc. console scripts). Only `webcli` and `llmcli` ship.
- NO MCP anywhere. Google Recorder's old `recorder mcp …` and `recorder-mcp` are
  DROPPED. Port the recorder data verbs only (list/info/search/download/transcript/sync).
  Keep `recorder skill` ONLY if it is pure-local text (no MCP); otherwise drop it and note it.
- EXCLUDE Google Messages entirely. Never import, open, or reference it. No `messages` site.
- Reads first. Every write/mutating verb defaults to --dry-run and requires --confirm
  (or the verb's existing --yes/--send) to actually act. Never perform a write during
  tests or verification.
- Headless only: no visible window, no Dock icon, no focus change for any verb except
  `identity login` (which is a person's job — do NOT run it). Mirror the no-visible-window
  test the llmcli sites have.
- Redaction: cookies/tokens never appear in output, logs, or errors. Reuse webcli.core.redact.
- Commit and push to main as you go. Run `pytest` green before each commit.

## Plugin contract (how a data site registers)
A site is a package `webcli/sites/<name>/` exposing `ADAPTER = Site(...)` from its
`__init__.py` (see webcli/sites/base.py). For a DATA site set `kind="data"` so it
appears under the `webcli` binary (llm sites use `kind="llm"` → `llmcli`).

Register each new site in BOTH places:
1. `pyproject.toml` under `[project.entry-points."webcli.sites"]`:  `<name> = "webcli.sites.<name>:ADAPTER"`
2. `webcli/sites/__init__.py` `BUILTIN` dict (so source-tree runs without installed metadata work).

`Site` fields: name, kind="data", provider (identity provider; default identity is
`<provider>:default`), summary, cli (lazy () -> click.Group), fetch (module path whose
functions the daemon runs), ops (tuple of those fn names, each takes headed=), verbs
(dict name->Strategy), login (async or None), status_op (default "status"; set None for
sites with no sign-in, e.g. aa/walkie).

Strategies (webcli.sites.base.Strategy): PUBLIC (plain HTTP, no login), COOKIE
(in-page fetch with credentials against the site's own API), INTERCEPT (read the site's
own XHR/fetch responses), UI (drive the DOM).

Per-site package files mirror the llmcli sites: `__init__.py`, `cli.py` (the click group),
`fetch.py` (daemon ops: status + the data ops), `guard.py` (fail-closed request allowlist —
host/path allowlist; WebSocket allowlist if used), `parse.py`, `selectors.py`,
`endpoints.py` as needed. Sites with no browser (aa, walkie) need no guard/selectors; their
cli.py just does the work (HTTP for aa, SQLite for walkie) and they have no daemon ops
beyond maybe none (status_op=None, ops=()).

## Shared core to REUSE (do not reimplement)
- webcli/core/browser.py, daemon.py: headless per-identity Chrome daemon + page leases.
- webcli/core/guard.py: fail-closed allowlist engine (construct a per-site policy).
- webcli/core/redact.py: redaction of tokens/cookies for all output and logs.
- webcli/core/output.py: EXIT_OK/USAGE/NOT_FOUND/AUTH/API/RATE_LIMITED/CONFIG (0/2/3/4/5/6/7),
  agent_output_options(), resolve_format, emit_json/emit_rows, die(), die_rate_limited()
  (exit 6 with resets_at), note(), AgentGroup, agent_help_command. --format json|table|md|csv.
- webcli/core/identities.py: identity resolution (provider:account).
- webcli/core/config.py, capture.py, hardening.py.
- webcli.sites.llm has LLM helpers (not for data sites) but env_help is reused by apps.

The `webcli` binary (webcli/apps/webcli.py) already auto-loads `sites.of_kind("data")`
and adds each site's cli group. Just register the adapter; no app edits needed beyond that.

## Exit codes (unified, already in output.py)
0 ok · 2 usage · 3 not found/no results · 4 sign-in needed (or Cloudflare hidden_browser_blocked) ·
5 site/network error · 6 rate limited ({error:"rate_limited",code:6,message,resets_at}) · 7 config.

## Identities
- google:default  — contacts, keep, photos, recorder, gmail, youtube  (already logged in)
- facebook:default — facebook  (its own login. Built first on Muse's meta:default, but checked
                    live that profile never held facebook.com's session (c_user, xs), so it
                    moved to provider "facebook" on 2026-10-19.)
- linkedin:default — linkedin  (NEW login; needs a person)
- loom:default    — loom  (NEW login; needs a person)
- (none)          — aa (PUBLIC HTTP, status_op=None), walkie (local SQLite, status_op=None)

## Sites to build (source repo under ~/Sites/truefrontier unless noted; command list = design §3,
## docs/consolidation-design.md §3, cross-checked against each source's actual CLI)

### aa — Artificial Analysis  (source: artificial-analysis-cli; PUBLIC, no browser/identity)
Verbs: `webcli aa models`, `webcli aa digest <smartest|smart-fast|smart-cheap|open|coding|pick|all>`,
`webcli aa tts models|digest`, `webcli aa config`. Free Data API over plain HTTPS. Strategy PUBLIC.

### walkie — Walkie.app  (source: walkie-cli; local SQLite only, no browser/network/identity)
Verbs: `webcli walkie meetings`, `webcli walkie dictations`, `webcli walkie status`
(+ `--db`, `--account`, `--format`). Read-only SQLite. No daemon, no login.

### keep — Google Keep  (source: google-keep-cli; google:default; read-only)
Verbs: `webcli keep list|get|info|search`. Read-only.

### photos — Google Photos  (source: google-photos-cli; google:default)
Verbs: `webcli photos list|search|info|albums|download`. `download` saves the ORIGINAL file.

### youtube — YouTube  (source: youtube-cli / ytcli; google:default; read-only)
Verbs: `webcli youtube history|status`. Read-only watch history.

### recorder — Google Recorder  (source: ~/Sites/CodefiLabs/google-recorder-cli; google:default)
Verbs: `webcli recorder list|info|search|download|transcript|sync`. DROP all MCP (`mcp`, recorder-mcp).
`skill` only if pure-local text, else drop and note.

### contacts — Google Contacts  (source: google-contacts-cli / gcontacts; google:default)
Verbs: `webcli contacts list|get|search|other|labels|export|status`.
WRITE: `webcli contacts label add|remove` — KEEP the batch add/remove semantics exactly;
dry-run default, requires --confirm to apply.

### gmail — Gmail  (source: google-gmail-cli; google:default)
Verbs: `webcli gmail filters list|create|delete`. Filters-first. Writes (create/delete) stay
dry-run default + --confirm, matching today.

### linkedin — LinkedIn  (source: linkedin-messages-cli / limsg; linkedin:default)
Verbs: `webcli linkedin messages list|history|read|send`, `webcli linkedin activity comments|reactions|posts|export`,
`webcli linkedin post create`, `webcli linkedin boost list|stats`, `webcli linkedin status`.
SAFETY (KEEP limsg's): `send` and `post create` default to dry-run; actually sending/posting
REFUSES unless the dry-run author matches `kevnk` (the configured account handle) — port the
`--expect-author` / author check, exit 4 on mismatch. Never send or post during the build.

### facebook — Facebook  (source: facebook-contacts-cli / fbc; provider "facebook", facebook:default)
Verbs: `webcli facebook sync|search|about|enrich|export|status`. STRICTLY read-only on Facebook
(the source has a test that the code contains no clicks — keep that guarantee). Friends cache
moves under `~/.webcli/sites/facebook/`. `enrich`'s optional Tavily (`tvly`) dependency stays
optional (LinkedIn left null when absent).

## Tests (per site)
- Command wiring: the site's verbs exist under `webcli <site>` with the right options.
- Redaction: a sample token/cookie is redacted in output/error paths.
- No visible window: mirror tests/test_no_visible_window.py for any verb that launches Chrome.
- Guard fail-closed: the per-site allowlist rejects an off-allowlist host/path.
- Write safety: write verbs are dry-run by default; contacts label batch semantics; linkedin
  author refusal (exit 4) on mismatch.
- aa/walkie: pure-logic tests (HTTP parsing / SQLite reads) with fixtures, no network.

## Build order (one group per tmuxlet session, sequential to avoid pyproject/test conflicts)
1. no-browser: aa, walkie   (validates data-site plumbing end to end)
2. google read-only: keep, photos, youtube, recorder
3. google read+write: contacts, gmail
4. social (new logins): linkedin, facebook, loom

### loom — Loom  (source: loom-cli; loom:default; read-only)
Verbs: `webcli loom list|info|transcript`. Read-only; official transcripts. Identity loom:default (NEW login).
