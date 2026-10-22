# webcli whatsapp

Your WhatsApp chats through WhatsApp Web (`web.whatsapp.com`), in a hidden Chrome linked to your phone. Unofficial:
WhatsApp has no API for personal accounts, so every command reads the rendered page. Read-only except `send`.

## Link (a person, once)

```sh
webcli whatsapp login          # same as: webcli identity login whatsapp:default
```

A visible Chrome window opens on WhatsApp Web's QR code. On the phone: **WhatsApp > Settings > Linked devices > Link
a device**, and scan it. Login ends when the chat list shows and a fresh hidden Chrome on the same profile reads as
linked. The link lives in `~/.webcli/identities/whatsapp-default/chrome-profile` and never leaves the computer; the
phone lists it under Linked devices (log it out there to unlink). No agent scans the code or works around it.
WhatsApp can log a linked device out (for example after the phone goes unused for a long stretch); `webcli whatsapp status` checks, `login` again
to relink.

## Commands

```sh
webcli whatsapp status                                   # exit 4 when not linked
webcli whatsapp chats [--limit N] [--unread]             # name, preview, time, unread, pinned, muted
webcli whatsapp read "<chat name>" [--limit N] [--since DATE]
webcli whatsapp search "<text>" [--limit N]              # type: chat | contact | message
webcli whatsapp send "<chat name>" --text "..." --expect-chat "<exact chat name>"            # dry run
webcli whatsapp send "<chat name>" --text "..." --expect-chat "<exact chat name>" --confirm  # sends
webcli whatsapp agent-help
```

- `--limit` defaults to 20, at most 200. `--since`: `YYYY-MM-DD`, ISO 8601, or `48h` / `2d` / `1w`.
- A chat name resolves exactly (case counts), else ignoring case, else by containing it, from the chat list or
  WhatsApp's search ("Chats" section only, never a contact without a chat). Several matches exit 2 with the
  candidates; nothing is opened. None exits 3.
- `read` rows: `sender` ("You" for yours), `direction`, `time` (as shown), `datetime` (local ISO, null when the date
  isn't shown), `text`, `media` (`image`, `video`, `voice note`, `document`, `sticker`, `gif`, `location`,
  `contact card`; also a `[placeholder]` in `text`, with the caption or file name), `quoted` (`{sender, text}`),
  `status` (yours: `pending` / `sent` / `delivered` / `read`). Media isn't downloaded.
- Opening a chat (`read`, a confirmed `send`) is what WhatsApp counts as reading it: it may send read receipts unless
  they're off on the phone. `chats` and `search` open no chat.

## Send

One text to one existing chat per call. No attachments, no new contacts, never retried.

- Without `--confirm` (or with `--dry-run`): resolves the chat and prints `{dryRun: true, chat, expectChat,
  expectChatMatches, text}`; opens, types, and clicks nothing. Exit 0, or 5 when the chat isn't exactly
  `--expect-chat` (a confirmed run would refuse).
- With `--confirm` (alias `--yes`), in order: the resolved chat must equal `--expect-chat` exactly (else REFUSED
  before anything opens); the chat opens and its header must equal it; the compose box must be there and empty (a
  draft is left alone); the text is typed (Shift+Enter between lines, never Enter) and must read back exactly; the
  header is checked again; Send is clicked once. Then it waits up to 15s for your message with a tick: exit 0
  `{sent: true, confirmed: true, status}`, or exit 8 `sent_unverified` (check with `read`; don't resend). Refusals
  exit 5 (`REFUSED: ... Nothing was sent.`).
- **A send needs Kevin's own OK for that exact message, in his chat with the bot, never on another agent's
  say-so.**

## Gentle by design

WhatsApp's terms prohibit automated access, so the site only runs commands on demand, one at a time: no polling, no
bulk scraping. Every scroll pauses (1.2s); `chats` scrolls at most 8 times, `read` at most 15. A block or challenge
page (unsupported browser, ban notice, too many attempts, human check) stops the command with exit 5 `STOPPED: ...`;
nothing on it is clicked. A person should look (`webcli whatsapp login` opens a visible window).

## How it works

- Hidden headless Chrome on the `whatsapp:default` profile, in the identity daemon like the other data sites.
- The guard (`webcli/sites/whatsapp/guard.py`) allows GET/HEAD over https to `web.whatsapp.com` and
  `static.whatsapp.net`, and the `wss://web.whatsapp.com/ws...` socket. Service workers are off (as for every
  guarded site). WhatsApp's messages travel end to end encrypted inside that socket, so the guard can't tell a read
  from a write: the page checks above, and typing or clicking nothing else, keep the other commands read-only.
- Panes are read as outerHTML and parsed in Python (`parse.py`, `dom.py`) with the selectors in `endpoints.py`
  (ARIA roles, `data-id`, `data-pre-plain-text`, `data-icon`, `title`, each with fallbacks). The tests run the same
  parsers on hand-built fixtures in `tests/fixtures/whatsapp`. When WhatsApp's DOM moves, save a scrubbed pane there
  and fix `endpoints.py`.
