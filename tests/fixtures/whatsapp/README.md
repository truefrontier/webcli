# WhatsApp Web fixtures

Hand-built HTML in the shape of WhatsApp Web's rendered panes (the `outerHTML` the live commands read), modelled on
WhatsApp Web's public DOM: `role`/`aria-label`/`data-icon`/`data-id`/`data-pre-plain-text` attributes and obfuscated
class names, with no real names, numbers, or messages; plus the `thread_live_*` captures below, sanitized in the
browser before they were saved. When a live run shows the DOM has moved, save the pane's outerHTML here (scrub every
name, number, message, id and date first) and fix `webcli/sites/whatsapp/endpoints.py`.

- `chat_list.html`: `#pane-side` with an Archived button row, a pinned group with an unread count, chats that make
  "Sam" ambiguous, a muted chat, a "Marked as unread" chat, and a phone-number chat wrapped in bidi marks.
- `thread.html`: `#main` for the group "Family Group": date separators, an encryption notice and an unread banner
  (skipped), text with an emoji, a quoted reply, an image with a caption, an image without one, a voice note, a
  document, a deleted message, a multi-line message, and your messages with read / delivered / pending ticks.
- `search.html`: `#pane-side` showing search results for "sam": Chats, Contacts, and Messages sections.
- `qr.html`: the not-linked landing page with its QR canvas.
- `thread_live_{group,direct}_{bottom,top}.html` (0.11.2): `#main` captured from WhatsApp Web (October 2026) and
  sanitized in the page before it left the browser: every message text is "Placeholder text N", every person "Sender
  A/B/C", chat names "Group Chat Placeholder" / "Direct Chat Placeholder", message ids FAKEMSGIDnnnn, labels, titles,
  links and images replaced, styles and svg paths dropped, and every date shifted back 364 days (weekdays kept; their
  "Today" is 2025-10-23). Times, UI labels and the DOM structure are as captured. `bottom` is the chat as it opens,
  `top` after scrolling up: WhatsApp keeps every row's `data-id` but renders only the rows near the viewport, so each
  look holds a different window of messages. The live shape: `div[role=row] > div[data-id=<bare id>]
  [data-testid=conv-msg-<id>]` (no `true_`/`false_` prefix, no `message-in/out` classes), yours carry a hidden
  `span[aria-label="You:"]`, tails (`tail-in`/`tail-out`) and a group's `author` only on a run's first message, and
  date separators are plain divs between rows.
