"""www.perplexity.ai Discover: the only URLs webcli perplexity touches (the visible sign-in window aside). Recon
2026-10-26, signed out, in a hidden Chrome on a never-signed-in profile: the /discover document answers 200 with no challenge, and the two REST reads
below answer from the page's own origin with every script, beacon, and third-party request aborted.

  GET /discover                                   the document (an origin for the reads; its scripts never run)
  GET /rest/discover/topics?version&source        {all_topics: [{id (uuid), slug, name}], ...}
  GET /rest/discover/feed?limit&offset&topic&version&source
                                                  {status, items[], next_token, cache_window_in_seconds}
                                                  topic is "top" or a topic's uuid; a slug answers 0 items.
  GET /api/auth/session                           {} signed out, {user: {...}} signed in: every read checks it
"""

from __future__ import annotations

APP_HOST = "www.perplexity.ai"
ORIGIN = f"https://{APP_HOST}"
DISCOVER_PATH = "/discover"
DISCOVER_URL = ORIGIN + DISCOVER_PATH
LOGIN_URL = ORIGIN + "/"  # the sign-in dialog opens from the home page (only `login`, in a visible window)
FEED_PATH = "/rest/discover/feed"
TOPICS_PATH = "/rest/discover/topics"
SESSION_PATH = "/api/auth/session"
# Exact paths, GET/HEAD only. Everything else (scripts, every POST, /rest/* besides these, other hosts) is aborted.
READ_PATHS = frozenset({DISCOVER_PATH, FEED_PATH, TOPICS_PATH, SESSION_PATH})
API_VERSION = "2.18"  # what the Discover page itself sends (version=2.18&source=default)

# --topic choices: "top" is the feed's own name; the rest resolve by slug through /rest/discover/topics.
# Perplexity has one "Tech & Science" topic (slug tech), so science reads it too.
TOPICS = ("top", "tech", "science", "finance", "arts", "sports")
TOPIC_ALIASES = {"science": "tech"}

PAGE_SIZE = 20  # the page asks for 20 at a time
DEFAULT_LIMIT = 30
MAX_LIMIT = 100
PAGE_PAUSE_MS = 1500  # between feed pages: polite pacing
