# Google Messages: `webcli messages`

`webcli messages` reads your phone's conversations through Google Messages for web (messages.google.com), in a hidden Chrome paired with your phone. It is gmsg (google-messages-cli) rebuilt on webcli's shared core. Every verb is read-only except `send`, which is a dry run unless you pass `--confirm`.

`status`, `list`, and `read` have run against the live site (webcli 0.4.1, October 19, 2026). `send` has not. The selectors come from gmsg, which did run live. The network rules come from Google Messages' web protocol as the open-source mautrix-gmessages bridge (libgm) implements it, corrected by live captures (tagged LIVE in `webcli/sites/messages/endpoints.py`). Anything marked GUESS fails closed: if it's wrong, the guard blocks the request and names it in the error.

## What ships

| Verb | What it does |
|---|---|
| `list` (alias `threads`) | The conversation list the app has loaded, newest first: `id`, `name`, `preview` (the latest message), `timestamp`, `unread`. |
| `read QUERY` | One conversation's messages, oldest first: `direction` (sent or received), `text`, `timestamp`. It stays unread. |
| `search QUERY` | Conversations whose name, number, or latest message matches. Each row lists what `matched`. |
| `send --to QUERY --text T` | A dry run: resolves the conversation and sends nothing. With `--confirm` (or `--yes`), sends one text. |
| `status` | Whether `messages:default` is paired. Exit 4 if not. |

QUERY is a conversation id, a name (exact, then the one containing it), or a phone number (for a conversation shown by number). Several matches exit 2; none exits 3. Only conversations in the loaded list are opened, and nothing ever starts a new chat.

`threads` is an alias of `list` because Google Messages has only one conversation index. Output follows the rest of webcli: `--format table|md|csv|json` (JSON when piped), `--compact`, `--select`, and the unified exit codes.

## Pairing

Google Messages for web pairs with the phone by QR code, not with a Google account, so it has its own identity, `messages:default`, and its own Chrome profile.

```sh
webcli identity login messages:default
```

A visible Chrome window opens on the QR code page. On the phone, open Google Messages, then **Device pairing > QR code scanner**, and scan the code. Leave **Remember this computer** on. Login finishes when the window shows the conversation list and a fresh hidden Chrome on the same profile is paired too. If the hidden check fails, Remember this computer was probably off: pair again.

Google Messages allows one active web session. Every command takes it over (it clicks **Use here**, as gmsg did). So a Messages tab open somewhere else then asks you to click **Use here** again.

## How it reads

Messages arrive from the phone end-to-end encrypted, so there is no readable API response to intercept. Every verb reads the page as rendered, with gmsg's selectors. Selectors and timings are in `endpoints.py`.

## The guard

Every phone action is one POST to the relay, `instantmessaging-pa.googleapis.com/$rpc/...Messaging/SendMessage` (the live app uses the relay's other host, `instantmessaging-pa.clients6.google.com`). Its body is PBLite (protobuf as a JSON array). The action type (`LIST_MESSAGES`, `MESSAGE_READ`, `SEND_MESSAGE`...) is in the clear; the request itself, including the conversation and the text, is encrypted between the browser and the phone.

So the guard sorts phone actions by type:

- **Passes:** GETs on messages.google.com and Google's static hosts; the relay's long poll, acks, and token refresh; the app's sign-in (`Registration/SignInGaia`) and connection check (`Messaging/Echo`), on the clients6 host only; and `SendMessage` calls whose single action is a read: listing, thumbnails, and the session's own upkeep (`GET_UPDATES`, which makes this tab the active session, plus presence acks).
- **Needed, and why:** without the sign-in, the app never shows the list; it gives up on `/web/unableToConnect`. The sign-in returns the session's auth token and pairs nothing. Without the connection check, the list shows, but an opened conversation never asks the phone for its messages.
- **Blocks:** every other action, including `MESSAGE_READ` (so reading never marks anything read), `TYPING_UPDATES`, `SEND_MESSAGE`, `RESEND_MESSAGE`, reactions, replies, deletes, archive, new conversations, and settings. Also blocked: pairing and unpairing calls, attachment uploads, telemetry, every other host, and any body that doesn't read as exactly one action. A field given twice counts as unreadable. The app also calls the regional relay (`instantmessaging-pa-jms-us`), avatars, the account bar, web attestation, and cookie rotation. Live, it works without them, so they stay blocked.

## How a confirmed send is bound

The guard can count sends and read their type, but it can't see which conversation or what text. So the command checks those in the page before it lets anything out:

1. It opens the resolved conversation from the list and waits until the URL holds that conversation's id. If the page shows a title, the title must name the conversation.
2. It types the text into the compose box and waits for the send button.
3. Then, last, it checks three things again: the URL still holds that conversation's id, the compose box reads back exactly the text, and the page hasn't tried a send of its own during this command (a queued message or a retry, which the guard blocked). If the page moved to another conversation, it refuses without touching that conversation's compose box.
4. Only then does it arm the guard's one slot and click send. The slot passes one `SEND_MESSAGE` and closes, or closes after 10 seconds. Everything else stays blocked: the typing indicator, the mark-as-read, and a reply or retry.
5. It waits up to 15 seconds for the conversation to show one more sent bubble with the text. The output's `confirmed` says whether it did. A send is never retried.

### Exit codes for `send`

| Exit | When | stdout | stderr |
|---|---|---|---|
| 0 | Dry run (no `--confirm`, or `--dry-run`): nothing was sent. | `{dryRun: true, to, threadId, text}` | nothing |
| 0 | Sent and verified: the conversation showed the text. | `{sent: true, confirmed: true, ...}` | nothing |
| 8 | Sent but not verified: the text went out, but the conversation didn't show it within 15 seconds. | `{sent: true, confirmed: false, ...}` | `{"error": "sent_unverified", "code": 8, "message", "hint"}` |
| 5 | Refused by a page check (`REFUSED: ... Nothing was sent.`), or the click sent nothing (`... Nothing was sent.`). | nothing | `{"error": "<what happened>", "code": 5}` |
| 2 | Usage error, such as empty text. Nothing runs. | nothing | the usage error |

Exit 8 means the text **was sent**. Don't send it again. Check with `webcli messages read <threadId>`. The stderr message reads: "The text was sent, but it couldn't be verified: conversation <threadId> didn't show it within 15s." Its hint names the `read` command to run.

`titleChecked` doesn't change the exit code. It records whether the page showed a title to match in step 1, before the click. Without a title, the conversation id in the URL still ties the send to the resolved conversation.

## Changes from gmsg, and why

- **Exit codes follow webcli:** not paired is 4 (gmsg used 3), not found is 3 (gmsg used 4), and a dry run exits 0 (gmsg exited 5).
- **`send` resolves the conversation in its dry run**, so you see who it would go to before you confirm. gmsg's dry run never opened the browser.
- **`send` goes only to an existing conversation.** gmsg could start a new chat by number. Starting one is a separate write (`GET_OR_CREATE_CONVERSATION`) that the one-send guard doesn't allow. Start the chat on the phone first.
- **`read` doesn't mark the conversation read.** The guard blocks the app's `MESSAGE_READ`.
- **New:** `search`, the `threads` alias, conversation ids in `list`, and `--yes` alongside `--confirm`.
- **Gone:** gmsg's `login`/`pair`, `version`, and `--profile`. Use `webcli identity login messages:default`, `--version`, and `--identity`. There is no `gmsg` alias.

## What the live runs showed, and what is still unverified

`python -m webcli.core.capture messages` records what the page requests and what the guard blocked, redacted. Live, read-only:

- The list needs only `IS_BUGLE_DEFAULT`, `GET_UPDATES`, and `LIST_CONVERSATIONS`. Opening a conversation adds `GET_CONVERSATION_TYPE`, `LIST_MESSAGES`, and `NOTIFY_DITTO_ACTIVITY`. All are in `READ_ACTIONS`. The guard blocks `FETCH_BLOCKED_CONTACTS` and `STICKER_USER_CONTEXT`, and nothing breaks.
- The app never called `Pairing/GetWebEncryptionKey`.
- Opening a conversation from the list by its link works headless.

Still unverified, because they need a send: whether the title selector (`header`, GUESS) finds the conversation's name, and whether gmsg's send button selector still matches.

The offline tests in `tests/test_messages.py` run every send path, dry run and confirmed, against a scripted page through the real policy.
