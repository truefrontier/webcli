# Post-login verification

Run these after you sign in to all three identities. They prove the hidden-browser promise against your real, signed-in profiles. The offline tests can't prove it, because they never start Chrome.

## 0. ChatGPT: sign in once more (required after the 2026-10-19 fingerprint change)

**Headless ChatGPT won't work until you do this once.** Every headless `llmcli chatgpt` command exits 4 (`hidden_browser_blocked`) until then. Gemini and Muse don't need it.

ChatGPT's login window now presents the same browser fingerprint as the headless commands. The `cf_clearance` Cloudflare set at your first sign-in came from the old window, and headless Chrome can't use it. Signing in again gets you a clearance from the new window.

```sh
pipx install --force -e ~/Sites/truefrontier/webcli   # already done 2026-10-19; skip unless the code changed
webcli identity login chatgpt:default
```

What you'll see:

- The page area is a fixed 1280×900, the size headless commands use. On a Retina screen it looks a little soft. That's on purpose.
- If Cloudflare shows "Verify you are human", click it. If you're already signed in, the window may go straight to the chat page.
- Wait until the command prints that you're signed in. Then the window closes.

Then re-run the ChatGPT checks:

```sh
PY=~/.local/pipx/venvs/webcli/bin/python
$PY -m webcli.scripts.window_check -- llmcli chatgpt status
$PY -m webcli.scripts.window_check -- llmcli chatgpt -p "Reply with exactly: pong"
LLMCLI_FORMAT=json llmcli chatgpt --image-out /tmp/cg-img -p "Create an image of a blue circle"
```

Pass means:

- `status` exits 0 with `"signedIn": true`.
- `-p` prints `pong`.
- Each `window_check` report ends with `"ok": true`, `"maxVisibleProfileWindows": 0`, and `"profileChromeActivated": false`.
- The image command saves an image, and its JSON has a non-null `servedModel`.

If headless still exits 4 after the re-login, stop and report it with the `status` JSON. Don't keep logging in. The next suspects are listed in [chatgpt-cloudflare-fix.md](chatgpt-cloudflare-fix.md), under "What still can't match".

## 1. Sign in (you, at the window)

```sh
webcli identity login google:default
webcli identity login chatgpt:default
webcli identity login meta:default      # Muse: a Meta account (Facebook, Instagram, email, or phone)
```

Muse's window passes through auth.muse.ai, facebook.com, and instagram.com before its login form shows. If Muse puts the account on a waitlist, invite, or disclosure page, finish it in the same window: sign-in counts only once the chat page shows.

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
$PY -m webcli.scripts.window_check -- llmcli muse status
$PY -m webcli.scripts.window_check -- llmcli muse chats list --limit 2
$PY -m webcli.scripts.window_check -- llmcli muse -p "Reply with exactly: pong"
```

Each report ends with `"ok": true`, `"maxVisibleProfileWindows": 0`, and `"profileChromeActivated": false`. Add `--report FILE` to keep a copy.

## 3. Check the rest

```sh
webcli doctor                          # all three identities "signedIn": true, home "ok": true
llmcli serve --identity google:default --status
llmcli serve --identity chatgpt:default --status
llmcli serve --identity meta:default --status
llmcli muse status                     # {"signedIn": true, "access": "ok", ...}; exit 4 if signed out or held
```

### Muse specifics

- Muse's chat streams over an end-to-end encrypted WebSocket to the account's cloud VM (`wss://*.metaaivm.com`). It is the only socket the guard lets through, and only from the running command's tab. If `llmcli muse -p` times out with "composer never appeared", the error lists what the guard blocked; `python -m webcli.core.capture muse` shows the page's traffic (redacted).
- Muse's HTTP guard (ported from musecli, which was checked live on 2026-10-15) allows GETs to any host except sign-out, because sign-in hops through Meta's hosts and Muse's CDN hosts aren't mapped yet. POSTs pass only from its table (session check, VM token, VM wake); everything else is aborted.
- `llmcli muse models` prints `[]` and opens no browser: muse.ai has no model picker.
- `llmcli muse chats list` opens each side chat to read its id, which may mark it seen (as a browser would).

## What was already checked before sign-in (2026-10-19)

- Signed-out `llmcli gemini status` and `llmcli chatgpt status` exit 4 and start no Chrome.
- Added later the same day: signed-out `llmcli muse status` exits 4 and starts no Chrome (no `meta:default` profile yet).
- On throwaway identities (`google:livecheck`, `chatgpt:livecheck`, removed afterward), the daemon started on demand, ran Chrome headless as a background app with no Dock icon, and answered signed-out `status` with exit 4. `window_check` reported no window and no focus change.
- `~/.webcli` got 0700, the Time Machine exclusion, and the Spotlight/`.nosync` markers on first creation.

## Checked after the fingerprint change (2026-10-19, afternoon, before the ChatGPT re-login)

- The idle `google:default` and `meta:default` daemons were stopped so they'd load the new code. Commands restart them on demand. No `chatgpt:default` daemon was running.
- Under `window_check`, these passed headless with `"ok": true`, 0 profile windows, and Chrome never activated:
  - `llmcli gemini status`: signed in
  - `llmcli gemini -p "Reply with exactly: pong"`: `pong`
  - `llmcli muse status`: signed in, access ok
  - `llmcli muse -p "Reply with exactly: pong"`: `pong`
- `llmcli chatgpt status` still exits 4 (`hidden_browser_blocked`), with 0 windows. That's expected until step 0 above.

## Not done yet (your call)

- Retiring `~/.chatgpt-web-cli` (H1) waits until this verification passes.
- tmuxlet still points at `gweb` / `cgpt`. Wiring it to `llmcli <site> -p` comes after this check.
