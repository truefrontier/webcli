"""Everything llmcli muse knows about muse.ai's hosts, routes, and API paths, and the request guard's allowlists
(verbatim from musecli's endpoints.py). Fix this file first when a capture disagrees. The DOM selectors are in
selectors.py.

Muse is Meta's personal AI agent (Meta Superintelligence Labs, launched 2026-09-08). Its web app is muse.ai, a
Next.js app whose internal codename is "hatch". Not the old muse.ai video host (now Skiv); that table lives on
muse-cli's `skiv` branch. Sign-in is a Meta account (Facebook, Instagram, email, or phone), so the identity is
meta:default. Chat runs over an end-to-end encrypted WebSocket to the account's cloud VM (wss://*.metaaivm.com).

Each entry carries a status tag. Recon on 2026-10-14 used anonymous curl only (no sign-in, no browser):
  BUNDLE  - the string is in muse.ai's public JS (deployment dpl_5Y93fGXrcHCUgdXHzBACNUyarC7J)
  ANON    - an anonymous request answered as noted
  3P      - documented by third-party clients that say they checked it live (nikships/muse-cli
            docs/PROTOCOL.md, fierzone/MUSE_MCP); not in muse.ai's public bundle
  GUESS   - UNVERIFIED. Nothing confirms it. Every guess fails closed: a wrong path or op name is
            blocked and named in the error, and a wrong selector finds nothing.
  VERIFIED 2026-10-15 - seen in muse.ai's DOM in a signed-in session (live recon); `ask` in a new
            chat sent the prompt and read the answer back exactly.
Muse has no model picker (not in the composer, not in any settings tab): Muse picks its model itself.
"""

from __future__ import annotations

from urllib.parse import quote

ORIGIN = "https://muse.ai"
APP_HOSTS = {"muse.ai", "www.muse.ai"}
# ANON: signed out, muse.ai/ 307s to auth.muse.ai/aymh/, which cycles through
# facebook.com/aymh/redirect-cycle/ and instagram.com/aymh/redirect-cycle/ (looking for existing
# Meta sessions), then back to muse.ai/, where the login form renders (NATIVE_LOGIN_PATH = "/").
AUTH_HOSTS = {"auth.muse.ai", "www.facebook.com", "www.instagram.com"}

ROUTES = {
    "home": "/",  # BUNDLE: chat = "/" (the main chat), and the login page when signed out
    "new_thread": "/thread/new",  # BUNDLE: threadDraft
    "thread": "/thread/{id}",  # BUNDLE: thread = /thread/:sessionId
}
NOT_THREADS = {"new"}  # BUNDLE: /thread/new is the new-chat draft
MAIN = "main"  # BUNDLE: threadMain = /thread/main, an alias of "/"; llmcli muse opens the main chat at "/"
# BUNDLE: pages a signed-in account can be held on: /access/{waitlist,invite,verification,disclosure,
# welcome}, /blocked (Meta checkpoint), /unavailable(/session).
GATE_PREFIXES = ("/access", "/blocked", "/unavailable")

# BUNDLE: POST, credentials:'include', no body; the app calls it after any 401/403 to re-check the
# session. ANON: GET answers 405, so the route exists. Its 2xx body is a GUESS (fetch.session reads
# only a name and an explicit false).
AUTH_CHECK = "/api/auth/check"

# --- Request guard (guard.py). Fail-closed: anything not listed is aborted. ---------------------------------
#
# A POST is matched on (host, path, op). op is the GraphQL operation name (form field
# fb_api_req_friendly_name or JSON operationName, which must agree with any x-fb-friendly-name
# header), else a Next.js server-action id ("next-action:<id>"), else "doc_id:<id>", else None.
# See parse.post_op.

# GETs with side effects. BUNDLE: the app signs out with GET /api/logout?sessionBindingId=...
SIDE_EFFECT_GETS = (("muse.ai", "/api/logout"),)

# BUNDLE: session upkeep, no user data. Allowed for every guarded command.
SESSION_POSTS = {("muse.ai", AUTH_CHECK, None)}

# POSTs the app needs before it can show anything: a token for the VM gateway socket, and waking
# the account's cloud VM. 3P: POST /api/hatch/token {vmAddress, vmName}, POST /api/hatch/vm/wake.
# (GET /api/session, which names the VM, is a GET.) Add what a capture shows blocked here.
READ_POSTS: set[tuple[str, str, str | None]] = {
    ("muse.ai", "/api/hatch/token", None),
    ("muse.ai", "/api/hatch/vm/wake", None),
}

# POSTs for sending one prompt; allowed only for `ask` / `-p`. 3P: none. The prompt goes through
# the VM gateway socket (gateway method chat.stream), not an HTTP POST.
SEND_POSTS: set[tuple[str, str, str | None]] = set()

# The only WebSocket hosts allowed. BUNDLE: ".metaaivm.com" (with ".test-only.metaaivm.com") is the
# app's VM gateway domain. 3P: wss://hatch.metaaivm.com/v1/noise?vm_id=...&auth_token=... is a
# Noise_XX-encrypted socket carrying protobuf requests to ~258 gateway methods: chat.history,
# chat.stream (send), sessions.list, model.get/set, session.rename/pin/archive/delete, chat.mark_seen...
# The guard can't see inside it, so it is all-or-nothing: allowed, because no chat loads without it.
# Every other socket (e.g. wss://gateway.meta.ai, wss://edge-chat.meta.ai from the CSP) is blocked.
SOCKET_HOST_SUFFIXES = (".metaaivm.com",)

# BUNDLE: telemetry and ads. Blocked like everything unlisted; named here only so error messages
# can leave them out of "the guard blocked ..." and show the blocks that matter.
EXPECTED_BLOCKS = ("muse.ai/api/falco", "muse.ai/api/analytics", "muse.ai/api/auth/caa-events",
                   "muse.ai/api/marketing/", "muse.ai/api/consent/record", "muse.ai/api/metaconfig/exposure",
                   ".ingest.us.sentry.io/")
# BUNDLE: writes: /api/hatch/artifact-share, /api/support/help-ticket, /api/auth/native/*.
# Inside the gateway socket (3P), unblockable here: chat.mark_seen when a chat is opened.


def thread_url(chat_id: str) -> str:
    if chat_id == MAIN:
        return ORIGIN + ROUTES["home"]
    return ORIGIN + ROUTES["thread"].format(id=quote(chat_id, safe=""))
