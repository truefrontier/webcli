"""webcli linkedin feed / posts search|from|read, offline. Every fixture here is synthetic (Jane Doe, John Roe,
Example Co, example.com, made-up ids): no real post or person. Nothing reaches LinkedIn and no Chrome starts.
Covers the Voyager and card parsers, null-not-guessed counts, --since (ISO or relative), the guard (search pagination
is the only non-GET let through, and only its exact shape), and the CLI wiring."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from webcli.core import guard
from webcli.core.errors import AuthRequired, NotFound
from webcli.sites.linkedin import fetch, parse, posts
from webcli.sites.linkedin.endpoints import PAGINATION_PATH, VOYAGER, SEARCH_PAGER, SEARCH_SCREEN
from webcli.sites.linkedin.guard import POLICY, search_page_allowed, search_policy

from test_linkedin import LinkedIn, calls, run, tab  # noqa: F401 - fixtures reused

NOW = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
UPDATE = "com.linkedin.voyager.dash.feed.Update"
COUNTS = "com.linkedin.voyager.dash.feed.SocialActivityCounts"
PROFILE = "com.linkedin.voyager.dash.identity.profile.Profile"


def sid(when: datetime, low: int = 1) -> str:
    """A post id the way LinkedIn mints them: Unix ms << 22, plus low bits."""
    return str((int(when.timestamp() * 1000) << 22) | low)


def iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


T1, T2, T3 = NOW - timedelta(hours=2), NOW - timedelta(hours=30), NOW - timedelta(days=5)
A1, A2, A3 = (f"urn:li:activity:{sid(t)}" for t in (T1, T2, T3))
ORIG = f"urn:li:activity:{sid(NOW - timedelta(days=9), 7)}"
AD = f"urn:li:activity:{sid(NOW - timedelta(hours=1), 3)}"


def update(urn: str, name: str, handle: str | None, text: str, *, counts: tuple | None = (3, 1, 0), header: str = "",
           reshared: str | None = None, article: str | None = None, entity: str | None = None, hide: dict | None = None,
           company: str | None = None) -> list[dict]:
    """A dash Update plus what it points at in `included`."""
    detail_urn = f"urn:li:fsd_socialDetail:({urn})"
    counts_urn = f"urn:li:fsd_socialActivityCounts:{urn}"
    profile_urn = f"urn:li:fsd_profile:ACoAA{name.replace(' ', '')}"
    actor = {"name": {"text": name, "attributesV2": [{"detailData": {"*profileFullName": profile_urn}}] if handle else []},
             "description": {"text": f"{name.split()[0]}'s headline at Example Co"},
             "navigationContext": {"actionTarget": f"https://www.linkedin.com/company/{company}/" if company
                                   else f"https://www.linkedin.com/in/{handle}/"}}
    up = {"$type": UPDATE, "entityUrn": entity or f"urn:li:fsd_update:({urn},MAIN_FEED,EMPTY,DEFAULT,false)",
          "metadata": {"backendUrn": urn}, "actor": actor, "commentary": {"text": {"text": text}},
          "*socialDetail": detail_urn, "header": {"text": {"text": header}} if header else None}
    if hide:
        up["socialContent"] = hide
    if reshared:
        up["*resharedUpdate"] = f"urn:li:fsd_update:({reshared},MAIN_FEED,EMPTY,DEFAULT,false)"
    if article:
        up["content"] = {"articleComponent": {"navigationContext": {"actionTarget": article}}}
    out = [up, {"$type": "com.linkedin.voyager.dash.social.SocialDetail", "entityUrn": detail_urn,
                **({"*totalSocialActivityCounts": counts_urn} if counts else {})}]
    if counts:
        out.append({"$type": COUNTS, "entityUrn": counts_urn, "urn": urn, "numLikes": counts[0],
                    "numComments": counts[1], "numShares": counts[2]})
    if handle:
        out.append({"$type": PROFILE, "entityUrn": profile_urn, "publicIdentifier": handle})
    return out


def gql(*items: list[dict], also: list[dict] = (), token: str | None = None) -> dict:
    """A GraphQL feed page: *elements points at each item's Update; `also` is included but not pointed at."""
    included = [obj for group in items for obj in group] + list(also)
    pointers = [group[0]["entityUrn"] for group in items]
    return {"data": {"data": {"feedDashMainFeedByMainFeed": {"*elements": pointers,
                                                              "metadata": {"paginationToken": token}}}},
            "included": included}


ORIGINAL = update(ORIG, "John Roe", "john-roe", "The original thought.")
FEED = gql(
    update(A2, "Jane Doe", "jane-doe", "An older post about widgets.", counts=(12, 4, 2)),
    update(A1, "John Roe", "john-roe", "A newer post.", counts=None),
    update(AD, "Example Co", None, "Buy widgets!", entity=f"urn:li:fsd_update:(urn:li:sponsoredContentV2:(1,2),X)",
           company="example-co"),
    update(A3, "Jane Doe", "jane-doe", "Sharing this.", reshared=ORIG, header="Jane Doe reposted this"),
    also=ORIGINAL,
)


# --- Pure parsers ---------------------------------------------------------------------------------------------------

def test_feed_updates_parse_to_rows_with_counts_ads_left_out_and_null_when_not_shown():
    rows = posts.updates(FEED)
    assert [r["urn"] for r in rows] == [A2, A1, A3]  # the ad is gone; the reshared original isn't a row of its own
    older, newer, repost = rows
    assert set(older) == set(posts.FIELDS)
    assert older == {"url": f"https://www.linkedin.com/feed/update/{A2}/", "urn": A2, "author_name": "Jane Doe",
                     "author_handle": "jane-doe", "author_headline": "Jane's headline at Example Co",
                     "posted_at": iso(T2), "text": "An older post about widgets.", "reactions": 12, "comments": 4,
                     "reposts": 2, "kind": "post", "original_url": None, "image": None}
    assert (newer["reactions"], newer["comments"], newer["reposts"]) == (None, None, None)  # never a guessed 0
    assert repost["kind"] == "repost" and repost["original_url"] == f"https://www.linkedin.com/feed/update/{ORIG}/"


def test_hidden_counts_are_null_and_pulse_links_make_an_article():
    hidden = posts.updates(gql(update(A1, "Jane Doe", "jane-doe", "x", hide={"hideReactionsCount": True})))[0]
    assert (hidden["reactions"], hidden["comments"]) == (None, 1)
    art = posts.updates(gql(update(A1, "Jane Doe", "jane-doe", "x",
                                   article="https://www.linkedin.com/pulse/widgets-jane-doe")))[0]
    assert art["kind"] == "article"
    company = posts.updates(gql(update(A1, "Example Co", None, "x", company="example-co")))[0]
    assert company["author_handle"] == "https://www.linkedin.com/company/example-co/"


def test_urn_time_reads_the_id_and_refuses_implausible_ones():
    assert posts.urn_time(A1) == iso(T1)
    assert posts.urn_time("urn:li:activity:12345678") is None  # 1970: not a time-based id
    assert posts.urn_time(f"urn:li:activity:{sid(NOW + timedelta(days=400))}") is None
    assert posts.urn_time("urn:li:fsd_profile:x") is None


@pytest.mark.parametrize(("value", "urn"), [
    (A1, A1), ("urn:li:ugcPost:7380000000000000001", "urn:li:ugcPost:7380000000000000001"),
    (f"https://www.linkedin.com/feed/update/{A1}/", A1),
    ("https://www.linkedin.com/feed/update/urn%3Ali%3Ashare%3A7380000000000000002/", "urn:li:share:7380000000000000002"),
    ("https://www.linkedin.com/posts/jane-doe_widgets-activity-7380000000000000003-AbCd", "urn:li:activity:7380000000000000003"),
    ("linkedin.com/posts/jane-doe_widgets-ugcPost-7380000000000000004-AbCd", "urn:li:ugcPost:7380000000000000004"),
])
def test_post_targets(value, urn):
    assert posts.parse_post_target(value) == urn


@pytest.mark.parametrize("value", ["", "not-a-post", "https://example.com/posts/x-activity-7380000000000000003-a"])
def test_bad_post_targets(value):
    with pytest.raises(ValueError):
        posts.parse_post_target(value)


@pytest.mark.parametrize(("value", "target"), [
    ("jane-doe", ("member", "jane-doe")), ("@jane-doe", ("member", "jane-doe")), ("in/jane-doe", ("member", "jane-doe")),
    ("linkedin.com/in/jane-doe", ("member", "jane-doe")), ("https://www.linkedin.com/in/jane-doe/", ("member", "jane-doe")),
    ("company/example-co", ("company", "example-co")),
    ("https://www.linkedin.com/showcase/example-co-labs/", ("company", "example-co-labs")),
])
def test_author_targets(value, target):
    assert posts.parse_author_target(value) == target


@pytest.mark.parametrize("value", ["", "a b", "https://example.com/in/jane-doe", "https://www.linkedin.com/feed/"])
def test_bad_author_targets(value):
    with pytest.raises(ValueError):
        posts.parse_author_target(value)


@pytest.mark.parametrize(("value", "delta"), [("48h", timedelta(hours=48)), ("2d", timedelta(days=2)),
                                               ("90m", timedelta(minutes=90)), ("1w", timedelta(weeks=1)),
                                               ("3 days", timedelta(days=3))])
def test_since_takes_relative_times(value, delta):
    assert parse.parse_since(value, now=NOW) == NOW - delta


def test_since_still_takes_iso_and_refuses_prose():
    assert parse.parse_since("2026-10-18T00:00:00Z", now=NOW) == datetime(2026, 10, 18, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="48h"):
        parse.parse_since("yesterday", now=NOW)


def test_apply_since_drops_older_and_undated_and_newest_first_is_stable():
    rows = [{"posted_at": iso(T2)}, {"posted_at": None}, {"posted_at": iso(T1)}, {"posted_at": iso(T3)}]
    assert posts.apply_since(rows, NOW - timedelta(hours=48)) == [{"posted_at": iso(T2)}, {"posted_at": iso(T1)}]
    assert [r["posted_at"] for r in posts.newest_first(rows)] == [iso(T1), iso(T2), iso(T3), None]


@pytest.mark.parametrize(("text", "count"), [("29", 29), ("1,234", 1234), ("\u200b", None), ("", None),
                                             ("1.2K", None), (None, None), ("12 comments", None)])
def test_card_counts_are_exact_or_null(text, count):
    assert posts.card_count(text) == count


CARD = {"actorName": "Jane Doe", "actorHref": "https://www.linkedin.com/in/jane-doe/",
        "actorLines": ["Jane Doe", "• 2nd", "Widget engineer at Example Co", "3h •", "Follow"], "header": [],
        "text": "Widgets are great. https://example.com …more", "reactions": "7", "comments": "\u200b",
        "reposts": "", "articleLinks": [], "links": [], "ids": ""}


def test_a_search_card_becomes_a_row():
    row = posts.card_row(CARD, now=NOW)
    assert row == {"url": None, "urn": None, "author_name": "Jane Doe", "author_handle": "jane-doe",
                   "author_headline": "Widget engineer at Example Co", "posted_at": iso(NOW - timedelta(hours=3)),
                   "text": "Widgets are great. https://example.com", "reactions": 7, "comments": None,
                   "reposts": None, "kind": "post", "original_url": None, "image": None}
    assert posts.card_row({**CARD, "actorName": ""}, now=NOW) is None
    shared = posts.card_row({**CARD, "ids": "x-shareId=7380000000000000005-y"}, now=NOW)
    assert shared["urn"] == "urn:li:share:7380000000000000005"
    assert len(posts.cards([CARD, CARD, "junk"], now=NOW)) == 1


def flight(*pairs: tuple[str, str]) -> str:
    """The escaped RSC payload LinkedIn embeds (synthetic): per post, its legacy update urn then its permalink."""
    return "".join(f'{{\\"updateUrnLegacy\\":\\"urn:li:fsd_update:({urn},MAIN_FEED,DEBUG_REASON,DEFAULT,false)\\",'
                   f'\\"url\\":\\"https://www.linkedin.com/posts/{slug}\\"}},' for urn, slug in pairs)


def test_flight_pairs_give_cards_their_urns_in_order_when_the_handles_agree():
    pairs = posts.flight_posts(flight((A1, f"jane-doe_widgets-activity-{A1[-19:]}-AbCd"),
                                      (A2, "widgets-share-7380000000000000009-XyZw")))
    assert [u for u, _ in pairs] == [A1, A2]
    company = {**CARD, "actorName": "Example Co", "actorHref": "https://www.linkedin.com/company/example-co/"}
    rows = posts.attach_flight(posts.cards([CARD, company], now=NOW), pairs)
    assert [(r["urn"], r["posted_at"]) for r in rows] == [(A1, iso(T1)), (A2, iso(T2))]
    assert rows[0]["url"] == f"https://www.linkedin.com/feed/update/{A1}/"


def test_flight_pairs_out_of_step_only_fill_unambiguous_handle_matches():
    john = {**CARD, "actorName": "John Roe", "actorHref": "https://www.linkedin.com/in/john-roe/", "text": "other"}
    pairs = posts.flight_posts(flight((A1, "john-roe_x-activity-1-a"), (A2, "jane-doe_y-activity-2-b")))
    rows = posts.attach_flight(posts.cards([CARD, john], now=NOW), pairs)
    assert [r["urn"] for r in rows] == [A2, A1]
    rows = posts.attach_flight(posts.cards([CARD, john], now=NOW), pairs[:1] + pairs[:0])
    assert [r["urn"] for r in rows] == [None, A1]


def test_search_url_defaults_to_relevance_sorts_latest_on_request_and_narrows_when_since_fits():
    base = posts.search_url("AI agents", None, now=NOW)
    assert "keywords=AI%20agents" in base and "sortBy=%22relevance%22" in base and "datePosted" not in base
    assert "sortBy=%22date_posted%22" in posts.search_url("AI agents", None, sort="latest", now=NOW)
    assert posts.search_url("x", None, sort="top", now=NOW) == posts.search_url("x", None, now=NOW)
    with pytest.raises(ValueError):
        posts.search_url("x", None, sort="oldest", now=NOW)
    assert posts.search_url("x", NOW - timedelta(hours=20), now=NOW).endswith("datePosted=%22past-24h%22")
    assert posts.search_url("x", NOW - timedelta(hours=48), now=NOW).endswith("datePosted=%22past-week%22")
    assert posts.search_url("x", NOW - timedelta(days=20), now=NOW).endswith("datePosted=%22past-month%22")
    assert "datePosted" not in posts.search_url("x", NOW - timedelta(days=90), now=NOW)


# --- The guard ------------------------------------------------------------------------------------------------------

PAGE = f"https://www.linkedin.com{PAGINATION_PATH}?parentSpanId=abc&sduiid=def"
BODY = {"pagerId": SEARCH_PAGER, "clientArguments": {"screenId": SEARCH_SCREEN, "payload": {}},
        "paginationRequest": {"pagerId": SEARCH_PAGER, "start": 3}}


def req(method: str, url: str, body: object = None) -> SimpleNamespace:
    return SimpleNamespace(method=method, url=url, post_data=None if body is None else json.dumps(body))


def test_search_pagination_is_allowed_only_in_its_exact_shape():
    assert search_page_allowed("POST", PAGE, json.dumps(BODY))
    for method, url, body in [
        ("PUT", PAGE, BODY), ("POST", PAGE.replace("www.", "evil."), BODY),
        ("POST", PAGE.replace("https:", "http:"), BODY), ("POST", PAGE + "&action=react", BODY),
        ("POST", PAGE.replace("pagination", "react"), BODY), ("POST", PAGE, {**BODY, "reaction": "LIKE"}),
        ("POST", PAGE, {**BODY, "pagerId": "com.linkedin.sdui.feed.mainFeed"}),
        ("POST", PAGE, {**BODY, "clientArguments": {"screenId": "com.linkedin.sdui.Messaging"}}),
        ("POST", PAGE, {**BODY, "paginationRequest": {"pagerId": "x"}}), ("POST", PAGE, None),
    ]:
        assert not search_page_allowed(method, url, None if body is None else json.dumps(body)), (method, url, body)


@pytest.mark.parametrize(("method", "url", "body"), [
    ("POST", "https://www.linkedin.com/voyager/api/voyagerSocialDashReactions?action=create", {"reactionType": "LIKE"}),
    ("POST", "https://www.linkedin.com/voyager/api/voyagerSocialDashNormComments", {"commentary": "hi"}),
    ("POST", "https://www.linkedin.com/voyager/api/voyagerRelationshipsDashMemberRelationships?action=verifyQuotaAndCreate", {}),
    ("POST", "https://www.linkedin.com/voyager/api/feed/dash/followingStates/x", {}),
    ("POST", "https://www.linkedin.com/voyager/api/contentcreation/normShares", {}),
    ("POST", "https://www.linkedin.com/voyager/api/voyagerMessagingDashMessengerMessages?action=createMessage", {}),
    ("POST", "https://www.linkedin.com/flagship-web/rsc-action/actions/server-request?sduiid=x", {}),
    ("DELETE", "https://www.linkedin.com/voyager/api/feed/updates/x", None),
    ("GET", "https://www.linkedin.com/voyager/api/x?action=markAllItemsAsSeen", None),
])
def test_read_policies_block_every_write(method, url, body):
    for policy in (POLICY, search_policy([])):
        state = guard.Guard(policy=policy, send=False)
        assert not policy.allowed(req(method, url, body), state), (policy, method, url)


def test_search_policy_reads_and_records_the_pagination_it_lets_through():
    passed: list[str] = []
    policy = search_policy(passed)
    state = guard.Guard(policy=policy, send=False)
    assert policy.allowed(req("GET", "https://www.linkedin.com/search/results/content/?keywords=x"), state)
    assert policy.allowed(req("POST", PAGE, BODY), state)
    assert len(passed) == 1 and "cookie" not in passed[0].lower()
    assert not POLICY.allowed(req("POST", PAGE, BODY), guard.Guard(policy=POLICY, send=False))  # only search has it


# --- The fetch ops, on a scripted tab -------------------------------------------------------------------------------

def test_feed_reads_with_the_member_setting_order_and_sorts_newest_first(tab):
    site = tab(LinkedIn({fetch.VOYAGER_GQL: [FEED]}))
    rows = asyncio.run(fetch.feed(limit=2))
    assert [r["urn"] for r in rows] == [A1, A2]
    assert len(site.reads) == 1 and "sortOrder:MEMBER_SETTING" in site.reads[0] and "count:12" in site.reads[0]
    assert site.sent == []
    tab(LinkedIn({fetch.VOYAGER_GQL: [FEED]}))
    assert [r["urn"] for r in asyncio.run(fetch.feed(limit=5, ranked=True))] == [A2, A1, A3]  # LinkedIn's order


def test_feed_since_keeps_only_recent_posts(tab):
    tab(LinkedIn({fetch.VOYAGER_GQL: [FEED]}))
    since = iso(datetime.now(timezone.utc) - timedelta(days=36500))
    assert len(asyncio.run(fetch.feed(limit=5, since=since))) == 3
    tab(LinkedIn({fetch.VOYAGER_GQL: [FEED]}))
    assert asyncio.run(fetch.feed(limit=5, since=iso(datetime.now(timezone.utc)))) == []


def test_posts_from_looks_up_the_member_then_reads_their_share_feed(tab):
    lookup = {"included": [{"$type": PROFILE, "entityUrn": "urn:li:fsd_profile:ACoAAJaneDoe",
                            "publicIdentifier": "jane-doe"}]}
    mine = gql(update(A2, "Jane Doe", "jane-doe", "Older."), update(A1, "Jane Doe", "jane-doe", "Newer."))
    site = tab(LinkedIn({fetch.PROFILE_LOOKUP.split("{")[0]: lookup, fetch.VOYAGER_GQL: [mine]}))
    rows = asyncio.run(fetch.posts_from("https://www.linkedin.com/in/jane-doe/", limit=3))
    assert [r["urn"] for r in rows] == [A1, A2]
    assert "memberIdentity=jane-doe" in site.reads[0] and fetch.PROFILE_UPDATES_QUERY_ID in site.reads[1]
    assert "urn%3Ali%3Afsd_profile%3AACoAAJaneDoe" in site.reads[1] and site.sent == []


def test_posts_from_an_unknown_member_is_not_found(tab):
    tab(LinkedIn({fetch.PROFILE_LOOKUP.split("{")[0]: {"included": []}}))
    with pytest.raises(NotFound):
        asyncio.run(fetch.posts_from("nobody-here", limit=3))


def test_post_read_returns_the_one_post(tab):
    single = {"included": [obj for obj in update(A3, "Jane Doe", "jane-doe", "Sharing this.", reshared=ORIG)]
              + ORIGINAL}
    site = tab(LinkedIn({VOYAGER + "/feed/updatesV2": single}))
    row = asyncio.run(fetch.post_read(f"https://www.linkedin.com/feed/update/{A3}/"))
    assert row["urn"] == A3 and row["kind"] == "repost"
    assert unquote(site.reads[0]).endswith(f"urnOrNss={A3}")


def test_post_read_of_a_post_you_cant_see_is_not_found(tab):
    tab(LinkedIn({VOYAGER + "/feed/updatesV2": 404}))
    with pytest.raises(NotFound):
        asyncio.run(fetch.post_read(A1))


class SearchPage:
    """A scripted search tab: goto fires the document request (and its finished body), the cards are scripted, and
    the first scroll fires one pagination request through the page's policy, as the page itself would."""

    def __init__(self, cards: list[list[dict]], bodies: list[str], state: dict | None = None) -> None:
        self.cards, self.bodies, self.url, self.policy = cards, bodies, "about:blank", None
        self.state = state or {}
        self.scrolled_list = 0
        self.handlers: dict[str, list] = {}
        self.gotos: list[str] = []
        self.mouse = SimpleNamespace(wheel=self._wheel)
        self.shown = 0

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def _fire(self, url: str, body: str) -> None:
        async def response():
            return SimpleNamespace(body=lambda: asyncio.sleep(0, body.encode()))
        request = SimpleNamespace(url=url, response=response)
        for event in ("request", "requestfinished"):
            for handler in self.handlers.get(event, []):
                handler(request)

    async def goto(self, url, **_options):
        self.gotos.append(url)
        self.url = url
        self._fire(url, self.bodies[0])

    async def _wheel(self, _x, _y):
        if self.shown + 1 < len(self.cards):
            self.shown += 1
            request = req("POST", PAGE, BODY)
            assert self.policy.allowed(request, guard.Guard(policy=self.policy, send=False))
            self._fire(PAGE, self.bodies[self.shown])

    async def evaluate(self, script, arg=None):
        if script is fetch.POST_CARD_JS:
            return [card for batch in self.cards[: self.shown + 1] for card in batch]
        if script is fetch.SCROLL_LAST_JS:  # the results list's own scroller, not the window
            self.scrolled_list += 1
            return None
        if script is fetch.SEARCH_STATE_JS:
            return self.state
        return None

    async def wait_for_function(self, *_args, **_options):
        pass

    async def wait_for_timeout(self, _ms):
        await asyncio.sleep(0)


def install_search(monkeypatch, page: SearchPage) -> SearchPage:
    from contextlib import asynccontextmanager
    from webcli.core import browser

    @asynccontextmanager
    async def site_page(policy, **_options):
        page.policy = policy
        yield page

    monkeypatch.setattr(browser, "site_page", site_page)
    return page


JOHN = {**CARD, "actorName": "John Roe", "actorHref": "https://www.linkedin.com/in/john-roe/", "text": "other"}
COMPANY = {**CARD, "actorName": "Example Co", "actorHref": "https://www.linkedin.com/company/example-co/",
           "text": "third"}


def search_page(state: dict | None = None) -> SearchPage:
    return SearchPage([[CARD, JOHN], [COMPANY]],
                      [flight((A2, "jane-doe_a-activity-1-a"), (A1, "john-roe_b-activity-2-b")),
                       flight((A3, "widgets-share-3-c"))], state)


def test_posts_search_keeps_linkedins_relevance_order_and_scrolls_the_results_list(monkeypatch):
    page = install_search(monkeypatch, search_page())
    rows = asyncio.run(fetch.posts_search("AI agents", limit=3))
    assert [(r["author_name"], r["urn"]) for r in rows] == [("Jane Doe", A2), ("John Roe", A1), ("Example Co", A3)]
    assert "sortBy=%22relevance%22" in page.gotos[0]
    assert page.scrolled_list >= 1  # the list's scroller was moved (the window alone never loads more)


def test_posts_search_latest_sorts_newest_first_and_since_is_cut_here(monkeypatch):
    page = install_search(monkeypatch, search_page())
    rows = asyncio.run(fetch.posts_search("AI agents", limit=3, sort="latest"))
    assert [(r["author_name"], r["urn"]) for r in rows] == [("John Roe", A1), ("Jane Doe", A2), ("Example Co", A3)]
    assert "sortBy=%22date_posted%22" in page.gotos[0]
    page = install_search(monkeypatch, search_page())
    since = iso(datetime.now(timezone.utc) - timedelta(days=36500))
    assert len(asyncio.run(fetch.posts_search("AI agents", limit=2, since=since))) == 2


def test_posts_search_with_nothing_inside_since_says_so(monkeypatch):
    install_search(monkeypatch, search_page())
    with pytest.raises(NotFound, match=r"returned 3 result\(s\).*--since"):
        asyncio.run(fetch.posts_search("AI agents", limit=5, since=iso(datetime.now(timezone.utc))))


@pytest.mark.parametrize(("state", "reason"), [
    ({"noResults": True}, "LinkedIn found no posts for 'nothing here'"),
    ({}, "showed no result cards"),
])
def test_posts_search_with_no_cards_is_never_silent(monkeypatch, state, reason):
    install_search(monkeypatch, SearchPage([[]], [""], state))
    with pytest.raises(NotFound, match=reason):
        asyncio.run(fetch.posts_search("nothing here", limit=5))


def test_posts_search_stops_at_a_security_check(monkeypatch):
    install_search(monkeypatch, SearchPage([[]], [""], {"challenge": True}))
    with pytest.raises(AuthRequired, match="security check"):
        asyncio.run(fetch.posts_search("AI agents", limit=5))


def test_posts_search_stops_when_linkedin_pauses_search(monkeypatch):
    from webcli.core.errors import RateLimited
    install_search(monkeypatch, SearchPage([[]], [""], {"limited": True}))
    with pytest.raises(RateLimited, match="paused search"):
        asyncio.run(fetch.posts_search("AI agents", limit=5))


def test_posts_search_refuses_an_unknown_sort():
    from webcli.core.errors import UsageError
    with pytest.raises(UsageError):
        asyncio.run(fetch.posts_search("AI agents", sort="oldest"))


# --- Feed depth -----------------------------------------------------------------------------------------------------

def feed_page(start: int, n: int, *, token: str | None = "t", age: timedelta = timedelta(hours=1)) -> dict:
    """n posts, each `age` apart from the one before, starting `start` steps back from now."""
    now = datetime.now(timezone.utc)
    items = [update(f"urn:li:activity:{sid(now - age * (start + i), 5)}", "Jane Doe", "jane-doe", f"post {start + i}")
             for i in range(n)]
    return gql(*items, token=token)


def test_feed_keeps_paging_past_a_short_page_until_since_has_enough(tab):
    pages = [feed_page(0, 24), feed_page(24, 25), feed_page(49, 25), feed_page(74, 25)]
    site = tab(LinkedIn({fetch.VOYAGER_GQL: pages}))
    rows = asyncio.run(fetch.feed(limit=50, since=iso(datetime.now(timezone.utc) - timedelta(hours=200))))
    assert len(rows) == 50 and len(site.reads) == 3  # 24 + 25 + 25 read: the 24-row page was not the end
    assert "paginationToken:t" in site.reads[1] and site.sent == []


def test_feed_stops_at_the_read_budget_and_at_the_end_of_the_feed(tab):
    old = timedelta(days=30)  # nothing inside --since: it pages until the budget, never past it
    pages = [feed_page(25 * i + 1, 25, age=old) for i in range(20)]
    site = tab(LinkedIn({fetch.VOYAGER_GQL: pages}))
    assert asyncio.run(fetch.feed(limit=50, since=iso(datetime.now(timezone.utc) - timedelta(hours=1)))) == []
    assert len(site.reads) == fetch.FEED_READ_BUDGET // fetch.POSTS_PAGE
    site = tab(LinkedIn({fetch.VOYAGER_GQL: [feed_page(0, 25), feed_page(25, 0, token=None)]}))
    assert len(asyncio.run(fetch.feed(limit=50))) == 25 and len(site.reads) == 2


def test_feed_without_since_reads_until_the_limit(tab):
    site = tab(LinkedIn({fetch.VOYAGER_GQL: [feed_page(0, 25), feed_page(25, 25), feed_page(50, 10)]}))
    assert len(asyncio.run(fetch.feed(limit=50))) == 50 and len(site.reads) == 3  # limit + 10 read, then sorted
    assert "count:10," in site.reads[2]


# --- posts read --comments ------------------------------------------------------------------------------------------

LEGACY = "com.linkedin.voyager"


def legacy_update(urn: str, comment_ids: list[int], replies: dict[int, int] | None = None) -> dict:
    """updatesV2?commentsCount=N: the UpdateV2, its socialDetail listing the top-level comments, each comment with
    its commenter's miniProfile and its own counts; plus one reply (left out of top_comments)."""
    activity = urn.split(":")[-1]
    detail = f"urn:li:fs_socialDetail:{urn}"
    included = [
        {"$type": f"{LEGACY}.feed.render.UpdateV2", "entityUrn": f"urn:li:fs_updateV2:({urn},FEED_DETAIL,EMPTY,DEFAULT,false)",
         "updateMetadata": {"urn": urn}, "actor": {"name": {"text": "Jane Doe"}, "description": {"text": "Widgets"}},
         "commentary": {"text": {"text": "The post."}}, "*socialDetail": detail},
        {"$type": f"{LEGACY}.feed.SocialDetail", "entityUrn": detail,
         "comments": {"*elements": [f"urn:li:fs_objectComment:({c},activity:{activity})" for c in comment_ids],
                      "paging": {"total": 91}}},
    ]
    for n, cid in enumerate([*comment_ids, 999]):
        urn_c = f"urn:li:comment:(activity:{activity},{cid})"
        mini = f"urn:li:fs_miniProfile:P{cid}"
        counts = f"urn:li:fs_socialActivityCounts:{urn_c}"
        included += [
            {"$type": f"{LEGACY}.feed.Comment", "entityUrn": f"urn:li:fs_objectComment:({cid},activity:{activity})",
             "urn": urn_c, "commenter": {"*miniProfile": mini}, "commentV2": {"text": f"Comment {cid}"},
             "createdTime": int((NOW - timedelta(minutes=n)).timestamp() * 1000), "pinned": n == 0,
             "permalink": f"https://www.linkedin.com/feed/update/{urn}?commentUrn=x{cid}",
             "*socialDetail": f"urn:li:fs_socialDetail:{urn_c}",
             **({"parentCommentUrn": f"urn:li:comment:(activity:{activity},{comment_ids[0]})"} if cid == 999 else {})},
            {"$type": f"{LEGACY}.identity.shared.MiniProfile", "entityUrn": mini, "firstName": "Pat",
             "lastName": f"Q{cid}", "occupation": "Builder", "publicIdentifier": f"pat-q{cid}"},
            {"$type": f"{LEGACY}.feed.SocialDetail", "entityUrn": f"urn:li:fs_socialDetail:{urn_c}",
             "*totalSocialActivityCounts": counts},
            {"$type": f"{LEGACY}.feed.shared.SocialActivityCounts", "entityUrn": counts, "urn": urn_c,
             "numLikes": 4 if cid != 3 else None, "numComments": (replies or {}).get(cid, 0)},
        ]
    return {"included": included}


def test_top_comments_follow_linkedins_order_skip_replies_and_never_invent_counts():
    data = legacy_update(A1, [3, 1, 2], replies={1: 2})
    rows = posts.top_comments(data, A1, 10)
    assert [r["text"] for r in rows] == ["Comment 3", "Comment 1", "Comment 2"]  # 999 is a reply: left out
    first = rows[0]
    assert set(first) == set(posts.COMMENT_FIELDS)
    assert first["author_name"] == "Pat Q3" and first["author_handle"] == "pat-q3" and first["pinned"] is True
    assert first["reactions"] is None and rows[1]["reactions"] == 4 and rows[1]["replies"] == 2
    assert first["posted_at"] == iso(NOW) and first["url"].startswith("https://www.linkedin.com/feed/update/")
    assert len(posts.top_comments(data, A1, 2)) == 2 and posts.top_comments(data, A1, 0) == []


def test_post_read_asks_for_its_top_comments_in_the_same_get(tab):
    site = tab(LinkedIn({VOYAGER + "/feed/updatesV2": legacy_update(A1, [3, 1, 2])}))
    row = asyncio.run(fetch.post_read(A1, comments=2))
    assert row["urn"] == A1 and [c["text"] for c in row["top_comments"]] == ["Comment 3", "Comment 1"]
    assert "commentsCount=2&" in site.reads[0] and len(site.reads) == 1 and site.sent == []
    tab(LinkedIn({VOYAGER + "/feed/updatesV2": legacy_update(A1, [3])}))
    assert asyncio.run(fetch.post_read(A1, comments=0))["top_comments"] == []


# --- The CLI --------------------------------------------------------------------------------------------------------

ROW = posts.updates(FEED)[0]


def test_feed_cli_defaults_and_relative_since_becomes_absolute(calls):
    seen, answers = calls
    answers["feed"] = [ROW]
    result = run("feed", "--since", "48h", "--format", "json")
    assert result.exit_code == 0, result.output
    op, args, _ = seen[0]
    assert op == "feed" and args[0] == 20 and args[2] is False
    since = datetime.fromisoformat(args[1].replace("Z", "+00:00"))
    assert abs((datetime.now(timezone.utc) - timedelta(hours=48) - since).total_seconds()) < 120
    assert json.loads(result.output)[0]["urn"] == A2


def test_limit_is_capped_at_50(calls):
    seen, answers = calls
    answers["posts_search"] = []
    assert run("posts", "search", "widgets", "--limit", "500", "--format", "json").exit_code == 0
    assert seen[0][0] == "posts_search" and seen[0][1] == ("widgets", 50, None, "relevance")


def test_posts_from_and_read_cli(calls):
    seen, answers = calls
    answers["posts_from"] = [ROW]
    answers["post_read"] = ROW
    result = run("posts", "from", "jane-doe", "--limit", "3", "--format", "csv")
    assert result.exit_code == 0 and result.output.splitlines()[0].split(",")[:2] == ["url", "urn"]
    result = run("posts", "read", A2, "--format", "json")
    assert result.exit_code == 0 and json.loads(result.output)["urn"] == A2
    assert [s[0] for s in seen] == ["posts_from", "post_read"]
    assert seen[0][1] == ("jane-doe", 3, None) and seen[1][1] == (A2, 10)


def test_posts_search_cli_sort_and_empty_exit_3_with_the_reason(calls):
    seen, answers = calls
    answers["posts_search"] = [ROW]
    assert run("posts", "search", "widgets", "--sort", "latest", "--format", "json").exit_code == 0
    assert seen[-1][1] == ("widgets", 20, None, "latest")
    assert run("posts", "search", "widgets", "--sort", "oldest").exit_code == 2

    def empty(*_args, **_kwargs):
        raise NotFound("LinkedIn found no posts for 'widgets'.")

    answers["posts_search"] = empty
    result = run("posts", "search", "widgets", "--format", "json")
    assert result.exit_code == 3 and "found no posts" in result.output


def test_posts_read_cli_comments(calls):
    seen, answers = calls
    comment = {"author_name": "Pat Q", "author_handle": "pat-q", "author_headline": "Builder",
               "posted_at": iso(NOW), "text": "Nice.", "reactions": 4, "replies": 0, "pinned": False, "url": None}
    answers["post_read"] = {**ROW, "top_comments": [comment]}
    result = run("posts", "read", A2, "--comments", "3", "--format", "json")
    assert result.exit_code == 0 and json.loads(result.output)["top_comments"][0]["text"] == "Nice."
    assert seen[-1][1] == (A2, 3)
    assert run("posts", "read", A2, "--comments", "500", "--format", "json").exit_code == 0
    assert seen[-1][1] == (A2, 50)
    result = run("posts", "read", A2, "--format", "csv")
    assert result.exit_code == 0 and "top_comments" in result.output.splitlines()[0]
    assert run("posts", "read", A2, "--format", "md").exit_code == 0


def test_flight_pairs_carry_ugcpost_and_share_urns_too():
    ugc, share = "urn:li:ugcPost:7380000000000000001", "urn:li:share:7380000000000000002"
    pairs = posts.flight_posts(flight((ugc, "jane-doe_a-ugcPost-7380000000000000001-ab"),
                                      (share, "john-roe_b-share-7380000000000000002-cd")))
    assert [urn for urn, _ in pairs] == [ugc, share]
    assert pairs[0][1] == "https://www.linkedin.com/posts/jane-doe_a-ugcPost-7380000000000000001-ab"


def test_posts_search_with_cards_it_cannot_read_names_the_markup_not_the_page(monkeypatch):
    nameless = {**CARD, "actorName": "", "actorHref": ""}
    install_search(monkeypatch, SearchPage([[nameless, nameless]], [""], {}))
    with pytest.raises(NotFound, match=r"rendered 2 result card\(s\).*CARD_PARTS_JS"):
        asyncio.run(fetch.posts_search("AI adoption", limit=5, sort="latest"))


def test_posts_search_reads_urns_from_the_pages_own_stream_responses(monkeypatch):
    from webcli.sites.linkedin.endpoints import STREAM_PATH

    class StreamingPage(SearchPage):
        async def goto(self, url, **options):
            await super().goto(url, **options)
            self._fire(f"https://www.linkedin.com{STREAM_PATH}?x=1", flight((A2, "jane-doe_a-activity-1-a")))

    install_search(monkeypatch, StreamingPage([[CARD]], [""]))
    rows = asyncio.run(fetch.posts_search("AI adoption", limit=1))
    assert [(r["author_name"], r["urn"]) for r in rows] == [("Jane Doe", A2)]


@pytest.mark.parametrize(("line", "minutes"), [("Just now", 0), ("with 2 others • Just now", 0), ("3h •", 180),
                                               ("2d • Edited", 2880), ("5m • Edited •", 5)])
def test_card_time_lines(line, minutes):
    now = datetime(2026, 10, 23, 17, 0, tzinfo=timezone.utc)
    row = posts.card_row({**CARD, "actorLines": ["Jane Doe", "Builder", line]}, now=now)
    assert datetime.fromisoformat(row["posted_at"]) == now - timedelta(minutes=minutes)
    assert row["author_headline"] == "Builder"


def test_update_image_from_content_vector_image_and_pointer():
    """0.17.2: the post's own image: a VectorImage in content (widest artifact <= 1280), through a * pointer, or the
    reshared update's; avatars and other hosts never count."""
    root = "https://media.licdn.com/dms/image/v2/D4E22AQFake/"
    vec = {"rootUrl": root, "artifacts": [{"width": 480, "fileIdentifyingUrlPathSegment": "s480/0/1?e=1&t=a"},
                                          {"width": 1280, "fileIdentifyingUrlPathSegment": "s1280/0/1?e=1&t=b"},
                                          {"width": 2048, "fileIdentifyingUrlPathSegment": "s2048/0/1?e=1&t=c"}]}
    content = {"imageComponent": {"images": [{"attributes": [{"detailData": {"vectorImage": vec}}]}]}}
    assert posts.image_url(content) == root + "s1280/0/1?e=1&t=b"
    index = {"urn:li:fsd_image:1": {"entityUrn": "urn:li:fsd_image:1", "vectorImage": vec}}
    assert posts.image_url({"articleComponent": {"*largeImage": "urn:li:fsd_image:1"}}, index) == \
        root + "s1280/0/1?e=1&t=b"
    assert posts.image_url({"x": "https://media.licdn.com/dms/image/v2/C/profile-displayphoto-shrink_200_200/0"}) is None
    assert posts.image_url({"x": "https://evil.example/dms/image/a.jpg"}) is None
    assert posts.image_url(None) is None
    rows = posts.updates({"included": [
        {**update(A1, "Jane Doe", "jane-doe", "Look")[0], "content": content}]})
    assert rows[0]["image"] == root + "s1280/0/1?e=1&t=b"
    assert posts.updates({"included": update(A2, "John Roe", "john-roe", "Text only")})[0]["image"] is None
