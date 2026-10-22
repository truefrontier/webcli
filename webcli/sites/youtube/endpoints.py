"""Every URL webcli youtube touches. Fix this file first when a capture disagrees.

YouTube history: youtubei POST /youtubei/v1/browse {browseId: "FEhistory"} and its continuations, called with
fetch() inside a www.youtube.com page; the SAPISIDHASH header is computed there from document.cookie, so no cookie
value reaches Python. account/account_menu and account/accounts_list name the account. Day precision (section
headers like "Today", "Oct 3").

My Activity (myactivity.google.com/product/youtube): exact watch times, read from the page's embedded first page
(AF_initDataCallback), the batchexecute pages it fetches while scrolling, and its rendered DOM. Google may hide it
behind a "verify it's you" check that needs a person; then `history` falls back to YouTube (--source auto).

The one write: `playlist create --confirm` calls youtubei POST /youtubei/v1/playlist/create {title, privacyStatus,
videoIds} the same way, once, and reads the new playlist back with browse {browseId: "VL<playlistId>"}.

These are ytcli's verified rules (2026-10-15), plus Google's cookie rotation (shared with gemini on google:default).
Run `python -m webcli.core.capture youtube` after `webcli identity login google:default` to see what the guard
blocked.
"""

from __future__ import annotations

import re

APP_HOST = "www.youtube.com"
APP_URL = "https://www.youtube.com/"
# YouTube's own "Sign in" link. passive=true: a profile that already has a Google session (google:default, signed
# in through gemini) comes back signed in to YouTube without a page to fill in; one without stays signed out.
SIGN_IN_URL = ("https://accounts.google.com/ServiceLogin?service=youtube&uilel=3&passive=true&continue="
               "https%3A%2F%2Fwww.youtube.com%2Fsignin%3Faction_handle_signin%3Dtrue%26app%3Ddesktop%26next%3D"
               "https%253A%252F%252Fwww.youtube.com%252F")
MYACTIVITY_HOST = "myactivity.google.com"
MYACTIVITY_URL = "https://myactivity.google.com/product/youtube"
HOSTS = (APP_HOST, "youtube.com", "accounts.youtube.com", "*.ytimg.com", "*.ggpht.com", MYACTIVITY_HOST)
# The only POSTs on youtube.com: reads. Telemetry, player, likes, subscriptions, playlist edits... are aborted.
READ_POSTS = frozenset({
    (APP_HOST, "/youtubei/v1/browse"),
    (APP_HOST, "/youtubei/v1/account/account_menu"),
    (APP_HOST, "/youtubei/v1/account/accounts_list"),
})
# The one write (guard.WriteScope): only `playlist create --confirm`, only while its own call is in flight, and only a
# body of exactly these keys. LIVE 2026-10-21: answers {playlistId} (13 characters now, not 34).
PLAYLIST_CREATE = (APP_HOST, "/youtubei/v1/playlist/create")
CREATE_KEYS = frozenset({"context", "title", "privacyStatus", "videoIds"})
PRIVACY = {"private": "PRIVATE", "unlisted": "UNLISTED", "public": "PUBLIC"}
PLAYLIST_URL = "https://www.youtube.com/playlist?list={id}"
VERIFY_TRIES = 3  # the new playlist's read-back can lag its create by a moment
# My Activity pages its own list with batchexecute while it is scrolled (any rpcid: see guard.py's Limits).
MYACTIVITY_BATCH = re.compile(r"/_/[^/]+/data/batchexecute")
DEFAULT_LIMIT = 50
MAX_SCROLLS = 400
