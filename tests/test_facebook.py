"""webcli facebook, offline: STRICTLY read-only on Facebook. The source contains no clicks (fbc's static check, kept),
the guard passes only page loads and the command's read queries, and nothing else: then the parsers on fbc's authored
fixtures, the fetch flows on a fake page, the cache under ~/.webcli/sites/facebook/, enrichment without tvly, and the
commands over a faked daemon. Nothing reaches Facebook; no Chrome starts.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from click.testing import CliRunner

import webcli
from webcli.apps import webcli as webcli_app
from webcli.core import browser, config, daemon, guard
from webcli.core.errors import AuthRequired, NotFound
from webcli.sites.facebook import endpoints, enrich, fetch, parse, store
from webcli.sites.facebook.guard import POLICY, read_policy, request_allowed

from conftest import FIXTURES, make_profile

FB = "https://www.facebook.com"
GRAPHQL = FB + "/api/graphql/"
PAGINATION = "ProfileCometAppCollectionSelfFriendsListRendererPaginationQuery"
FRIENDS = f"{FB}/kevin.example/friends"
OVERVIEW = ('<div role="main"><h1>Jane Doe</h1><div><span>Works at <a href="/AcmeCoffeeRoasters">Acme Coffee '
            'Roasters</a></span></div><div><span>Lives in Springfield, Missouri</span></div>'
            '<div><span>From Bolivar, Missouri</span></div></div>')
LOGIN_PAGE = '<div id="root"><form id="login_form"><input name="email"><input name="pass" type="password"></form></div>'
PACKAGE = Path(webcli.__file__).parent


def fixture(name: str) -> str:
    return (FIXTURES / "facebook" / name).read_text()


def form(op: str | None = PAGINATION, *, doc_id: str | None = "7123456789012345", **extra) -> str:
    """A GraphQL body the way Facebook's web client sends it (tokens made up)."""
    fields = [("av", "100000000000001"), ("__user", "100000000000001"), ("__a", "1"),
              ("fb_dtsg", "NAcMfakeTOKEN:12:1760450400"), ("jazoest", "25512"), ("lsd", "AVqfakeTOKEN")]
    if op is not None:
        fields.append(("fb_api_req_friendly_name", op))
    fields.append(("variables", '{"count":8,"cursor":"AQHRfake"}'))
    if doc_id is not None:
        fields.append(("doc_id", doc_id))
    return urlencode(fields + list(extra.items()))


def allowed(method, url, headers=None, body=None, *, reads=endpoints.SYNC_QUERIES):
    return request_allowed(method, url, headers, body, reads=reads)


# No clicks, ever (fbc's static check). Every Playwright (and DOM) way to press, type into, or submit something.
# webcli facebook only navigates, reads, waits, and scrolls, so none may appear in its source or in the core modules
# its commands run through.
INTERACTIONS = re.compile(
    r"\.(click|dblclick|tap|check|uncheck|set_checked|select_option|fill|type|press|press_sequentially|"
    r"set_input_files|dispatch_event|drag_to|drag_and_drop|submit|requestSubmit|focus|hover)\s*\(|"
    r"\bmouse\s*\.\s*(click|dblclick|down|up)\b|\bkeyboard\s*\.|\bget_by_role\s*\(|\bget_by_text\s*\(|"
    r"\bget_by_label\s*\(|\bexpect_file_chooser\b|\bdispatchEvent\b|new\s+(Mouse|Keyboard|Pointer)Event"
)
# A selector that names an action control (by aria-label or visible text) has no business here.
CONTROL_SELECTOR = re.compile(
    r"(?i:aria-label|has-text|text=|:text\(|innerText|textContent)[^\n]{0,40}"  # labels are capitalized
    r"\b(Add [Ff]riend|Unfriend|Message|Poke|Like|React|Comment|Share|Follow|Block|Settings|Post|Confirm|Delete)\b"
)
ON_ITS_PATH = ["core/browser.py", "core/guard.py", "core/daemon.py", "sites/data.py", "sites/base.py"]


def sources():
    files = sorted((PACKAGE / "sites" / "facebook").glob("*.py")) + [PACKAGE / p for p in ON_ITS_PATH]
    assert len(files) >= 12, files
    return [(path, path.read_text()) for path in files]


def test_the_site_never_clicks_types_or_submits():
    offenders = [f"{path.name}: {m.group(0)}" for path, text in sources() for m in INTERACTIONS.finditer(text)]
    assert offenders == []


def test_no_selector_targets_an_action_control():
    offenders = [f"{path.name}: {m.group(0)}" for path, text in sources() for m in CONTROL_SELECTOR.finditer(text)]
    assert offenders == []
    for value in endpoints.SELECTORS.values():  # selectors are only ever read with querySelector
        assert not re.search(r"button|aria-label|role=\"button\"", value, re.I), value


def test_the_static_checks_catch_what_they_claim():
    assert INTERACTIONS.search('await page.locator("x").click()')
    assert INTERACTIONS.search("await page.keyboard.press('Enter')")
    assert INTERACTIONS.search("el => el.click()")
    assert INTERACTIONS.search("form.submit()")
    assert INTERACTIONS.search("await page.mouse.click(1, 2)")
    assert not INTERACTIONS.search("await page.mouse.wheel(0, 500)")
    assert CONTROL_SELECTOR.search("page.locator('[aria-label=\"Add friend\"]')")
    assert not CONTROL_SELECTOR.search("a.attrs.get('aria-label')")


# The guard.

@pytest.mark.parametrize(("method", "url", "ok"), [
    ("GET", "https://www.facebook.com/me/friends", True),
    ("HEAD", "https://www.facebook.com/", True),
    ("GET", "https://www.facebook.com/jane.doe/directory_work", True),
    ("GET", "https://static.xx.fbcdn.net/rsrc.php/v4/x.js", True),
    ("GET", "https://scontent-dfw5-1.xx.fbcdn.net/v/t39/x.jpg", True),
    ("GET", "https://www.facebook.com/ajax/bootloader-endpoint/?modules=x", True),
    ("GET", "https://www.facebook.com/logout.php?h=AT0", False),
    ("GET", "https://www.facebook.com/logout/", False),
    ("GET", GRAPHQL + "?doc_id=1&variables=%7B%7D", False),  # GraphQL is POST-only
    ("GET", "https://www.facebook.com/webgraphql/mutation/?doc_id=1", False),
    ("GET", "https://www.instagram.com/x", False),  # facebook.com read endpoints only
    ("GET", "https://connect.facebook.net/en_US/sdk.js", False),
    ("GET", "https://example.com/anything", False),
    ("GET", "http://www.facebook.com/me", False),
    ("GET", "https://facebook.com.evil.example/me", False),
    ("POST", "https://www.facebook.com/ajax/bz", False),
    ("POST", "https://www.facebook.com/ajax/add_friend/action.php", False),
    ("POST", "https://www.facebook.com/ufi/reaction/", False),
    ("POST", "https://www.facebook.com/messages/send/", False),
    ("POST", "https://www.facebook.com/login/?privacy_mutation_token=x", False),  # no sign-in mode at all
    ("POST", "https://edge-chat.facebook.com/pull", False),
    ("PUT", GRAPHQL, False),
    ("DELETE", "https://www.facebook.com/jane.doe", False),
    ("OPTIONS", GRAPHQL, False),
])
def test_request_allowed_without_bodies(method, url, ok):
    assert allowed(method, url) is ok


def test_the_pagination_read_passes_only_for_sync():
    body = form()
    assert allowed("POST", GRAPHQL, {"x-fb-friendly-name": PAGINATION}, body) is True
    assert allowed("POST", GRAPHQL.rstrip("/"), {}, body) is True
    assert allowed("POST", GRAPHQL, {}, body, reads=endpoints.ABOUT_QUERIES) is False
    assert allowed("POST", "http://www.facebook.com/api/graphql/", {}, body) is False
    assert allowed("POST", "https://www.facebook.com/api/graphqlbatch/", {}, body) is False


UNSOURCED_WRITES = ("useCometUFICreateCommentMutation", "CometUserFollowMutation", "CometProfileBlockMutation",
                    "CometPokeMutation", "ProfileCometSeenStateMutation", "CometSettingsUpdateMutation")


@pytest.mark.parametrize("op", endpoints.MUTATIONS + UNSOURCED_WRITES)
@pytest.mark.parametrize("reads", [endpoints.SYNC_QUERIES, endpoints.ABOUT_QUERIES, frozenset()])
def test_every_known_mutation_is_blocked_for_every_command(op, reads):
    assert allowed("POST", GRAPHQL, {"x-fb-friendly-name": op}, form(op), reads=reads) is False


def test_mutations_are_blocked_even_if_someone_lists_them():
    for op in ("FriendingCometFriendRequestSendMutation", "SomeQuerySubscription", "useCometUFICreateCommentMutationQuery"):
        assert allowed("POST", GRAPHQL, {}, form(op), reads=frozenset({op})) is False
    for op in ("ProfileCometAboutAppSectionQuery", "CometNotificationsRootQuery"):  # reads outside the command's set
        assert allowed("POST", GRAPHQL, {}, form(op)) is False


@pytest.mark.parametrize(("headers", "body"), [
    ({"x-fb-friendly-name": "FriendingCometUnfriendMutation"}, form()),  # header and body disagree
    ({}, form() + "&fb_api_req_friendly_name=FriendingCometUnfriendMutation"),  # two names
    ({}, form(doc_id=None)), ({}, form() + "&doc_id=7999999999999999"), ({}, form(doc_id="abc")),
    ({}, form(op=None)), ({}, form(queries='{"q1":{"doc_id":"1"}}')),
    ({}, '{"fb_api_req_friendly_name":"%s","doc_id":"1"}' % PAGINATION), ({}, ""), ({}, None),
])
def test_ambiguous_graphql_bodies_fail_closed(headers, body):
    assert allowed("POST", GRAPHQL, headers, body) is False


def test_the_core_guard_records_blocks_without_tokens_or_bodies():
    request = SimpleNamespace(method="POST", url=GRAPHQL + "?__user=100000000000001", headers={},
                              post_data=form("FriendingCometUnfriendMutation"))
    guard.state.begin(read_policy(endpoints.SYNC_QUERIES))
    assert guard.check(request, own_page=True) is False
    assert guard.state.blocked == ["POST www.facebook.com/api/graphql/ op=FriendingCometUnfriendMutation"]
    socket_line = guard.describe("WS", "wss://edge-chat.facebook.com/chat?sid=1234567890123456789")
    assert socket_line == "WS edge-chat.facebook.com/chat"  # and POLICY lists no sockets: the core closes them all
    assert POLICY.sockets is None


# The parsers (fbc's fixtures: authored, every id and token made up).

def test_friends_tab():
    rows = parse.friends(fixture("friends_tab.html"), FRIENDS)
    assert [(r["key"], r["name"], r["subtitle"]) for r in rows] == [
        ("jane.doe", "Jane Doe", "Owner at Acme Coffee Roasters"),
        ("100004567890123", "Bob Smith", "Lives in Springfield, Missouri"),
        ("carla.nunez.77", "Carla Núñez", None),
        ("100009876543210", "Eve Adams", "Studied at Missouri State University"),
        ("dan.lee.5", "Dan Lee", "Works at Initech · Springfield, Missouri"),
    ]
    assert "kevin.example" not in {r["key"] for r in rows}  # the owner is not a friend
    dumped = json.dumps(rows)
    for leak in ("__cft__", "__tn__", "AZX", "TOKEN", "fb_dtsg"):
        assert leak not in dumped


def test_about_sections_and_canonical_links():
    record = parse.about({"about": fixture("about_work.html"), "about_places": fixture("about_places.html"),
                          "about_contact_and_basic_info": fixture("about_contact.html")},
                         f"{FB}/jane.doe/about", "(3) Jane Doe | Facebook")
    assert (record["key"], record["name"], record["currentCity"], record["hometown"]) == (
        "jane.doe", "Jane Doe", "Springfield, Missouri", "Bolivar, Missouri")
    assert record["links"] == [{"type": "website", "url": "https://acmecoffee.example/"},
                               {"type": "linkedin", "url": "https://www.linkedin.com/in/jane-doe-acme/"},
                               {"type": "instagram", "url": "https://www.instagram.com/janedoe.coffee/"}]
    assert parse.external_url("https://l.facebook.com/l.php?u=https%3A%2F%2Fx.example%2F%3Fp%3D1%26fbclid%3Dz&h=AT0") \
        == "https://x.example/?p=1"


@pytest.mark.parametrize(("value", "key"), [("jane.doe", "jane.doe"), ("100004567890123", "100004567890123"),
                                            ("facebook.com/profile.php?id=100004567890123", "100004567890123"),
                                            ("https://www.facebook.com/jane.doe?__cft__[0]=x", "jane.doe")])
def test_profile_arguments(value, key):
    assert parse.person_from_arg(value).key == key


# The fetch flows, on a fake page.

class FakePage:
    """Just enough of a Playwright page: goto follows `redirects`, evaluate answers fetch.py's JS (read and scroll
    only: anything else is an error)."""

    def __init__(self, pages: dict, redirects: dict | None = None) -> None:
        self.pages, self.redirects = pages, redirects or {}
        self.url, self.visits, self.scrolls = "about:blank", [], 0

    async def goto(self, url, **_options):
        self.visits.append(url)
        self.url, self.scrolls = self.redirects.get(url, url), 0

    def html(self) -> str:
        page = self.pages.get(self.url, "")
        return page[min(self.scrolls, len(page) - 1)] if isinstance(page, list) else page

    async def wait_for_selector(self, selector, **_options):
        if 'role="main"' not in self.html():
            raise TimeoutError(selector)

    async def title(self):
        return "Jane Doe | Facebook"

    async def evaluate(self, script, arg=None):
        if script == fetch.MAIN_JS:
            return self.html() or None
        if script == fetch.MAIN_TEXT_LEN_JS:
            return len(self.html())
        if script == fetch.SIGNED_OUT_JS:
            return 'name="pass"' in self.html()
        if script == fetch.SCROLL_JS:
            self.scrolls += 1
            return 1000 * self.scrolls
        raise AssertionError(f"unexpected page script: {script[:60]}")


@pytest.fixture
def page(monkeypatch):
    """page(pages, redirects) -> (the fake, the policies the command opened its page with)."""
    monkeypatch.setattr(fetch, "SETTLE_POLL", 0)

    async def no_pause(_span):
        return None

    monkeypatch.setattr(fetch, "pause", no_pause)
    policies: list = []

    def install(pages, redirects=None):
        fake = FakePage(pages, redirects)

        @asynccontextmanager
        async def site_page(policy, **options):
            assert options == {}  # never send, never a visible window
            policies.append(policy)
            yield fake

        monkeypatch.setattr(browser, "site_page", site_page)
        return fake, policies

    return install


def sync_policy_only(policy) -> bool:
    request = SimpleNamespace(method="POST", url=GRAPHQL, headers={}, post_data=form())
    return policy.allowed(request, guard.Guard())


def test_sync_scrolls_merges_and_stops_when_the_list_stops_growing(page):
    fake, policies = page({FRIENDS: [fixture("friends_tab.html"), fixture("friends_tab_scrolled.html")]},
                          {endpoints.FRIENDS_URL: FRIENDS})
    result = asyncio.run(fetch.sync(None))
    assert [r["key"] for r in result["friends"]] == [
        "jane.doe", "100004567890123", "carla.nunez.77", "100009876543210", "dan.lee.5", "frank.owner",
        "100001234567891"]  # the first five survive Facebook dropping them from the DOM
    assert result["complete"] is True and result["scrolls"] == 1 + endpoints.STALL_SCROLLS
    assert fake.visits == [endpoints.FRIENDS_URL]
    assert [sync_policy_only(p) for p in policies] == [True]  # only sync may run the pagination read


def test_sync_is_not_complete_when_a_pagination_query_was_blocked(page):
    page({FRIENDS: [fixture("friends_tab.html")]}, {endpoints.FRIENDS_URL: FRIENDS})
    guard.state.blocked.append(f"POST www.facebook.com/api/graphql/ op=Other{PAGINATION}")
    assert asyncio.run(fetch.sync(None))["complete"] is False


def test_about_reads_each_section_as_a_page_under_no_queries(page):
    base = f"{FB}/jane.doe"
    fake, policies = page({f"{base}/about": OVERVIEW, f"{base}/directory_work": fixture("about_work.html"),
                           f"{base}/directory_education": fixture("about_work.html"),
                           f"{base}/about_contact_and_basic_info": fixture("about_contact.html")})
    record = asyncio.run(fetch.about({"key": "jane.doe", "username": "jane.doe", "numericId": None}))
    assert fake.visits == [f"{base}/{s}" for s in endpoints.ABOUT_SECTIONS]
    assert [sync_policy_only(p) for p in policies] == [False]
    assert (record["key"], record["currentCity"], record["blocked"]) == ("jane.doe", "Springfield, Missouri", [])


def test_about_many_keeps_going_past_a_missing_profile_and_stops_when_signed_out(page):
    page({f"{FB}/gone.person/about": fixture("profile_unavailable.html"), f"{FB}/jane.doe/about": OVERVIEW})
    people = [{"key": "gone.person", "username": "gone.person"}, {"key": "jane.doe", "username": "jane.doe"}]
    out = asyncio.run(fetch.about_many(people))
    assert "isn't available" in out[0]["error"] and out[1]["record"]["key"] == "jane.doe"
    page({f"{FB}/jane.doe/about": LOGIN_PAGE})
    with pytest.raises(AuthRequired):
        asyncio.run(fetch.about_many(people[1:]))


def test_status_reads_the_account_from_me(page):
    page({f"{FB}/kevin.example": OVERVIEW}, {endpoints.ME_URL: f"{FB}/kevin.example"})
    assert asyncio.run(fetch.status()) == {"signedIn": True, "name": "Jane Doe", "key": "kevin.example",
                                           "profileUrl": f"{FB}/kevin.example"}
    page({}, {endpoints.ME_URL: f"{FB}/login/?next=x"})
    assert asyncio.run(fetch.status()) == fetch.signed_out()


def test_unavailable_profile_is_not_found(page):
    page({f"{FB}/gone.person/about": fixture("profile_unavailable.html")})
    with pytest.raises(NotFound, match="isn't available"):
        asyncio.run(fetch.about({"key": "gone.person", "username": "gone.person"}))


# The cache and enrichment.

def test_the_cache_lives_under_the_webcli_home_and_is_private():
    store.save_friends([{"key": "jane.doe", "username": "jane.doe", "numericId": None, "name": "Jane Doe",
                         "profileUrl": f"{FB}/jane.doe", "subtitle": "Owner at Acme Coffee Roasters"}])
    db = config.home() / "sites" / "facebook" / "friends.sqlite"
    assert store.data_dir() == db.parent and db.is_file()
    assert stat.S_IMODE(os.stat(db).st_mode) == 0o600 and stat.S_IMODE(os.stat(db.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(store.exports_dir()).st_mode) == 0o700
    [person] = store.people()
    assert store.flat(person)["employer"] is None and store.matches(person, name="JANE", keyword="acme")


def test_enrich_without_tvly_leaves_linkedin_null(monkeypatch):
    monkeypatch.setenv(enrich.TVLY_ENV, "off")
    person = {"key": "jane.doe", "name": "Jane Doe", "subtitle": "Owner at Acme Coffee Roasters"}
    record = enrich.build(person, {"work": [{"role": "Owner", "employer": "Acme", "current": True}]},
                          site=lambda _url: None)
    assert record["publicLinks"]["linkedin"] is None and record["webSearch"] == "unavailable"
    assert record["company"] == {"value": "Acme", "source": "facebook_about", "confidence": "high"}
    assert enrich.tvly_path() is None


def test_enrich_never_fetches_a_facebook_host():
    assert not enrich.public_http_url("https://www.facebook.com/x")
    assert not enrich.public_http_url("http://127.0.0.1/")


# The commands, over a faked daemon.

@pytest.fixture
def calls(monkeypatch):
    make_profile("meta:default")
    seen, answers = [], {}

    async def call(ident, site, op, *args, **kwargs):
        assert ident.id == "meta:default"  # Muse's Meta profile: no login of its own
        seen.append((op, args, kwargs))
        value = answers[op]
        return value(*args, **kwargs) if callable(value) else value

    monkeypatch.setattr(daemon, "call", call)
    return seen, answers


def run(*args):
    return CliRunner().invoke(webcli_app.build(), ["facebook", *args])


def test_sync_caches_friends_and_reports_json(calls):
    seen, answers = calls
    answers["sync"] = {"friends": parse.friends(fixture("friends_tab.html"), FRIENDS), "scrolls": 4,
                       "complete": True, "blocked": []}
    out = json.loads(run("sync", "--limit", "9").stdout)
    assert out == {"synced": 5, "new": 5, "updated": 0, "cachedFriends": 5, "scrolls": 4, "complete": True,
                   "blocked": []}
    assert seen == [("sync", (9,), {"headed": False})]


def test_search_and_export_never_open_chrome(calls, fake_playwright, tmp_path):
    seen, answers = calls
    answers["sync"] = {"friends": parse.friends(fixture("friends_tab.html"), FRIENDS), "scrolls": 0,
                       "complete": False, "blocked": []}
    run("sync")
    rows = json.loads(run("search", "--city", "springfield", "--select", "key,name").stdout)
    assert rows == [{"key": "100004567890123", "name": "Bob Smith"}, {"key": "dan.lee.5", "name": "Dan Lee"}]
    assert run("search", "--name", "nobody").exit_code == 3
    out = json.loads(run("export", "--format", "csv", "--out", str(tmp_path / "friends.csv")).stdout)
    path = Path(out["path"])
    assert out["count"] == 5 and stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert "__cft__" not in path.read_text() and path.read_text().startswith("key,name,subtitle,")
    default = Path(json.loads(run("export").stdout)["path"])
    assert default.parent == config.home() / "sites" / "facebook" / "exports"
    assert [op for op, _, _ in seen] == ["sync"] and fake_playwright == []


def test_enrich_reads_about_through_the_daemon_then_caches(calls, monkeypatch):
    seen, answers = calls
    monkeypatch.setenv(enrich.TVLY_ENV, "off")
    answers["about"] = lambda who, **_: {**parse.about({"about": OVERVIEW}, f"{FB}/jane.doe/about"), "blocked": []}
    out = json.loads(run("enrich", "jane.doe", "--no-web").stdout)
    assert out["company"]["value"] == "Acme Coffee Roasters" and out["webSearch"] == "skipped"
    assert seen == [("about", ({"key": "jane.doe", "username": "jane.doe", "numericId": None},), {"headed": False})]
    cached = json.loads(run("about", "jane.doe", "--cached").stdout)
    assert cached["currentCity"] == "Springfield, Missouri" and len(seen) == 1  # --cached: no Chrome


def test_status_signed_out_points_at_the_meta_login(calls):
    _, answers = calls
    answers["status"] = fetch.signed_out()
    result = run("status")
    assert result.exit_code == 4 and "webcli identity login meta:default" in result.stderr
    assert "facebook.com" in result.stderr
