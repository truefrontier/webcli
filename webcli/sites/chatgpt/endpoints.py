"""Every host and path llmcli chatgpt touches, in one table. Fix this file first when a probe disagrees.

chatgpt.com is a React app that talks to its own JSON API under /backend-api/ with a bearer token it gets from
/api/auth/session. llmcli chatgpt calls the read endpoints with fetch() from inside a chatgpt.com page (fetch.py's JS gets
the token there and never hands it to Python), and lets the page itself send prompts through the composer:

  session        GET  /api/auth/session                    -> {user, expires, account{planType}, accessToken}
  models         GET  /backend-api/models                   -> {models: [{slug, title, description, tags}], ...}
  conversations  GET  /backend-api/conversations?offset=&limit=&order=updated -> {items: [{id, title, ...}]}
  conversation   GET  /backend-api/conversation/<id>        -> {title, mapping{node: {message, parent, children}},
                                                                current_node, default_model_slug, ...}
  send           POST /backend-api/f/conversation (SSE)     the page's own request; llmcli chatgpt only watches it
  file download  GET  /backend-api/files/download/<file_id>?conversation_id=<id>&inline=false   (sediment://)
                 GET  /backend-api/files/<file-id>/download                                    (file-service://)
                 -> {status: "success", download_url: <signed link>, file_name, ...}

Fail closed: the guard (browser.request_allowed) aborts any request this file doesn't allow.
"""

from __future__ import annotations

import re

ORIGIN = "https://chatgpt.com"
APP_HOST = "chatgpt.com"
APP_URL = ORIGIN + "/"
CHAT_URL = ORIGIN + "/c/{id}"
LOGIN_URL = ORIGIN + "/auth/login"
SESSION_PATH = "/api/auth/session"
API = "/backend-api/"

# Hosts any command may GET from: the app, its static CDN, its image/file CDN, Cloudflare's challenge, and its
# feature-flag host. Everything else (analytics, Sentry, Datadog, third parties) is aborted.
READ_HOSTS = re.compile(
    r"chatgpt\.com|ab\.chatgpt\.com|(?:[\w-]+\.)*oaistatic\.com|(?:[\w-]+\.)*oaiusercontent\.com"
    r"|(?:[\w-]+\.)*challenges\.cloudflare\.com|cdn\.platform\.openai\.com|cdn\.openai\.com",
    re.I,
)

# POSTs any command may send: they read, or keep the page alive, and change nothing in the account.
# (host, path prefix). Filled in from probes; see webcli.core.capture.
READ_POSTS: tuple[tuple[str, str], ...] = (
    ("ab.chatgpt.com", "/v1/initialize"),  # feature flags for this session (Statsig); read-only
    ("chatgpt.com", "/cdn-cgi/challenge-platform/"),  # Cloudflare's bot check
    ("challenges.cloudflare.com", "/"),
)

# Only `llmcli chatgpt ask` / `-p` (send=True): (methods, host, path prefix). Checked first, so no other rule lets anything
# else reach them. A prefix ending in "/" matches what is under it; any other matches itself only.
SEND_REQUESTS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("POST",), APP_HOST, "/backend-api/sentinel/chat-requirements"),  # proof-of-work / turnstile before a send
    (("POST",), APP_HOST, "/backend-api/sentinel/chat-requirements/"),
    (("POST",), APP_HOST, "/backend-api/f/conversation"),  # the prompt (SSE answer)
    (("POST",), APP_HOST, "/backend-api/f/conversation/prepare"),
    (("POST",), APP_HOST, "/backend-api/conversation"),  # the older path for the same request
    (("POST",), APP_HOST, "/backend-api/conversation/init"),  # the composer's pre-flight (limits, model) on load
    (("POST",), APP_HOST, "/backend-api/conversation/prepare"),
    (("POST",), APP_HOST, "/backend-api/files"),  # --file: create the upload
    (("POST",), APP_HOST, "/backend-api/files/process_upload_stream"),  # --file: tell ChatGPT the bytes are up
    (("PUT", "OPTIONS"), "*.oaiusercontent.com", "/"),  # --file: the bytes (signed upload link)
    (("PUT", "OPTIONS"), "*.blob.core.windows.net", "/"),
)
# --file also posts /backend-api/files/<file-id>/uploaded once the bytes are up.
UPLOADED = re.compile(r"^/backend-api/files/[\w-]+/uploaded$")
# The prompt request: its JSON body names the model, which the guard checks when -m was given.
CONVERSATION_PATHS = frozenset({"/backend-api/f/conversation", "/backend-api/conversation"})

# GETs with side effects. The guard aborts them even though they are GETs.
SIDE_EFFECT_GETS = (
    (APP_HOST, "/auth/logout"),
    (APP_HOST, "/api/auth/signout"),
)

# Known writes, for tests and for reading probe output. The guard blocks them because nothing above allows
# them, not because they are listed here.
MUTATING = {
    ("PATCH", "/backend-api/conversation/<id>"): "rename, archive, or delete (is_visible=false) a chat",
    ("DELETE", "/backend-api/conversation/<id>"): "delete a chat",
    ("PATCH", "/backend-api/conversations"): "archive or delete every chat",
    ("POST", "/backend-api/conversation/gen_title/<id>"): "generate (rename to) a chat title",
    ("POST", "/backend-api/share/create"): "share a chat (public link)",
    ("PATCH", "/backend-api/share/<id>"): "change a share link",
    ("POST", "/backend-api/conversation/message_feedback"): "thumbs up/down",
    ("POST", "/backend-api/memories"): "change memory",
    ("DELETE", "/backend-api/memories"): "delete memory",
    ("POST", "/backend-api/user_system_messages"): "change custom instructions",
    ("PATCH", "/backend-api/settings/account_user_setting"): "change a setting",
    ("POST", "/backend-api/settings/account_user_setting"): "change a setting",
    ("POST", "/backend-api/accounts/<id>/invites"): "invite someone to a workspace",
    ("POST", "/backend-api/gizmos/snorlax/upsert"): "create or change a project",
    ("POST", "/ces/v1/t"): "analytics",
    ("POST", "/backend-api/lat/r"): "latency telemetry",
}
