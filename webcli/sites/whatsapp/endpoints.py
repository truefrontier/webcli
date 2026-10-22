"""web.whatsapp.com: the app's URL, its request policy's tables, the page's selectors, and the pacing.

WhatsApp Web talks to WhatsApp over ONE WebSocket (wss://web.whatsapp.com/ws/chat) whose frames are Noise-encrypted
end to end: reads, read receipts, and sends all travel inside it, so no request can be told apart from another and
there is no JSON API to read. Every verb reads the rendered page (the chat list pane, the conversation pane, the
search box); the guard only decides which hosts the page may reach.

Tags: LIVE (seen on the live site), GUESS (from WhatsApp Web's public DOM as other open-source tools read it; not
yet checked live by webcli). A wrong GUESS fails closed: a blocked host is named in the error, a selector that finds
nothing reads as "not found" or a timeout, never as a write.
"""

from __future__ import annotations

APP_HOST = "web.whatsapp.com"
ORIGIN = f"https://{APP_HOST}"
APP_URL = f"{ORIGIN}/"
LOGIN_URL = APP_URL  # linked: the chat list; not linked: the QR code

# --- Request guard (guard.py). Fail-closed: anything not listed is aborted. ---------------------------------------
# GUESS: the app shell, its scripts and fonts come from web.whatsapp.com; static.whatsapp.net serves the rest of its
# static resources (rsrc.php). GETs only.
HOSTS = (APP_HOST, "static.whatsapp.net")
# GUESS: the one socket the app needs. Its frames are encrypted, so the guard can't filter inside it: every write
# (a send, a read receipt, a reaction) would ride in it too. What keeps commands read-only is that they type and click
# nothing but what each one documents.
SOCKET_HOST = APP_HOST
SOCKET_PATH_PREFIX = "/ws"
# Blocked on purpose (GUESS hosts): media downloads (mmg.whatsapp.net, media-*.cdn.whatsapp.net: `read` shows
# placeholders like [image] instead), profile pictures (pps.whatsapp.net), crash logs and telemetry
# (crashlogs.whatsapp.net, dit.whatsapp.net), every POST/PUT/DELETE, and every other host.

# --- The page (GUESS unless tagged) -------------------------------------------------------------------------------
# One selector string per role; a comma list is tried as one querySelectorAll (document order). The Python parser
# (parse.py) understands the same small subset of CSS: tag, #id, .class, [attr], [attr="v"], [attr^="v"],
# [attr*="v"], [attr$="v"], descendant (space), and comma lists.
SELECTORS = {
    # The QR code page (not linked).
    "qr": 'canvas[aria-label*="QR"], div[data-ref] canvas, [data-testid="qrcode"]',
    "qrText": ["Scan to log in", "Log into WhatsApp Web", "Link with phone number", "Steps to log in",
               "Use WhatsApp on your computer", "Scan this QR code"],
    # The chat list pane, and its rows (the list is virtualised: only the rows near the scroll position exist).
    "side": "#side",
    "pane": "#pane-side",
    "chatList": '#pane-side, [aria-label="Chat list"], [data-testid="chat-list"]',
    "row": '[role="listitem"], [role="row"], [data-testid="cell-frame-container"]',
    "rowName": '[data-testid="cell-frame-title"] span[title], span[dir="auto"][title], span[title]',
    "rowTime": '[data-testid="cell-frame-primary-detail"]',
    "rowPreview": '[data-testid="last-msg-status"] span[title], [data-testid="last-msg-status"]',
    "unread": '[aria-label*="unread message"], [data-testid="icon-unread-count"], [aria-label="Unread"], '
              '[aria-label*="Marked as unread"]',
    "muted": '[data-icon="muted"], [data-testid="muted"], [aria-label*="Muted"]',
    "pinned": '[data-icon="pinned"], [data-icon="pinned2"], [data-testid="pinned"], [aria-label="Pinned chat"]',
    # Loading: the sync screen after linking ("Loading your chats [n%]") and the progress bar.
    "loading": 'progress, [data-testid="startup-progress"]',
    "loadingText": ["Loading your chats", "Loading chats", "Syncing"],
    # Another tab of this profile holds WhatsApp ("WhatsApp is open in another window. Click Use here...").
    "useHere": ["Use here", "Use Here"],
    # The search box at the top of the chat list (a contenteditable textbox, or an <input> in newer builds).
    "search": '#side div[contenteditable="true"][role="textbox"], #side [contenteditable="true"], '
              '#side input[type="text"], [aria-label="Search input textbox"], input[aria-label*="Search"], '
              'div[contenteditable="true"][title*="Search"]',
    # The open conversation.
    "main": "#main",
    "header": '#main header [data-testid="conversation-info-header-chat-title"], #main header span[dir="auto"][title], '
              '#main header span[dir="auto"]',
    "message": '[data-id^="true_"], [data-id^="false_"]',
    "messageOut": ".message-out",
    "messageIn": ".message-in",
    "prePlain": "[data-pre-plain-text]",
    "text": "span.selectable-text, [data-testid=\"msg-text\"]",
    "quoted": '[aria-label="Quoted message"], [data-testid="quoted-message"], .quoted-mention',
    "quotedText": ".quoted-mention",
    "author": '[data-testid="author"], [aria-label^="Message from"]',
    "msgTime": '[data-testid="msg-meta"] span, [data-testid="msg-time"]',
    # The scrollable message list in #main (scrolled up to load older messages).
    "scroller": '#main [data-testid="conversation-panel-messages"], #main [role="application"], '
                '#main .copyable-area > div[tabindex]',
    # The compose box and the send button.
    "compose": '#main footer div[contenteditable="true"][role="textbox"], #main footer div[contenteditable="true"], '
               '[data-testid="conversation-compose-box-input"]',
    "sendButton": '#main footer button[aria-label="Send"], #main footer [data-testid="compose-btn-send"], '
                  '#main footer span[data-icon="send"], #main footer span[data-icon="wds-ic-send-filled"]',
}
# Media placeholders: (selector inside one message, outside its quoted block) -> the placeholder's word. First wins.
MEDIA = (
    ('[data-icon="audio-play"], [data-icon="ptt-status"], [data-icon="ptt-play"], button[aria-label*="voice message"], '
     '[data-testid="audio-play"], [data-testid="ptt-status"]', "voice note"),
    ('[data-icon="media-gif"], [data-testid="media-gif"]', "gif"),
    ('[data-icon="media-play"], [data-icon="video-pip"], [data-testid="media-play"], [aria-label*="Play video"]',
     "video"),
    ('img[alt*="sticker"], [data-testid="sticker"], [aria-label*="Sticker"]', "sticker"),
    ('[data-icon^="document"], [data-icon="audio-file"], [data-testid="document-thumb"], [title^="Download"]',
     "document"),
    ('[data-testid="location-map"], [data-icon="location"], a[href*="maps.google.com"]', "location"),
    ('[data-testid="vcard-msg"], [data-icon="default-contact-refreshed"]', "contact card"),
    ('[data-testid="image-thumb"], [aria-label="Open picture"], img[src^="blob:"], img[src^="data:image"]', "image"),
)
# A sent message's tick, by data-icon or aria-label (WhatsApp pads its aria-labels with spaces).
STATUS_ICONS = {"msg-time": "pending", "msg-check": "sent", "msg-dblcheck": "delivered", "msg-dblcheck-ack": "read"}
STATUS_LABELS = {"pending": "pending", "sent": "sent", "delivered": "delivered", "read": "read"}
# A block or challenge page: stop and report, never click through. Lowercase substrings of the page text.
BLOCK_TEXT = (
    "works with google chrome", "browser is not supported", "update your browser", "update chrome to use whatsapp",
    "this account is not allowed to use whatsapp", "account has been banned", "account is banned",
    "temporarily banned", "your account was banned", "too many attempts", "verify you are human", "captcha",
    "unusual activity",
)
# Signed out mid-session (the phone unlinked this browser).
LOGGED_OUT_TEXT = ("you've been logged out", "you were logged out", "log in again")
# Search result section headings (the text of a heading row, lowercase) -> the row type under it.
SECTIONS = {"chats": "chat", "groups": "chat", "groups in common": "chat", "contacts": "contact",
            "other contacts": "contact", "messages": "message", "no chats, contacts or messages found": None}

# --- Pacing: on demand and gentle (no polling loops; reasonable delays) ---------------------------------------------
POLL_MS = 500
LOAD_SECONDS = 90  # the chat list after a fresh load (a just-linked browser syncs first)
OPEN_SECONDS = 15  # a chat opens (its header and messages show)
SEARCH_SETTLE_MS = 1_500  # search results render after typing
SETTLE_MS = 800  # rows and bubbles hydrate after they appear
SCROLL_MS = 1_200  # after each scroll, older messages / more rows load
MAX_HISTORY_SCROLLS = 15  # `read`: at most this many scroll-ups for older messages
MAX_LIST_SCROLLS = 8  # `chats`: at most this many scroll-downs of the chat list
STALL_SCROLLS = 2  # stop scrolling once this many scrolls in a row loaded nothing new
TYPE_DELAY_MS = 30  # between typed characters in the search box
CONFIRM_SECONDS = 15  # a sent message shows in the thread with a tick within this
MAX_LIMIT = 200  # no command returns more rows than this
DEFAULT_LIMIT = 20
MAX_TEXT = 4_000  # one message's length (WhatsApp's own cap is higher; webcli sends short texts)
