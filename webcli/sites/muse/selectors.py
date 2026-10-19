"""muse.ai UI selectors, in one place (from musecli's endpoints.py; status tags as there). CSS selector lists; the
first visible match wins. fetch.py hands the whole dict to its in-page scripts as `sel`. Fix this file first when
the page changes; `--headed` shows the window."""

SELECTORS = {
    # BUNDLE + ANON: the signed-out login form ("Log in or create an account": FB/IG account cards,
    # "Mobile number or email", passkey). The same form is in the public HTML of muse.ai/business.
    "login": '[data-testid="native-auth-access-pane"], [data-testid="native-auth-account-list"], '
             'input[autocomplete="username webauthn"]',
    # VERIFIED 2026-10-15: fill + Enter sends.
    "composer": 'textarea[aria-label="Message"], textarea',
    # VERIFIED 2026-10-15: messages are [data-message-item][data-message-role=...] with data-message-id
    # (assistant ids look like assistant-msg-<uuid>).
    "user_message": '[data-message-role="user"]',
    "assistant_message": '[data-message-role="assistant"]',
    # VERIFIED 2026-10-15: an assistant message's text; the message element around it also holds
    # screen-reader text.
    "assistant_body": '[data-hatch-assistant-message-body]',
    # VERIFIED 2026-10-15: screen-reader and search copies inside a message ("You:\n\n...",
    # "Assistant message: ..."). Left out when reading message text.
    "surrogate": '[data-message-accessibility-surrogate], [data-message-search-surrogate]',
    # VERIFIED 2026-10-15: set on the assistant body while the answer streams ("false" once done).
    # This is the busy signal: no stop-button markup is known.
    "streaming": '[data-hatch-markdown-streaming="true"]',
    # VERIFIED 2026-10-15: rows in nav[aria-label="Side chats"], role="button", no href and no id.
    # The first is "Main chat" (aria-current="page" on muse.ai/); clicking a side chat goes to
    # /thread/<uuid>. The title is in span[title], the relative time ("2m", "Tue") in a sibling span.
    "thread_row": '[data-testid="hatch-thread-row"]',
    # GUESS: an extra busy signal if it ever matches. The bundle's analytics names
    # (chat_stop_response, chat_inline_approval_decision) say these controls exist; their markup is unknown.
    "stop": 'button[aria-label*="stop" i]',
    "approval": '[data-testid*="approval"]',
}
