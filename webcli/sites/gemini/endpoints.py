"""Every URL and rpcid llmcli gemini touches, in one table. Fix this file first when a sniff disagrees.

gemini.google.com is an Angular app that talks to these internal endpoints:

  batchexecute    POST /_/BardChatUi/data/batchexecute?rpcids=<id>[,<id>...]
                  form body f.req=[[[rpcid, "<payload json>", null, "generic"], ...]] & at=<token>
  processSession  https://geminiweb-pa.clients6.google.com/v1/processSession, a Google WebChannel
                  that now carries the prompt and the answer: POST is the forward channel (and
                  TYPE=terminate), GET ...TYPE=xmlhttp the long-poll back channel. Protobuf; llmcli gemini
                  never reads it.
  StreamGenerate  POST /_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate
                  (the old way to send a prompt; streamed the answer back)

The `at` token (WIZ_global_data.SNlM0e), build label (cfb2h), and session id (FdrFJe) are
read inside the page by fetch.py's JS. Python never asks for them; the only Python code that
sees them is the request guard, which reads each batchexecute body in memory to check its
rpcids and never stores or prints it.

Status: the rpcids, payloads, and response indices come from HanaokaYuzu/Gemini-API
(gemini_webapi, master of 2026-10-08), cross-checked against other open-source Gemini clients.
Live recon with a signed-in llmcli gemini profile on 2026-10-15: status, chats list (batchexecute MaZiqc),
and models work. Gemini no longer sends StreamGenerate: `ask` works once processSession is
allowed (send only), and its answer is read from the page. Run `python -m webcli.core.capture gemini` after
`llmcli gemini login` to see which rpcids the page sends that the guard blocked.
"""

from __future__ import annotations

import re

ORIGIN = "https://gemini.google.com"
APP_HOST = "gemini.google.com"
APP_URL = ORIGIN + "/app"
CHAT_URL = ORIGIN + "/app/{id}"
LOGIN_URL = "https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fgemini.google.com%2Fapp"
RPC_PREFIX = "/_/BardChatUi/data/"
BATCH_PATH = RPC_PREFIX + "batchexecute"
FRONTEND = RPC_PREFIX + "assistant.lamda.BardFrontendService/"
GENERATE_PATH = FRONTEND + "StreamGenerate"
UPLOAD_HOST = "push.clients6.google.com"  # VERIFIED 2026-10-18: where the web app uploads --file bytes
OLD_UPLOAD_HOST = "content-push.googleapis.com"
CHANNEL_HOST = "geminiweb-pa.clients6.google.com"
CHANNEL_PATH = "/v1/processSession"

# The rpcids llmcli gemini itself calls.
RPC = {
    # [page_size, null, [pinned, null, 1]] -> [_, _, [[c_id, title, pinned, _, _, [sec, nanos]], ...]]
    "list_chats": "MaZiqc",
    # [c_id, max_turns, null, 1, [1], [4], null, 1] -> [[turn, ...]] newest first
    "read_chat": "hNvQHb",
    # [[[...], [image_id, 0], null, [19, ""], ...], [r_id, rc_id, c_id, null, ""], 1, 0, 1] -> [gg-dl url]
    # VERIFIED 2026-10-17: what the "Download full size image" button sends.
    "full_size_image": "c8o8Fe",
}

# Fail closed: a batchexecute POST passes the guard only when EVERY rpcid in it is listed here.
# Only reads that gemini_webapi calls from real sessions. The page sends others while it loads
# (feature flags, tools, gems list...); the guard aborts those, which can leave parts of the UI
# empty but never changes anything. `python -m webcli.core.capture gemini` lists them under "blocked"; add one
# here only once you know it is a read.
READ_RPCIDS = frozenset({
    "MaZiqc",  # list chats
    "hNvQHb",  # read a chat's turns
    "otAQ7b",  # user status: account state, available models
    "ESY5D",  # read preferences
    "jSf9Qc",  # usage / plan window
    "qpEbW",  # model quota
    "aPya6c",  # quota
})

# Allowed only with open_page(download=True), i.e. `ask --image-out`: the full-size image link. Read-only
# (it returns a URL), but no other command needs it, so it stays out of READ_RPCIDS.
DOWNLOAD_RPCIDS = frozenset({RPC["full_size_image"]})

# The hosts a generated image downloads from, hop by hop (VERIFIED 2026-10-17): lh3.googleusercontent.com
# /gg-dl/ -> work.fife.usercontent.google.com/rd-gg-dl/ -> lh3.googleusercontent.com/rd-gg-dl/ (full size),
# and /gg/ -> lh3.google.com/rd-gg/ -> lh3.googleusercontent.com/rd-gg/ (preview). llmcli gemini fetches these with
# page.request, which context.route never sees, so browser.image_url_allowed checks every hop instead.
IMAGE_HOSTS = re.compile(r"lh\d+\.googleusercontent\.com|lh\d+\.google\.com|[a-z0-9-]+\.fife\.usercontent\.google\.com")

# Known writes, for tests and for reading sniff output. The guard blocks them because they are
# not in READ_RPCIDS, not because they are listed here.
MUTATING_RPCIDS = {
    "GzXR5e": "delete chat",
    "qWymEb": "second step of delete chat",
    "MUAZcd": "update chat: rename, pin, move",
    "fuVx7": "share chat (public link)",
    "SgORbf": "unshare chat",
    "kOWVAe": "mark last turn",
    "ukz1Fe": "generate chat title",
    "oMH3Zd": "create gem",
    "kHv0Vd": "update gem",
    "UXcSJb": "delete gem",
    "Nwkn9": "delete gem and its chats",
    "CNgdBe": "list gems (shared with a write form, so blocked)",
    "Jba3ib": "create task (deep research)",
    "PGX16d": "delete artifacts",
    "xVRQX": "create memory",
    "gSnMcd": "update memory",
    "Ok9j9b": "delete memory",
    "YgU2Cc": "delete all memories",
    "L5adhe": "update preferences (settings, last selected model)",
    "UVb0T": "accept terms",
    "ra9Swb": "continue a shared chat",
    "dI8W6e": "log event",
    "TFNzk": "log model selection",
}

# Non-batchexecute POSTs every command may send: cookie rotation keeps the Google session alive
# (Google rotates __Secure-1PSIDTS this way) and touches no Gemini data.
SESSION_POSTS = frozenset({("accounts.google.com", "/RotateCookies")})

# Only `llmcli gemini ask` (send=True): (methods, host, path). A path matches itself and what is under it
# ("/upload" matches "/upload/"). Every other command gets these blocked, whatever the method.
SEND_REQUESTS = (
    # VERIFIED 2026-10-15: the prompt's WebChannel (GET back channel, POST forward channel and terminate).
    (("GET", "POST"), CHANNEL_HOST, CHANNEL_PATH),
    (("POST",), APP_HOST, GENERATE_PATH),  # not sent by Gemini on 2026-10-15; kept in case it comes back
    (("POST",), APP_HOST, FRONTEND + "ProcessFile"),  # registers an uploaded --file
    (("POST", "PUT", "OPTIONS"), UPLOAD_HOST, "/upload"),  # the --file bytes (VERIFIED 2026-10-18)
    (("POST", "PUT", "OPTIONS"), OLD_UPLOAD_HOST, "/upload"),  # where they went before; kept in case
)

# GETs with side effects. The guard aborts them even though they are GETs.
SIDE_EFFECT_GETS = (
    ("accounts.google.com", "/Logout"),
    ("www.google.com", "/accounts/Logout"),
)
