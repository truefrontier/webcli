# Port spec: `llmcli muse` (Meta Muse AI chat at muse.ai)

Add a third llm site, **`muse`**, to the existing webcli/llmcli project. This is an ADDITION — do not change the gemini/chatgpt sites or the shared core except where noted. Follow the patterns the gemini and chatgpt adapters already established (read `webcli/sites/gemini/` and `webcli/sites/chatgpt/`). `BUILD_SPEC.md` still governs the project.

## What Muse is (confirmed from the source)
- `muse` = **Meta's Muse AI chat at https://muse.ai** (Next.js app, internal codename "hatch"). This is the Meta AI chat product, NOT muse.ai video hosting — the old video-host ("Skiv") table lives on muse-cli's abandoned `skiv` branch; ignore it.
- **Login is a Meta account.** Signed out, muse.ai 307s to auth.muse.ai, cycles through facebook.com / instagram.com to find an existing Meta session, then renders muse.ai's native login form (FB/IG account cards, email, passkey). So the identity is **`meta:default`** (provider `meta`). Record this in docs.
- Muse uses **WebSockets** to `*.metaaivm.com` for the chat stream (every other socket is blocked). The adapter/guard must allow that socket host suffix and nothing else.

## Source to port (read-only reference — do NOT modify it)
`~/Sites/truefrontier/muse-cli/muse_cli/`: `cli.py`, `browser.py`, `fetch.py`, `parse.py`, `endpoints.py`, `redact.py`, `agent_ux.py` (~1670 lines total). Port its logic onto the shared core exactly as gemini/chatgpt were ported. Muse-specific redaction rules in `muse_cli/redact.py` should be folded into / reused with the shared `webcli/core/redact.py` (add any Muse-specific patterns; never weaken existing ones).

## Command surface (full musecli parity)
- `llmcli muse -p PROMPT` / `llmcli muse ask PROMPT`: `--new` (default), `--chat ID` (`main` = the main chat), `--timeout` (default 300), `--headed`, `--format`, `-q`. This is the only command that writes/sends.
- `llmcli muse chats list` and `chats show ID` (READ-ONLY).
- `llmcli muse models` — always an EMPTY list (muse.ai has no model picker); keep that behavior.
- `llmcli muse status` — signed-in Muse account + access state; exit 4 when signed out.
- `llmcli muse login` — delegate to `identity login meta:default` (opens a visible window; a person only).

## Uniform llmcli contract (same as gemini/chatgpt)
- `-p`, `--prompt-file PATH`, `--output-file PATH`, `-` for stdin; answer on stdout like `claude -p`.
- Uniform JSON fields on ask: `answer`, `chatId`, `requestedModel` (null), `servedModel` (null), `servedModelDisplay` (null), `fallback` (false), `images` ([]), `citations` ([]), `canvas` (null). Muse has no models/files/images/canvas, so those are null/empty — but the fields MUST be present and identical in shape to the other sites.
- Same unified exit-code table as the other sites (0/2/3/4/5/6/7/128+N; 6 = rate_limited with resets_at; 4 on sign-in needed / hidden-browser-blocked).

## Core requirements (unchanged, must hold for muse too)
- Runs through the **per-identity headless daemon** on `meta:default`; never shows or focuses a window (only `identity login` / `--headed`).
- H2: launches with `--use-mock-keychain` and `--enable-automation` dropped (shared browser already does this).
- Guard **fail-closed** using Muse's endpoint tables (READ_POSTS, SEND_POSTS, SESSION_POSTS, SIDE_EFFECT_GETS, AUTH_HOSTS, APP_HOSTS, and the `.metaaivm.com` SOCKET_HOST_SUFFIXES). Port them verbatim from `muse_cli/endpoints.py`. Sends allowed only in send mode.
- Every log line and printed error passes through redaction.

## Registration
- New package `webcli/sites/muse/` with the adapter object, registered in `pyproject.toml` under `[project.entry-points."webcli.sites"]` as `muse = "webcli.sites.muse:ADAPTER"`, kind `llm`, provider `meta`, defaultIdentity `meta:default`. After editing pyproject you must reinstall (`pipx install --force -e .`) so the entry point is picked up.
- It must appear in `webcli sites` under `llmcli` and in `llmcli agent-help`.

## Tests (offline, no browser/network; temp WEBCLI_HOME via conftest)
Mirror the gemini/chatgpt test files for muse: parse tests (port muse fixtures if any), guard fail-closed (incl. the WebSocket host allow/deny), cli/ask uniform-JSON-fields test, a no-visible-window launch-args test (headless=True, channel=chrome, both args ignored; only login/--headed visible), status-exit-4-when-signed-out, and identity resolution to `meta:default`. All of `python -m pytest -q` must stay green (currently 829 passing — add to that, break none).

## Docs
- Update `docs/post-login-verification.md` to add muse (login `webcli identity login meta:default`, window_check lines for `llmcli muse status` and `llmcli muse -p "..."`).
- Note in the adapter module docstring and in docs that Muse logs in via a Meta (Facebook/Instagram) account and streams over `*.metaaivm.com` WebSockets.

## Do / don't
- Do NOT log in, run `identity login`, or open any visible window anywhere in code or tests.
- Do NOT modify muse-cli or `~/.muse-cli`, or any other old tool/data.
- Commit in logical chunks, push to `origin main`, reinstall the binary, and leave tests green. Print a summary, the pytest count, and the final commit SHA.
