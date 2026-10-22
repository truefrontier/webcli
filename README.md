# webcli / llmcli / chromecli

Agent-native CLIs over your own signed-in Chrome sessions. Chrome always runs hidden (headless). Only a sign-in, or an explicit `--headed`, shows a window.

- `llmcli`: chat AIs. `llmcli gemini` (gweb parity), `llmcli chatgpt` (cgpt parity), and `llmcli muse` (Meta's Muse at muse.ai, musecli parity).
- `webcli`: data sites. `webcli aa` (Artificial Analysis LLM and TTS leaderboards, aanalysis parity), `webcli walkie` (Walkie.app meetings and dictations, walkie-cli parity), four read-only Google sites: `webcli keep` (Keep notes), `webcli photos` (Google Photos, with original-file downloads), `webcli youtube` (watch history, ytcli parity), and `webcli recorder` (Pixel Recorder recordings, transcripts, and audio; no MCP server), and two Google sites with guarded writes: `webcli contacts` (Google Contacts, gcontacts parity; `label add|remove` writes only with `--confirm`) and `webcli gmail` (Gmail filters; `filters create|delete` write only with `--confirm`). Three social sites: `webcli linkedin` (messages, activity, your feed, post search, and Boost stats, limsg parity; the feed and posts commands are read-only; `messages send` and `post create` write only with `--confirm`, and only as the expected author), `webcli facebook` (your friends, fbc parity; read-only on Facebook), and `webcli loom` (your library and official transcripts, loom-cli parity; read-only). And `webcli messages` (your phone's texts through Google Messages for web, gmsg parity; `send` writes only with `--confirm`). Plus `identity`, `doctor`, `sites`, and `serve`.
- `chromecli`: the tabs you share from your real, signed-in Chrome, through the chromecli extension and a native messaging host: read them, and act in them (open, navigate, click, fill, ...) with your OK in the browser for anything risky; see [chromecli](#chromecli-tabs-you-share).

All three binaries come from one Python package and share one core and one data home, `~/.webcli/`.

## Install

```sh
pipx install truefrontier-webcli      # or: pip install truefrontier-webcli
```

This puts the `webcli`, `llmcli`, and `chromecli` commands (plus chromecli's `chromecli-host`) on your path. (`pip install truefrontier-llmcli` also works — it is a thin alias that depends on `truefrontier-webcli`.) The PyPI distribution is named `truefrontier-webcli`; the commands stay `webcli` and `llmcli`. With Homebrew: `brew install truefrontier/tap/webcli`. Both drive your installed **Google Chrome** (`channel="chrome"`), so install Chrome if you don't already have it; no separate `playwright install` step is needed.

To develop from a checkout instead, see [Develop](#develop).

## Sign in (a person, once per identity)

Each identity is one Chrome profile. There are seven:

| Identity | Used by | Profile |
|---|---|---|
| `google:default` | `llmcli gemini`, `webcli keep`, `webcli photos`, `webcli youtube`, `webcli recorder`, `webcli contacts`, `webcli gmail` | `~/.webcli/identities/google-default/chrome-profile` |
| `chatgpt:default` | `llmcli chatgpt` | `~/.webcli/identities/chatgpt-default/chrome-profile` |
| `meta:default` | `llmcli muse` | `~/.webcli/identities/meta-default/chrome-profile` |
| `facebook:default` | `webcli facebook` | `~/.webcli/identities/facebook-default/chrome-profile` |
| `linkedin:default` | `webcli linkedin` | `~/.webcli/identities/linkedin-default/chrome-profile` |
| `loom:default` | `webcli loom` | `~/.webcli/identities/loom-default/chrome-profile` |
| `messages:default` | `webcli messages` | `~/.webcli/identities/messages-default/chrome-profile` |

Sign in opens a visible Chrome window, so run it yourself, never from an agent:

```sh
webcli identity login google:default     # same as: llmcli gemini login
webcli identity login chatgpt:default    # same as: llmcli chatgpt login
webcli identity login meta:default       # same as: llmcli muse login
webcli identity login facebook:default   # Facebook (its own profile, not Muse's)
webcli identity login linkedin:default   # LinkedIn (writes refuse unless signed in as your configured author)
webcli identity login loom:default       # Loom
webcli identity login messages:default   # Google Messages: scan the QR code with your phone
```

Muse signs in with a Meta account (Facebook, Instagram, email, or phone). Signed out, muse.ai passes through auth.muse.ai, facebook.com, and instagram.com looking for a Meta session, then shows its own login form.

Profiles start fresh. Nothing is copied from `~/.gemini-web-cli`, `~/.chatgpt-web-cli`, or `~/.muse-cli`.

The Google data sites have no sign-in of their own. One `webcli identity login google:default` covers Gemini, Keep, Photos, YouTube, Recorder, Contacts, and Gmail. YouTube keeps its own session: when it isn't signed in yet, `webcli youtube` follows YouTube's passive "Sign in" link once, which needs no page if the Google session exists. Nothing is copied from `~/.keep-cli`, `~/.photos-cli`, `~/.youtube-cli`, `~/.recorder-cli`, `~/.google-contacts-cli`, or `~/.gmail-cli`.

`webcli facebook` has its own identity, `facebook:default`. It used to share Muse's `meta:default`, but Muse's sign-in leaves only Facebook's device cookies there, never its session (`c_user` and `xs`), so `webcli facebook status` stayed signed out. `webcli identity login facebook:default` finishes only once Facebook's own session is set, the account reads as signed in from `/me`, and a fresh hidden Chrome on the profile agrees. Nothing is copied from `~/.facebook-contacts-cli`, `~/.linkedin-messages-cli`, or `~/.loom-cli`; the friends cache starts fresh in `~/.webcli/sites/facebook/`.

`webcli messages` has its own identity, `messages:default`, because Google Messages for web pairs with your phone, not with a Google account. `webcli identity login messages:default` opens Messages on its QR code page. On the phone, open Google Messages, then **Device pairing > QR code scanner**, and scan the code. Leave **Remember this computer** on. Login finishes once the conversation list shows and a fresh hidden Chrome on the profile is paired too. The pairing lives in that profile. Nothing is copied from `~/.google-messages-cli`, and there is no `gmsg` alias.

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
webcli contacts search ada --compact            # Google Contacts: My Contacts, then Other contacts
webcli contacts label add Newsletter --from-label "Conference 2026"   # dry run: shows what would change
webcli gmail filters create --from alerts@example.com --skip-inbox     # dry run: prints the plan
webcli linkedin messages history Boardy --limit 5 # read-only; the guard blocks mark-as-read
webcli linkedin feed --since 48h --limit 5   # read-only: your feed, newest first
webcli linkedin posts search "AI agents" --since 2d --compact          # read-only: top posts matching a query (--sort latest)
webcli linkedin posts from jane-doe --limit 3 # read-only: one profile's (or company/NAME page's) recent posts
webcli linkedin posts read https://www.linkedin.com/feed/update/urn:li:activity:7380000000000000000/ --comments 10
webcli linkedin post create --text-file post.txt --image card.png      # dry run: checks the author, posts nothing
webcli facebook sync && webcli facebook search --city springfield      # read-only on Facebook; cache is local
webcli loom transcript https://www.loom.com/share/<id>                 # Loom's official transcript, as text
webcli messages read Alice --limit 20           # read-only: reading doesn't mark it read
webcli messages send --to Alice --text "On my way"                     # dry run: shows who it would go to
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
| 8 | sent but not verified: a confirmed write went out, but the site didn't show it afterwards (`messages send`; JSON `{"error":"sent_unverified","code":8,...}`). Check it; don't send it again |
| 128+N | killed by signal N |

## Writes

Only five data-site verbs change anything on a site, and only with `--confirm` (alias `--yes`):

- `webcli contacts label add|remove LABEL` adds the selected contacts to one of your labels or removes them from it.
- `webcli gmail filters create|delete` creates one filter or deletes one filter.
- `webcli linkedin messages send THREAD --text T` sends one DM to one existing thread (`--send` works too).
- `webcli linkedin post create --text T --image P` publishes one post: text plus one image.
- `webcli messages send --to QUERY --text T` sends one text to one existing conversation. It never starts a new one.

Without `--confirm`, each one is a dry run: it prints what it would change and exits 0. For contacts and gmail, passing `--dry-run` and `--confirm` together exits 2; for linkedin and messages, `--dry-run` wins and it stays a dry run. For a confirmed run, the request guard allows only that command's writes: one label, one direction, and only the selected contacts; one filter write, bound to that filter's id or criteria; one DM or one post, bound to its thread, sender, text, and image; or one Google Messages send. See [docs/google-data-sites.md](https://github.com/truefrontier/webcli/blob/main/docs/google-data-sites.md).

`chromecli` (a separate command) also writes, only with `--confirm` and only in tabs you share or it opened: `open`, `focus`, `close`, `navigate`, `click`, `type`, `fill`, `select`, `press` (`eval` is disabled). Without `--confirm`, or with `--dry-run`, each prints a preview and sends nothing to Chrome. Risky ones wait for your *Allow once* in a chromecli window in Chrome. See [chromecli](#chromecli-tabs-you-share).

Google Messages encrypts each message end to end between the browser and your phone, so its guard can't read the conversation or the text. The guard can read each phone action's type. It passes listing calls and blocks everything else, mark-as-read and typing included. A confirmed send first checks, in the page, that the open conversation is the one it resolved and that the compose box holds exactly your text. Only then does the guard open one slot for a single `SEND_MESSAGE`, for 10 seconds after the click. Afterwards, `confirmed` says whether the conversation showed the text. See [docs/messages-site.md](https://github.com/truefrontier/webcli/blob/main/docs/messages-site.md).

The LinkedIn writes also check who is signed in. LinkedIn's send and post calls name no author, so they go out as whichever account the profile is signed in to. Both refuse, exit 4, and write nothing unless the signed-in handle matches the expected author. No author ships as a default: set yours with `--expect-author`, `WEBCLI_LINKEDIN_EXPECT_AUTHOR`, or `expect_author` under `[sites.linkedin]` in `config.toml`. Until one is configured, every LinkedIn write refuses. A dry run with the wrong account still prints its preview, warns, and exits 4. See [docs/social-data-sites.md](https://github.com/truefrontier/webcli/blob/main/docs/social-data-sites.md).

## How it stays hidden and private

- Every command runs Chrome headless. `ignore_default_args` drops `--enable-automation` and `--use-mock-keychain`, so cookies stay under the real macOS Keychain key.
- One headless Chrome per identity runs in a daemon (`llmcli serve --identity ID`, socket `~/.webcli/run/<identity>.sock`, 0600). Commands start it on demand. `WEBCLI_DAEMON=0` runs Chrome in-process instead.
- A fail-closed request guard aborts anything a site's endpoint table doesn't list. Prompts go out only from `ask` / `-p`. WebSockets are closed, except Muse's chat stream to `wss://*.metaaivm.com`, which connects only from the running command's tab. That socket is encrypted, so the guard can't filter inside it; Muse's read commands type and click nothing that writes. Google Messages' relay calls are encrypted inside too, but their action type isn't, so that guard sorts them by type.
- Every log line and printed error goes through the redaction module.
- `~/.webcli` is 0700, excluded from Time Machine, and marked for Spotlight (`.metadata_never_index`) and sync tools (`.nosync`) when first created. `webcli doctor` re-checks it, and `webcli doctor --fix` re-applies it.

## chromecli (tabs you share)

`chromecli` reads and acts in the Chrome tabs you share with agents, in your everyday Chrome, through an MV3 extension ("chromecli", in `extensions/chromecli/` of this repo; Load unpacked) and a native messaging host (`chromecli-host`, host name `com.truefrontier.chromecli`). No CDP, no TCP port, no launchd: Chrome starts the host when the extension connects, and the host serves `~/.webcli/run/chromecli.sock` (0600).

```sh
chromecli install-host --confirm         # writes Chrome's NativeMessagingHosts/com.truefrontier.chromecli.json
chromecli status                         # connected, extension version + ID, paused, shares, policy
chromecli list                           # tabs you shared (popup: Off / Read / Act)
chromecli read 123 --as md --elements
chromecli screenshot --active -o ~/Downloads/t.png
chromecli history --limit 20
chromecli agent-help --format json

# tab actions (preview unless --confirm)
chromecli open https://docs.example.com --confirm        # background tab; auto-shared per act_allow / read_allow
chromecli focus 123 --confirm | close 123 --confirm
chromecli navigate 123 https://app.example.com/x --expect-origin https://app.example.com --confirm

# page actions (Share-act + --tab + --expect-origin/--expect-url; preview unless --confirm)
chromecli click  --tab 123 --text 'Next' --role link --expect-origin https://app.example.com --confirm
chromecli fill   --tab 123 --selector '#email' --value-file ~/v.txt --expect-origin https://app.example.com --confirm
chromecli select --tab 123 --selector '#country' --value US --expect-origin https://app.example.com --confirm
chromecli press Enter --tab 123 --ref e7 --expect-origin https://app.example.com --confirm
chromecli wait   --tab 123 --text 'Saved' --timeout 15
chromecli find   --tab 123 'the email field'                # Jev if you saved a TypeSafe key, else heuristics
chromecli click  --tab 123 --find 'the save button' --expect-origin https://app.example.com --confirm
```

Only tabs you share in the toolbar popup are visible; a share ends when the tab goes to another origin or Chrome restarts. *Pause agents* (popup, or Cmd+Shift+Period) makes everything but `status` exit 4. Banks, sign-in pages, password managers, and checkout/cart paths are denied by default (`[chromecli]` in `config.toml`); config changes that widen access wait for you to Accept them in the popup. **Writes:** every action prints a `{"dryRun": true, ...}` preview and sends nothing unless `--confirm` (and `--dry-run` wins). Risky ones (submit-like clicks such as Place order / Send / Delete, Enter in a form field, typing into password / payment / one-time-code fields, opening, or navigating to another origin, outside `read_allow`/`act_allow`) also open a chromecli window in Chrome where you click *Allow once* or *Deny* (60s; Deny is the default). Deny / timeout / Pause exit 4; a page not at `--expect-*` exits 5 `url_mismatch`; an action that went out but failed its read-back or `--verify-*` check exits 8 `sent_unverified`. `eval` is disabled (exit 7): Chrome's MV3 isolated-world CSP forbids running code strings. Typed values are logged only as `<N chars sha256:8>`. **Jev (optional, bring your own key):** paste a TypeSafe API key in the extension's options (popup → Jev → Options) and `find` / `--find` rank elements with Jev, and clicks are classified: in tabs you Share-act yourself, a click skips Jev's prompt when Jev is ≥ 70% sure it's safe (the act-tab safe floor in options), and same-origin moves don't ask; in tabs an agent opened, anything Jev isn't ≥ 0.9 sure is safe asks you. Jev only adds prompts, never removes one. The key stays in the extension (only its service worker sends it, to api.typesafe.ai); without a key, heuristics are used. Exit 5 `bridge_down` means Chrome or the extension isn't connected. Full reference: [docs/chromecli.md](https://github.com/truefrontier/webcli/blob/main/docs/chromecli.md).

## Develop

```sh
python3 -m venv .venv && .venv/bin/pip install -e . pytest
.venv/bin/python -m pytest -q      # offline: no browser, no network, temp WEBCLI_HOME
node --test extensions/chromecli/test   # the chromecli extension's pure modules and a mocked service worker
```

After sign-in, check the hidden-browser promise live with [docs/post-login-verification.md](https://github.com/truefrontier/webcli/blob/main/docs/post-login-verification.md).

See [`BUILD_SPEC.md`](https://github.com/truefrontier/webcli/blob/main/BUILD_SPEC.md) (Phase 1) and `docs/consolidation-design.md` for the plan.
