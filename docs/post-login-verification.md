# Post-login verification

Run these after you sign in to both identities. They prove the hidden-browser promise against your real, signed-in profiles. The offline tests can't prove it, because they never start Chrome.

## 1. Sign in (you, at the window)

```sh
webcli identity login google:default
webcli identity login chatgpt:default
```

## 2. Check that no command shows a window

`webcli/scripts/window_check.py` runs a command while it samples every on-screen window, the frontmost app, and each app activation. It fails (exit 1) if a Chrome on a `~/.webcli` profile shows a window or takes focus. It needs `swiftc`, which ships with the Xcode command-line tools.

```sh
PY=~/.local/pipx/venvs/webcli/bin/python
$PY -m webcli.scripts.window_check -- llmcli gemini status
$PY -m webcli.scripts.window_check -- llmcli chatgpt status
$PY -m webcli.scripts.window_check -- llmcli gemini chats list
$PY -m webcli.scripts.window_check -- llmcli chatgpt models
$PY -m webcli.scripts.window_check -- llmcli gemini -p "Reply with exactly: pong"
$PY -m webcli.scripts.window_check -- llmcli chatgpt -p "Reply with exactly: pong"
```

Each report ends with `"ok": true`, `"maxVisibleProfileWindows": 0`, and `"profileChromeActivated": false`. Add `--report FILE` to keep a copy.

## 3. Check the rest

```sh
webcli doctor                          # both identities "signedIn": true, home "ok": true
llmcli serve --identity google:default --status
llmcli serve --identity chatgpt:default --status
```

## What was already checked before sign-in (2026-10-19)

- Signed-out `llmcli gemini status` and `llmcli chatgpt status` exit 4 and start no Chrome.
- On throwaway identities (`google:livecheck`, `chatgpt:livecheck`, removed afterward), the daemon started on demand, ran Chrome headless as a background app with no Dock icon, and answered signed-out `status` with exit 4. `window_check` reported no window and no focus change.
- `~/.webcli` got 0700, the Time Machine exclusion, and the Spotlight/`.nosync` markers on first creation.

## Not done yet (your call)

- Retiring `~/.chatgpt-web-cli` (H1) waits until this verification passes.
- tmuxlet still points at `gweb` / `cgpt`. Wiring it to `llmcli <site> -p` comes after this check.
