# WhatsApp Web fixtures

Hand-built HTML in the shape of WhatsApp Web's rendered panes (the `outerHTML` the live commands read), modelled on
WhatsApp Web's public DOM: `role`/`aria-label`/`data-icon`/`data-id`/`data-pre-plain-text` attributes and obfuscated
class names. Not captured from Kevin's account: no real names, numbers, or messages. When a live run shows the DOM has
moved, save the pane's outerHTML here (scrub every name, number, and message first) and fix
`webcli/sites/whatsapp/endpoints.py`.

- `chat_list.html`: `#pane-side` with an Archived button row, a pinned group with an unread count, chats that make
  "Sam" ambiguous, a muted chat, a "Marked as unread" chat, and a phone-number chat wrapped in bidi marks.
- `thread.html`: `#main` for the group "Family Group": date separators, an encryption notice and an unread banner
  (skipped), text with an emoji, a quoted reply, an image with a caption, an image without one, a voice note, a
  document, a deleted message, a multi-line message, and your messages with read / delivered / pending ticks.
- `search.html`: `#pane-side` showing search results for "sam": Chats, Contacts, and Messages sections.
- `qr.html`: the not-linked landing page with its QR canvas.
