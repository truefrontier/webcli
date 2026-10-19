"""Every URL, GraphQL operation name, and selector webcli facebook touches on facebook.com, in one place (verbatim
from fbc's endpoints.py, minus its sign-in tables: `identity login` runs unguarded, as every webcli sign-in does).
Fix this file first when Facebook moves something.

webcli facebook reads Facebook the way a person scrolling would: full page loads (GET) and the DOM they render.
Facebook's "Comet" web app sends almost everything else as POST /api/graphql/ with a form body:
fb_api_req_friendly_name=<operation> (echoed in the x-fb-friendly-name header), doc_id=<persisted query id>,
variables=<json>, plus the fb_dtsg / lsd CSRF tokens. The guard (guard.py) reads only the operation name and doc_id,
in memory, and never stores or prints the body.

Status (fbc, 2026-10-15): the Friends-tab pagination query and the About tab's directory_* layout were CONFIRMED
live; everything else comes from open-source Facebook scrapers. Every guess fails closed: a wrong operation name is
blocked and listed under "blocked" in the command's output; a wrong selector or label finds nothing.
"""

from __future__ import annotations

ORIGIN = "https://www.facebook.com"
APP_HOST = "www.facebook.com"
ME_URL = ORIGIN + "/me"
LOGIN_URL = ORIGIN + "/login/"  # fbc's; signed in, it goes on to the home feed
# Facebook's session cookies, set only once a sign-in (and any checkpoint or two-factor step) is done. Login waits for
# both, by name: their values never leave Chrome.
SESSION_COOKIES = frozenset({"c_user", "xs"})
FRIENDS_URL = ORIGIN + "/me/friends"  # redirects to the signed-in account's own Friends tab
# Each is a full page load, with a pause in between. "about" is the default About tab (an overview: "Works at",
# "Lives in", "From"); the others add the full work history, education, and the websites / social links.
# CONFIRMED live 2026-10-15: the About tab is split into directory_* tabs (Personal details / Work / Education).
ABOUT_SECTIONS = ("about", "directory_work", "directory_education", "about_contact_and_basic_info")

# The only hosts a command may load from: Facebook itself and its static/image CDN. Everything else is blocked.
HOSTS = ("facebook.com", "*.facebook.com", "*.fbcdn.net")
# Hosts Facebook runs (for "is this a Facebook URL": enrich never fetches one outside the guarded browser).
FB_SUFFIXES = ("facebook.com", "facebook.net", "fbcdn.net", "fbsbx.com", "messenger.com", "instagram.com")
GRAPHQL_PATHS = frozenset({"/api/graphql/", "/api/graphql", "/graphql", "/graphql/", "/api/graphqlbatch/",
                           "/api/graphqlbatch"})

# GETs with side effects. The guard aborts them even though they are GETs.
SIDE_EFFECT_GETS = (
    ("www.facebook.com", "/logout.php"),
    ("www.facebook.com", "/logout"),
    ("m.facebook.com", "/logout.php"),
)

# Read-only GraphQL queries, per command. The guard allows a POST /api/graphql/ only when its operation is in the
# running command's set AND its name ends in "Query" (never "Mutation" or "Subscription"). Everything the page sends
# on load (notifications, chat, presence, "seen" markers, logging) stays blocked; that leaves parts of the UI empty
# and changes nothing.
SYNC_QUERIES = frozenset({
    # CONFIRMED live (fbc, 2026-10-15): your own profile's Friends tab loads the first cards with the page and the
    # rest through this pagination query as you scroll. If sync stalls, its "blocked" output names the real one
    # ("POST www.facebook.com/api/graphql/ op=...").
    "ProfileCometAppCollectionSelfFriendsListRendererPaginationQuery",
})
# About sections render from the full page load (Relay data preloaded in the HTML), so no query is allowed. If
# sections come back "missing" and "blocked" lists op=ProfileCometAboutAppSectionQuery, adding it here is the fix.
ABOUT_QUERIES: frozenset[str] = frozenset()

# Known writes (fbc: CONFIRMED in togashigreat/fbchat-muqit facebook/client.py). The guard blocks them because they
# are not in a read set, not because they are listed here; tests use them. Messenger send and mark-read go over the
# chat socket (MQTT), which the core guard closes.
MUTATIONS = (
    "FriendingCometFriendRequestSendMutation",
    "FriendingCometFriendRequestCancelMutation",
    "FriendingCometFriendRequestConfirmMutation",
    "FriendingCometFriendRequestDeleteMutation",
    "FriendingCometUnfriendMutation",
    "CometUFIFeedbackReactMutation",
    "ComposerStoryCreateMutation",
)

# --- DOM ----------------------------------------------------------------------------------------------------------
# CSS selectors, used only with querySelector inside page.evaluate (webcli facebook never clicks or types).
SELECTORS = {
    "main": '[role="main"]',
    # Signed-out pages: the login form (classic and Comet), or a password box anywhere.
    "login_form": 'form#login_form, form[data-testid="royal_login_form"], input[name="pass"], '
                  'input[type="password"]',
}
# URL paths that mean "not signed in, or held at a checkpoint".
AUTH_PATHS = ("/login", "/login.php", "/checkpoint", "/two_step_verification", "/two_factor", "/recover",
              "/cookie/consent")

# --- Pacing -------------------------------------------------------------------------------------------------------
SCROLL_PAUSE = (1.5, 3.5)  # seconds between scrolls on the Friends tab (random in range)
PAGE_PAUSE = (2.0, 4.5)  # seconds between full page loads (About sections, next person)
STALL_SCROLLS = 3  # stop scrolling after this many scrolls that load no new friend
MAX_SCROLLS = 2000
