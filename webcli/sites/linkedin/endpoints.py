"""Everything webcli linkedin knows about www.linkedin.com, and the guard's tables (from limsg). Fix this file first
when LinkedIn moves something.

Status (limsg): messaging list/history, activity, boost, and the post dry run read live sessions; the send
(createMessage) and the post writes (register, upload, normShares) match LinkedIn's own web client as OSS Voyager
clients report it, but limsg never ran the post writes live. Messaging queryIds rotate: `messages list` and
`messages history` read the ones the messaging page itself uses, and keep the latest in the site's query_ids.json.
"""

from __future__ import annotations

ORIGIN = "https://www.linkedin.com"
APP_HOST = "www.linkedin.com"
MESSAGING_URL = ORIGIN + "/messaging/"
THREAD_URL = ORIGIN + "/messaging/thread/{thread}/"
LOGIN_URL = MESSAGING_URL  # signed out, it sends the window to LinkedIn's sign-in
# Same origin, no app JS: the boost reads and every write run here, so nothing the app's own scripts send can ride
# along.
LANDING_URL = ORIGIN + "/robots.txt"
APP_URL = MESSAGING_URL  # what `python -m webcli.core.capture linkedin` loads

VOYAGER = ORIGIN + "/voyager/api"
ME_URL = VOYAGER + "/me"  # the signed-in member: the login check, the author, and the mailbox
MESSAGING_GQL = VOYAGER + "/voyagerMessagingGraphQL/graphql"
REST_CONVERSATIONS = VOYAGER + "/messaging/conversations"
DEFAULT_QUERY_IDS = {  # limsg's known defaults; the page's own (sniffed) win
    "messengerConversations": "messengerConversations.0d5e6781bbee71c3e51c8843c6519f48",
    "messengerMessages": "messengerMessages.5846eeb71c981f11e0134cb6626cc314",
}
SEND_PATH = "/voyager/api/voyagerMessagingDashMessengerMessages"
SEND_URL = ORIGIN + SEND_PATH + "?action=createMessage"
REGISTER_PATH = "/voyager/api/voyagerVideoDashMediaUploadMetadata"
REGISTER_URL = ORIGIN + REGISTER_PATH + "?action=upload"
CREATE_PATH = "/voyager/api/contentcreation/normShares"
CREATE_URL = ORIGIN + CREATE_PATH
FEED_UPDATE = ORIGIN + "/feed/update/{urn}/"
VOYAGER_HEADERS = {"accept": "application/vnd.linkedin.normalized+json+2.1", "x-restli-protocol-version": "2.0.0",
                   "x-li-lang": "en_US", "x-li-page-instance": "urn:li:page:messaging_index;"}

CM_BASE = ORIGIN + "/campaign-manager-api/"  # Boost: GET only
CM_HEADERS = {"x-restli-protocol-version": "2.0.0", "x-li-lang": "en_US", "accept": "application/json"}

# --- Posts (READ-ONLY: feed, posts search|from|read) ----------------------------------------------------------------
# Voyager GETs from robots.txt, as the feed and profile pages' own clients make them. The queryIds are LinkedIn's
# registered GraphQL queries (checked live 2026-10-20); when LinkedIn rotates one, these reads fail with a pointer
# here. The feed query takes only sortOrder RELEVANCE or MEMBER_SETTING: LinkedIn offers no "Recent" order for it.
VOYAGER_GQL = VOYAGER + "/graphql"
MAIN_FEED_QUERY_ID = "voyagerFeedDashMainFeed.923020905727c01516495a0ac90bb475"
FEED_SORT_ORDER = "MEMBER_SETTING"  # the member's own feed setting (LinkedIn ranks it)
PROFILE_UPDATES_QUERY_ID = "voyagerFeedDashProfileUpdates.4af00b28d60ed0f1488018948daad822"  # the member share feed
PROFILE_LOOKUP = VOYAGER + "/identity/dash/profiles?q=memberIdentity&memberIdentity={handle}"
PROFILE_UPDATES_REST = (VOYAGER + "/identity/profileUpdatesV2?count={count}&start={start}&q=memberShareFeed"
                        "&moduleKey=member-shares%3Aphone&includeLongTermHistory=true&profileUrn={urn}")
# One post, with its top `comments` top-level comments (LinkedIn's "Most relevant" order) in the same GET.
UPDATE_URL = VOYAGER + "/feed/updatesV2?commentsCount={comments}&likesCount=0&q=backendUrnOrNss&urnOrNss={urn}"
DEFAULT_COMMENTS = 10  # `posts read --comments N`
MAX_COMMENTS = 50
# Content search and company pages render as server-driven cards (no Voyager query serves them): read as rendered.
COMPANY_POSTS_URL = ORIGIN + "/company/{name}/posts/?feedView=all"
SEARCH_FEED = "FLAGSHIP_SEARCH"  # the search results' card ids: expanded<key>FeedType_FLAGSHIP_SEARCH
# The search page's own "next results" request: a POST, but a read. Only `posts search` lets it through, and only
# with exactly this pager and screen (guard.search_page_allowed).
PAGINATION_PATH = "/flagship-web/rsc-action/actions/pagination"
PAGINATION_QUERY_KEYS = frozenset({"parentSpanId", "sduiid"})
SEARCH_PAGER = "com.linkedin.sdui.search.contentSearchResults"
SEARCH_SCREEN = "com.linkedin.sdui.flagshipnav.search.SearchResultsContent"
MAX_POSTS = 50  # the hard cap: no post command returns more than this many posts
# The feed is ranked, not chronological, and a third of each page is ads, promos, and carousels: to find up to
# MAX_POSTS posts inside a --since window it may page through this many feed updates (FEED_READ_BUDGET / POSTS_PAGE
# GETs, PAGE_PAUSE_MS apart). Read-only Voyager GETs; nothing new for the guard.
FEED_READ_BUDGET = 200
POSTS_PAGE = 25  # posts per Voyager request
PAGE_PAUSE_MS = 1500  # between requests and scrolls: a person's pace, not a crawl

ME_ACTIVITY = ORIGIN + "/in/me/recent-activity/comments/"  # only finds the vanity; the lists render on /in/<vanity>/
ACTIVITY_URL = ORIGIN + "/in/{vanity}/recent-activity/{path}/"
KIND_PATH = {"comments": "comments", "reactions": "reactions", "posts": "all"}
KIND_FEED = {"comments": "PROFILE_COMMENTS", "reactions": "PROFILE_REACTIONS", "posts": "CREATOR_PROFILE_ALL_CONTENT_VIEW"}

# --- Request guard (guard.py). Fail-closed: anything not listed is aborted. ---------------------------------------
# Hosts every command may GET: LinkedIn and its static/media CDN.
HOSTS = ("linkedin.com", "*.linkedin.com", "*.licdn.com")
# GETs with side effects: sign-out. Aborted even on a listed host.
SIDE_EFFECT_PATHS = ("/m/logout", "/uas/logout", "/logout")
# Rest.li actions (?action=...) change things, or bill (Campaign Manager's fetchCurrentRunningSpend): never a read.
ACTION_PARAM = "action"

# A page URL holding one of these means signed out or held at a checkpoint.
AUTH_WALL = ("login", "checkpoint", "authwall", "uas/login")

# --- Writes -------------------------------------------------------------------------------------------------------
MAX_TEXT = 3000  # a post, counted like LinkedIn's composer (UTF-16 units)
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # LinkedIn takes larger images; 10 MB keeps the single in-page upload small
# No personal default ships: with nothing configured every write refuses (see author.expected).
DEFAULT_EXPECT_AUTHOR = None  # the handle (linkedin.com/in/<handle>) writes must come from
EXPECT_AUTHOR_ENV = "WEBCLI_LINKEDIN_EXPECT_AUTHOR"

# --- Pacing -------------------------------------------------------------------------------------------------------
SNIFF_MS = 2500  # how long a messaging page gets to fire its own queries (their queryIds are read from the URLs)
CSRF_MS = 10_000  # how long the messaging page gets to set JSESSIONID in a fresh Chrome (fetch.land)
