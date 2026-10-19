# webcli / llmcli

Agent-native CLIs over your own signed-in Chrome sessions. Chrome always runs hidden (headless). Only a sign-in, or an explicit `--headed`, shows a window.

- `llmcli`: chat AIs. `llmcli gemini` (gweb parity), `llmcli chatgpt` (cgpt parity), and `llmcli muse` (Meta's Muse at muse.ai, musecli parity).
- `webcli`: data sites. `webcli aa` (Artificial Analysis LLM and TTS leaderboards, aanalysis parity), `webcli walkie` (Walkie.app meetings and dictations, walkie-cli parity), and four read-only Google sites: `webcli keep` (Keep notes), `webcli photos` (Google Photos, with original-file downloads), `webcli youtube` (watch history, ytcli parity), and `webcli recorder` (Pixel Recorder recordings, transcripts, and audio; no MCP server). Plus `identity`, `doctor`, `sites`, and `serve`.

Both binaries come from one Python package and share one core and one data home, `~/.webcli/`.

## Install

```sh
pipx install --force -e .
```

This puts `webcli` and `llmcli` in `~/.local/bin`. It uses your installed Google Chrome (`channel="chrome"`), so `playwright install` is not needed.

## Sign in (a person, once per identity)

Each identity is one Chrome profile. There are three:

| Identity | Used by | Profile |
|---|---|---|
| `google:default` | `llmcli gemini`, `webcli keep`, `webcli photos`, `webcli youtube`, `webcli recorder` | `~/.webcli/identities/google-default/chrome-profile` |
| `chatgpt:default` | `llmcli chatgpt` | `~/.webcli/identities/chatgpt-default/chrome-profile` |
| `meta:default` | `llmcli muse` | `~/.webcli/identities/meta-default/chrome-profile` |

Sign in opens a visible Chrome window, so run it yourself, never from an agent:

```sh
webcli identity login google:default     # same as: llmcli gemini login
webcli identity login chatgpt:default    # same as: llmcli chatgpt login
webcli identity login meta:default       # same as: llmcli muse login
```

Muse signs in with a Meta account (Facebook, Instagram, email, or phone). Signed out, muse.ai passes through auth.muse.ai, facebook.com, and instagram.com looking for a Meta session, then shows its own login form.

Profiles start fresh. Nothing is copied from `~/.gemini-web-cli`, `~/.chatgpt-web-cli`, or `~/.muse-cli`.

The Google data sites have no sign-in of their own. One `webcli identity login google:default` covers Gemini, Keep, Photos, YouTube, and Recorder. YouTube keeps its own session: when it isn't signed in yet, `webcli youtube` follows YouTube's passive "Sign in" link once, which needs no page if the Google session exists. Nothing is copied from `~/.keep-cli`, `~/.photos-cli`, `~/.youtube-cli`, or `~/.recorder-cli`.

`aa` and `walkie` need no sign-in and never open Chrome:

- `aa` calls the Artificial Analysis Free Data API over HTTPS. It needs a free API key in `ARTIFICIAL_ANALYSIS_API_KEY`, or saved with `webcli aa config set-key`. To reuse aanalysis's key: `webcli aa config set-key --key-file ~/.config/artificial-analysis/api_key`.
- `walkie` reads Walkie.app's local SQLite, read-only, with no network. It finds the database itself, or takes `--db PATH` / `WALKIE_DB`.

## Use

```sh
llmcli gemini -p "What is 2+2?"                 # print mode, like claude -p
llmcli chatgpt -p -m gpt-6 "Summarize this" --file notes.md
llmcli gemini ask "Draw a fox" --image-out ~/Downloads/
llmcli chatgpt chats list --format md
llmcli muse -p --chat main "What's on my list today?"   # muse: no models, files, or images
llmcli gemini agent-help --format json          # commands, flags, exit codes, env
webcli aa digest smart-fast --limit 5           # Artificial Analysis: smart and fast LLMs
webcli walkie meetings transcript standup --format md
webcli keep search picnic --compact             # Google Keep notes (read-only)
webcli photos download AF1Qip... -o ~/Downloads # the original photo or video file
webcli youtube history --since 2026-10-01       # exact times from My Activity, else day precision
webcli recorder sync -o ~/Backups/recorder      # every transcript and audio file
webcli doctor                                   # hardening, Chrome, identities, daemons
```

Output is JSON when piped and human on a terminal. `LLMCLI_FORMAT` / `WEBCLI_FORMAT` (`json|table|text|md`) change the default.

The tmuxlet chat-backend contract works as before: `llmcli <site> -p --prompt-file P --output-file A` writes the answer text to `A`, and errors go to stderr as JSON when the format env is `json`.

## Exit codes (every site, both binaries)

| Code | Meaning |
|---|---|
| 0 | ok |
| 2 | usage error |
| 3 | not found |
| 4 | sign-in needed, or Cloudflare needs a visible check (`hidden_browser_blocked`) |
| 5 | site or network error |
| 6 | rate limited: JSON `{"error":"rate_limited","code":6,"message",...,"resets_at"}` (or `model_fallback` with `--strict-model`) |
| 7 | config error (config.toml, identity, site) |
| 128+N | killed by signal N |

## How it stays hidden and private

- Every command runs Chrome headless. `ignore_default_args` drops `--enable-automation` and `--use-mock-keychain`, so cookies stay under the real macOS Keychain key.
- One headless Chrome per identity runs in a daemon (`llmcli serve --identity ID`, socket `~/.webcli/run/<identity>.sock`, 0600). Commands start it on demand. `WEBCLI_DAEMON=0` runs Chrome in-process instead.
- A fail-closed request guard aborts anything a site's endpoint table doesn't list. Prompts go out only from `ask` / `-p`. WebSockets are closed, except Muse's chat stream to `wss://*.metaaivm.com`, which connects only from the running command's tab. That socket is encrypted, so the guard can't filter inside it; Muse's read commands type and click nothing that writes.
- Every log line and printed error goes through the redaction module.
- `~/.webcli` is 0700, excluded from Time Machine, and marked for Spotlight (`.metadata_never_index`) and sync tools (`.nosync`) when first created. `webcli doctor` re-checks it, and `webcli doctor --fix` re-applies it.

## Develop

```sh
python3 -m venv .venv && .venv/bin/pip install -e . pytest
.venv/bin/python -m pytest -q      # offline: no browser, no network, temp WEBCLI_HOME
```

After sign-in, check the hidden-browser promise live with [docs/post-login-verification.md](docs/post-login-verification.md).

See `BUILD_SPEC.md` (Phase 1) and `docs/consolidation-design.md` for the plan.
