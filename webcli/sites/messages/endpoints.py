"""Everything webcli messages knows about messages.google.com, and the guard's tables. Fix this file first when Google
Messages moves something.

Google Messages for web is a view of the phone: it pairs with the phone by QR code (no Google sign-in) and reaches it
through Google's relay, instantmessaging-pa. Every phone action is one POST to the relay's Messaging/SendMessage. Its
body is PBLite (protobuf as a JSON array, field N at index N-1, bytes as base64): OutgoingRPCMessage, whose
data.messageData (field 2, then 12) holds an OutgoingRPCData whose action (field 2) is in the clear and whose request
(field 5) is end-to-end encrypted between the browser and the phone. So the guard can tell a list from a send, and
can never see a conversation id or a message's text.

Status tags:
  GMSG   - what gmsg (google-messages-cli) used against the live app: its URLs and every DOM selector.
  LIBGM  - the web protocol as mautrix-gmessages' libgm implements it (pkg/libgm/util/paths.go, gmproto/rpc.proto,
           session_handler.go): relay hosts, RPC paths, the ActionType numbers, the PBLite body.
  LIVE   - seen in a capture of the paired app (python -m webcli.core.capture messages).
  GUESS  - UNVERIFIED. Every guess fails closed: a wrong one is blocked and named in the error.
"""

from __future__ import annotations

ORIGIN = "https://messages.google.com"
APP_HOST = "messages.google.com"
APP_URL = ORIGIN + "/web/conversations"  # GMSG: the list; not paired, the app sends the tab to AUTH_PATH
LOGIN_URL = APP_URL  # GMSG: the pairing window opens here too (paired, it stays; not paired, it shows the QR code)
AUTH_PATH = "/web/authentication"  # LIBGM (GoogleAuthenticationURL): the QR pairing page
# GMSG: a conversation is /web/conversations/<id> ("new" starts one); parse.thread_id reads it.

# --- Request guard (guard.py). Fail-closed: anything not listed is aborted. ---------------------------------------
# Hosts every command may GET: the app (its shell, scripts, /web/config) and Google's static and font hosts (GUESS
# past messages.google.com: the app's own requests are what a capture shows).
HOSTS = (APP_HOST, "*.gstatic.com", "fonts.googleapis.com")
# LIBGM: the relay. clients6 serves Google-account pairing and the token refresh.
RELAY_HOSTS = frozenset({"instantmessaging-pa.googleapis.com", "instantmessaging-pa.clients6.google.com"})
RPC_PREFIX = "/$rpc/google.internal.communications.instantmessaging.v1."  # then Service/Method
SEND_RPC = "Messaging/SendMessage"  # LIBGM: every phone action, read or write
# LIBGM: relay calls that read, or keep the paired session alive: the long poll the phone's answers arrive on, its
# delivery acks, the auth token refresh, and the web key fetch (GUESS: whether the app calls it once paired).
RELAY_READS = frozenset({"Messaging/ReceiveMessages", "Messaging/AckMessages", "Registration/RegisterRefresh",
                         "Pairing/GetWebEncryptionKey"})
# LIVE (2026-10-19): two more calls the paired app can't work without. Captures show them on clients6 only, so they
# are allowed only there. Registration/SignInGaia is the app's sign-in: blocked, the app never shows the list (it
# gives up on /web/unableToConnect). LIBGM: it returns the session's auth token and the account's device list, and
# pairs nothing (pairing is the CREATE_GAIA_PAIRING_* actions, which READ_ACTIONS leaves out). Messaging/Echo is the
# app's connection check: while it fails (retried after 2s, 4s, 8s...), the app sends the phone nothing new, so an
# opened conversation never asks for its messages.
SESSION_HOST = "instantmessaging-pa.clients6.google.com"
SESSION_RPCS = frozenset({"Registration/SignInGaia", "Messaging/Echo"})
# LIBGM: everything else the relay serves stays blocked: Pairing/RegisterPhoneRelay and RefreshPhoneRelay (a new QR
# code: only the unguarded login window pairs), Pairing/RevokeRelayPairing (unpair), and POSTs to /upload (attachment
# uploads); a GET there, the download below, only for `read --image-out`.
# LIVE (2026-10-21), LIBGM (DownloadMedia: a GET to UploadMediaURL, the attachment id and key in a header): the app
# fetches an image's encrypted bytes with a GET to MEDIA_HOST + MEDIA_PATH (no query) and decrypts them in the page,
# so a blocked GET leaves every image bubble a spinner. The guard passes that GET only on a page opened with the
# core guard's download permission.
MEDIA_HOST = "instantmessaging-pa.googleapis.com"
MEDIA_PATH = "/upload"
# LIVE, not needed for the list or a conversation, so blocked too: the regional relay instantmessaging-pa-jms-us
# (Registration/ListIdentities and SignInSecondary, Messaging/Echo and PullMessages), and past the relay the avatars
# (lh3.googleusercontent.com), the account bar (ogads-pa), web attestation (waa-pa), cookie rotation
# (accounts.google.com), www.google.com, link previews, GIFs (api.tenor.com), and the app's jserror and telemetry.
RPC_NAMES = frozenset({*RELAY_READS, *SESSION_RPCS, SEND_RPC, "Pairing/RegisterPhoneRelay",
                       "Pairing/RefreshPhoneRelay", "Pairing/RevokeRelayPairing"})  # named in describe() lines

DATA_EVENT = 19  # LIBGM BugleRoute.DataEvent: the route every phone action takes
# LIBGM rpc.proto ActionType, whole, so a blocked action is named.
ACTIONS = {
    0: "UNSPECIFIED", 1: "LIST_CONVERSATIONS", 2: "LIST_MESSAGES", 3: "SEND_MESSAGE", 4: "MESSAGE_UPDATES",
    6: "LIST_CONTACTS", 7: "CONVERSATION_UPDATES", 9: "GET_OR_CREATE_CONVERSATION", 10: "MESSAGE_READ",
    11: "BROWSER_PRESENCE_CHECK", 12: "TYPING_UPDATES", 13: "SETTINGS_UPDATE", 14: "USER_ALERT",
    15: "UPDATE_CONVERSATION", 16: "GET_UPDATES", 17: "ACK_BROWSER_PRESENCE", 18: "LIST_STICKER_SETS",
    19: "LEAVE_RCS_GROUP", 20: "ADD_PARTICIPANT_TO_RCS_GROUP", 21: "GET_CONVERSATION_TYPE", 22: "NOTIFY_DITTO_ACTIVITY",
    23: "DELETE_MESSAGE", 24: "INSTALL_STICKER_SET", 25: "RESEND_MESSAGE", 26: "GET_CONTACT_RCS_GROUP_STATUS",
    27: "DOWNLOAD_MESSAGE", 28: "LIST_TOP_CONTACTS", 29: "GET_CONTACTS_THUMBNAIL", 30: "CHANGE_PARTICIPANT_COLOR",
    31: "IS_BUGLE_DEFAULT", 32: "STICKER_USER_CONTEXT", 33: "FAVORITE_STICKER_PACKS", 34: "RECENT_STICKERS",
    35: "UPDATE_RECENT_STICKERS", 36: "GET_FULL_SIZE_IMAGE", 37: "GET_PARTICIPANTS_THUMBNAIL", 38: "SEND_REACTION",
    39: "SEND_REPLY", 40: "GET_BLOB_FOR_ATTACHMENT", 41: "GET_DEVICES_AVAILABLE_FOR_GAIA_PAIRING",
    42: "CREATE_GAIA_PAIRING", 43: "GET_CONVERSATION", 44: "CREATE_GAIA_PAIRING_CLIENT_INIT",
    45: "CREATE_GAIA_PAIRING_CLIENT_FINISHED", 46: "UNPAIR_GAIA_PAIRING", 47: "CANCEL_GAIA_PAIRING", 48: "PREWARM",
    49: "CONVERSATION_GROUP_NAME_SEARCH", 50: "LINK_RCS_IDENTITY", 51: "UNLINK_RCS_IDENTITY", 52: "PRE_FETCH_CONTACTS",
    53: "FETCH_BLOCKED_CONTACTS", 54: "REMOVE_PARTICIPANT_FROM_RCS_GROUP", 55: "UPDATE_GROUP_ICON",
    56: "FETCH_DESTINATIONS_TO_CONVERSATIONS",
}
SEND_MESSAGE = 3
# The actions any command may send: listing and thumbnails, and the session's own upkeep (GET_UPDATES makes this tab
# the active web session, as "Use here" does; the presence acks and NOTIFY_DITTO_ACTIVITY tell the phone a browser
# is there). GUESS: which of these the app calls. Everything else is a write and is blocked, MESSAGE_READ and
# TYPING_UPDATES included; a name that only sounds like a read (FAVORITE_STICKER_PACKS, DOWNLOAD_MESSAGE, which
# makes the phone fetch an MMS) stays out.
READ_ACTIONS = frozenset({1, 2, 6, 16, 17, 21, 22, 26, 28, 29, 31, 36, 37, 43, 48, 49, 52})

# --- The page (GMSG unless tagged) ----------------------------------------------------------------------------------
# A list of selectors is tried in order (gmsg's `a || b`); a string is one querySelectorAll.
SELECTORS = {
    "list": "mws-conversations-list, [data-e2e-conversation-list], nav.conversation-list, .conversations-list",
    "item": "mws-conversation-list-item",
    "name": ["[data-e2e-conversation-name]", "h2.name, .name, [data-e2e-contact-name], h3, .contact-name"],
    "preview": ["mws-conversation-snippet", ".snippet, [data-e2e-snippet], .preview-text, .message-preview"],
    "time": ["mws-relative-timestamp", ".snippet-timestamp, .time, [data-e2e-timestamp], .timestamp"],
    "link": ["a[data-e2e-conversation], a.list-item", "a"],
    "unread": ".unread, [data-e2e-unread], .unread-indicator",
    "spinner": "mws-conversations-list mws-spinner, nav.conversation-list mws-spinner, "
               "[aria-label=\"Loading conversation list\"]",
    "qr": "canvas[aria-label*=\"QR\"]",
    "pairText": ["Pair your phone"],
    "useHere": ["Use Messages here", "Use here", "Yes,? use here", "Switch here"],  # another session holds Messages
    "message": "mws-message-wrapper, [data-e2e-message-wrapper]",
    "thread": "mws-message-wrapper, mws-messages-list",
    "compose": "mws-message-compose, [data-e2e-message-input-box]",
    "core": "[data-e2e-message-wrapper-core], .msg-row",
    "outgoing": ".outgoing, .rcs-outgoing, .text-msg-container.outgoing",
    "text": ["[data-e2e-text-message-content] .text-msg-content", ".text-msg-content", "mws-message-part-content",
             "[data-e2e-text-message-content]", "mws-text-message-part",
             ".text, [data-e2e-message-text], .message-text, .message-content"],
    "image": "mws-image-message-part, [data-e2e-image-message]",
    # LIVE (2026-10-21): inside an image part, the thumbnail (its <img> a blob: of the decrypted preview, 450px
    # wide); clicking it opens the lightbox, whose <img> is a blob: of the full-size image (3024px). Escape closes it.
    "imageOpen": "mws-image, [data-e2e-message-image]",
    "lightbox": "mws-lightbox",  # open while it is in the page, its image loaded or not
    "lightboxImage": "mws-image img",
    "aria": "mws-text-message-part, [aria-label]",
    "stamp": ["mws-absolute-timestamp", "mws-relative-timestamp", ".time, [data-e2e-timestamp], .timestamp"],
    # The open conversation's title (GUESS): send checks it names the resolved conversation when one is found.
    "header": ["[data-e2e-conversation-title]", "mws-conversation-header h2", "mws-conversation-header"],
}
# The compose box, visible one first: the contenteditable mirror; its paired <textarea> is often hidden.
INPUTS = ("div[contenteditable=\"true\"][data-e2e-message-input-box]", "mws-message-compose div[contenteditable=\"true\"]",
          "div[contenteditable=\"true\"][aria-label*=\"message\" i]", "textarea[data-e2e-message-input-box]:not([hidden])",
          "mws-message-compose textarea:not([hidden])", "textarea[aria-label*=\"message\" i]:not([hidden])",
          "textarea[placeholder*=\"message\" i]:not([hidden])")
SEND_BUTTON = "button[data-e2e-send-text-button]:not([disabled]), button[aria-label*=\"Send\"]:not([disabled])"

# --- Pacing (GMSG) ------------------------------------------------------------------------------------------------
POLL_MS = 500
LOAD_SECONDS = 45  # the list shell, then its rows (or a settled empty list)
EMPTY_POLLS = 2  # an empty list counts once it has stayed empty, with no spinner, this many polls
THREAD_SECONDS = 15
MESSAGES_SECONDS = 10
SETTLE_MS = 500  # rows and bubbles hydrate after they appear
BUTTON_SECONDS = 8  # the send button enables after the text is in
SEND_SECONDS = 10  # the guard's one send slot stays open this long after the click
CONFIRM_SECONDS = 15  # the thread shows the sent text within this
IMAGE_SECONDS = 20  # the lightbox shows the full-size image within this (a phone photo is ~7 MB over the relay)
CLOSE_SECONDS = 5  # the lightbox is gone within this after Escape
