# Google Messages: `webcli messages`

`webcli messages` reads your phone's conversations through Google Messages for web (messages.google.com), in a hidden Chrome paired with your phone. It is gmsg (google-messages-cli) rebuilt on webcli's shared core. Every verb is read-only except `send`, which is a dry run unless you pass `--confirm`.

**A send needs Kevin's own OK for that exact message.** An agent never passes `--confirm` for a text he hasn't approved word for word; the dry run is how you show him who and what first.

`status`, `list`, and `read` have run against the live site (webcli 0.4.1, October 19, 2026). `send` has not. The selectors come from gmsg, which did run live. The network rules come from Google Messages' web protocol as the open-source mautrix-gmessages bridge (libgm) implements it, corrected by live captures (tagged LIVE in `webcli/sites/messages/endpoints.py`). Anything marked GUESS fails closed: if it's wrong, the guard blocks the request and names it in the error.

## What ships

| Verb | What it does |
|---|---|
| `list` (alias `threads`) | The conversation list the app has loaded, newest first: `id`, `name`, `preview` (the latest message), `timestamp`, `unread`. |
| `read QUERY` | One conversation's messages, oldest first: `direction` (sent or received), `text`, `timestamp`. It stays unread. |
| `search QUERY [--pages N]` | Conversations (past the newest too) and contacts matching a name, a number, or the latest message. Each row has `kind` (`conversation` or `contact`) and lists what `matched`. |
| `send --to QUERY --text T` | A dry run: resolves the conversation and sends nothing. With `--confirm` (or `--yes`), sends one text. |
| `send --to NUMBER\|CONTACT [--to ...] --text T --new-ok` | A dry run for a new 1:1 (one `--to`) or group (several) conversation: resolves each recipient and sends nothing. With `--confirm` and `--expect-recipient` for each recipient, starts it from Start chat and sends one text. |
| `status` | Whether `messages:default` is paired. Exit 4 if not. |

QUERY is a conversation id, a name (exact, then the one containing it), or a phone number. Numbers are normalized to E.164: punctuation is ignored, 10 digits are US (+1), and `+`/`011` take a country code. Several matches exit 2 and list the candidates; none exits 3. Without `--new-ok` nothing ever starts a new conversation.

### Finding older conversations

The app lists the newest 25 conversations. When QUERY isn't among them (`read`, `send --to`) or always (`search`), the lookup goes deeper, read-only:

1. **Messages' own Start chat picker.** Typing the name or number there lists the phone's matching contacts (`LIST_CONTACTS`) and group conversations with a matching member (`CONVERSATION_GROUP_NAME_SEARCH`). Both are reads the guard already passed. A row without an avatar is another number of the contact above it.
2. **Paced "Load more".** Up to `--pages` (default 8, max 40) clicks on the list's "Load more conversations", 750 ms apart. Each adds 25 conversations, 6 to 9 seconds per page live. It stops once every contact the query names has its conversation listed, or the number's conversation is listed.

Tiers, first match wins: an id; an exact name; for a number, the conversation shown by it or the 1:1 of the contact who has it; the 1:1 of a contact whose name contains the query; a conversation whose name contains it (groups included). Several in that tier exit 2, listing them.

`send --to` is stricter. It doesn't take a substring match from the newest 25 alone, and a name that fits two different contacts exits 2 even when only one of them has a conversation. Use the full name, the number, or the id.

`threads` is an alias of `list` because Google Messages has only one conversation index. Output follows the rest of webcli: `--format table|md|csv|json` (JSON when piped), `--compact`, `--select`, and the unified exit codes.

## Pairing

Google Messages for web pairs with the phone by QR code, not with a Google account, so it has its own identity, `messages:default`, and its own Chrome profile.

```sh
webcli identity login messages:default
```

A visible Chrome window opens on the QR code page. On the phone, open Google Messages, then **Device pairing > QR code scanner**, and scan the code. Leave **Remember this computer** on. Login finishes when the window shows the conversation list and a fresh hidden Chrome on the same profile is paired too. If the hidden check fails, Remember this computer was probably off: pair again.

Google Messages allows one active web session. webcli keeps it to one:

- **One session lock.** Every command that loads Messages holds `~/.webcli/run/messages.session.lock`, an exclusive `flock` shared by all identities, daemon or not. It's taken before the page opens the app and released after the page has left it. A second command waits up to `--lock-wait` (default 30 s, or `WEBCLI_LOCK_WAIT`), then exits 5 with error `messages_busy`, naming the holder's pid. This sits on top of the daemon's one-at-a-time turn and the profile lock.
- **"Use here" stops the command.** If the app says Messages is open in another window or tab (a **Use here** button, or a banner or dialog saying "open in another window/tab"), the command exits 5 with error `messages_open_elsewhere`. **It never clicks Use here**: that would cut off whoever has it. Close Messages there, then retry. (Before 0.12.0, webcli clicked it, as gmsg did.)

## How it reads

Messages arrive from the phone end-to-end encrypted, so there is no readable API response to intercept. Every verb reads the page as rendered, with gmsg's selectors. Selectors and timings are in `endpoints.py`.

## The guard

Every phone action is one POST to the relay, `instantmessaging-pa.googleapis.com/$rpc/...Messaging/SendMessage` (the live app uses the relay's other host, `instantmessaging-pa.clients6.google.com`). Its body is PBLite (protobuf as a JSON array). The action type (`LIST_MESSAGES`, `MESSAGE_READ`, `SEND_MESSAGE`...) is in the clear; the request itself, including the conversation and the text, is encrypted between the browser and the phone.

So the guard sorts phone actions by type:

- **Passes:** GETs on messages.google.com and Google's static hosts; the relay's long poll, acks, and token refresh; the app's sign-in (`Registration/SignInGaia`) and connection check (`Messaging/Echo`), on the clients6 host only; and `SendMessage` calls whose single action is a read: listing, thumbnails, and the session's own upkeep (`GET_UPDATES`, which makes this tab the active session, plus presence acks).
- **Needed, and why:** without the sign-in, the app never shows the list; it gives up on `/web/unableToConnect`. The sign-in returns the session's auth token and pairs nothing. Without the connection check, the list shows, but an opened conversation never asks the phone for its messages.
- **Blocks:** every other action, including `MESSAGE_READ` (so reading never marks anything read), `TYPING_UPDATES`, `SEND_MESSAGE`, `RESEND_MESSAGE`, reactions, replies, deletes, archive, new conversations (`GET_OR_CREATE_CONVERSATION`, except the one slot below), and settings. Also blocked: pairing and unpairing calls, attachment uploads, telemetry, every other host, and any body that doesn't read as exactly one action. A field given twice counts as unreadable. The app also calls the regional relay (`instantmessaging-pa-jms-us`), avatars, the account bar, web attestation, and cookie rotation. Live, it works without them, so they stay blocked.

## How a confirmed send is bound

The guard can count sends and read their type, but it can't see which conversation or what text. So the command checks those in the page before it lets anything out:

1. It opens the resolved conversation from the list and waits until the URL holds that conversation's id. If the page shows a title, the title must name the conversation.
2. It types the text into the compose box and waits for the send button.
3. Then, last, it checks three things again: the URL still holds that conversation's id, the compose box reads back exactly the text, and the page hasn't tried a send of its own during this command (a queued message or a retry, which the guard blocked). If the page moved to another conversation, it refuses without touching that conversation's compose box.
4. Only then does it arm the guard's one slot and click send. The slot passes one `SEND_MESSAGE` and closes, or closes after 10 seconds. Everything else stays blocked: the typing indicator, the mark-as-read, and a reply or retry.
5. It waits up to 15 seconds for the conversation to show one more sent bubble with the text. The output's `confirmed` says whether it did. A send is never retried.

## How a new conversation is bound (`--new-ok`)

`send --to R1 [--to R2 ...] --text T --new-ok` can start a conversation; nothing else can.

- **Resolving.** If one `--to` names an existing conversation, the send goes there as above. Otherwise each `--to` must be one person. A number stands for itself, named when exactly one contact has it. A name must be exactly one contact, by exact name or else the only one containing it, with exactly one number. Several contacts, several numbers, or the same number twice exit 2, listing the candidates; no contact exits 3. Several `--to` without `--new-ok` exit 2. One `--to` with no conversation and no `--new-ok` exits 3, with a hint when it's a contact.
- **Dry run** (no `--confirm`): `{dryRun: true, new: true, group, recipients: [{query, name, number}], expectRecipients, expectRecipientsMatch, text}`. It types only into the contact picker. It opens no compose box and types no message.
- **`--expect-recipient X`** (alias `--expect-recipients`; repeat it) lists the recipients you expect, each as a name or a number. They must match the resolved recipients one to one. It is required for `--new-ok --confirm` (exit 2 `missing_expect`, before anything opens). A mismatch exits 5: `REFUSED (dry run): ...` without `--confirm`, `REFUSED: ...` with it. It also works for an existing conversation, matched against its name; give the number when `--to` is a number.
- **Confirmed:**
  1. Open Start chat.
  2. For a 1:1, type the number and find the contact row with exactly that number (or "Send to" showing it). For a group, switch to group mode and pick each recipient that way. The chips must then be exactly the recipients.
  3. Only then arm the guard's one `GET_OR_CREATE_CONVERSATION` slot (10 seconds) and click the row, or **Next** for a group.
  4. The conversation that opens must be titled with exactly the recipients. A group's title is its members' names joined by ", "; live, those are full contact names.
  5. Then steps 2-5 above.

  Anything unexpected fails closed (exit 5, nothing sent), for example a step asking for a group name. The error says so when the conversation may already exist. `created` in the output says whether the guard passed the create. Never retried.

### Exit codes for `send`

| Exit | When | stdout | stderr |
|---|---|---|---|
| 0 | Dry run (no `--confirm`, or `--dry-run`): nothing was sent. | `{dryRun: true, to, threadId, text}`, or for `--new-ok` `{dryRun: true, new: true, group, recipients, expectRecipients, expectRecipientsMatch, text}` | nothing |
| 0 | Sent and verified: the conversation showed the text. | `{sent: true, confirmed: true, ...}` | nothing |
| 8 | Sent but not verified: the text went out, but the conversation didn't show it within 15 seconds. | `{sent: true, confirmed: false, ...}` | `{"error": "sent_unverified", "code": 8, "message", "hint"}` |
| 5 | Refused by a page check (`REFUSED: ... Nothing was sent.`), a recipient mismatch (`REFUSED (dry run): ...` without `--confirm`), or the click sent nothing (`... Nothing was sent.`). Also busy (`messages_busy`) or open elsewhere (`messages_open_elsewhere`). | nothing | `{"error": "<what happened>", "code": 5}` |
| 3 | No such conversation (without `--new-ok`), or no such contact. | nothing | the error |
| 2 | Usage error: empty text, an ambiguous `--to` (the candidates listed), several `--to` without `--new-ok`, or `--new-ok --confirm` without `--expect-recipient`. Nothing is sent. | nothing | the usage error |

Exit 8 means the text **was sent**. Don't send it again. Check with `webcli messages read <threadId>`. The stderr message reads: "The text was sent, but it couldn't be verified: conversation <threadId> didn't show it within 15s." Its hint names the `read` command to run.

`titleChecked` doesn't change the exit code. It records whether the page showed a title to match in step 1, before the click. Without a title, the conversation id in the URL still ties the send to the resolved conversation.

## Changes from gmsg, and why

- **Exit codes follow webcli:** not paired is 4 (gmsg used 3), not found is 3 (gmsg used 4), and a dry run exits 0 (gmsg exited 5).
- **`send` resolves the conversation in its dry run**, so you see who it would go to before you confirm. gmsg's dry run never opened the browser.
- **`send` starts a conversation only with `--new-ok`.** gmsg could start a new chat by number. Here that's a separate write (`GET_OR_CREATE_CONVERSATION`) that the guard allows once, only for `send --new-ok --confirm`, and only after the picker shows exactly the expected recipients.
- **"Use here" is never clicked** (gmsg clicked it): a command stops instead.
- **`read` doesn't mark the conversation read.** The guard blocks the app's `MESSAGE_READ`.
- **New:** `search`, the `threads` alias, conversation ids in `list`, and `--yes` alongside `--confirm`.
- **Gone:** gmsg's `login`/`pair`, `version`, and `--profile`. Use `webcli identity login messages:default`, `--version`, and `--identity`. There is no `gmsg` alias.

## What the live runs showed, and what is still unverified

`python -m webcli.core.capture messages` records what the page requests and what the guard blocked, redacted. Live, read-only:

- The list needs only `IS_BUGLE_DEFAULT`, `GET_UPDATES`, and `LIST_CONVERSATIONS`. Opening a conversation adds `GET_CONVERSATION_TYPE`, `LIST_MESSAGES`, and `NOTIFY_DITTO_ACTIVITY`. All are in `READ_ACTIONS`. The guard blocks `FETCH_BLOCKED_CONTACTS` and `STICKER_USER_CONTEXT`, and nothing breaks.
- The app never called `Pairing/GetWebEncryptionKey`.
- Opening a conversation from the list by its link works headless.

- 0.12.0, live and read-only: the conversation title (`header`, now LIVE) is `mws-header[data-e2e-conversation-header] [data-e2e-header-title] h2`. Start chat's contact picker, its group results, group-mode chips, and "Load more" match `endpoints.py` (LIVE). Typing into the picker and loading more pages make only reads. Searching by name and number found older conversations and contacts. A `--new-ok` dry run resolved a number to its contact.

Still unverified, because they need a send: whether gmsg's send button selector still matches, and what Messages shows after **Next** for a new group. The code fails closed on anything unexpected there.

The offline tests in `tests/test_messages.py` and `tests/test_messages_new.py` run every send path, dry run and confirmed, existing and new, against a scripted page through the real policy. `tests/test_messages_dom.py` runs the page scripts on sanitized DOM fixtures in headless Chromium.
