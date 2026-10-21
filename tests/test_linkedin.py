"""webcli linkedin, offline: the author check on both writes (dry run by default; --confirm refuses, exit 4, unless
signed in as the expected author, which is configured; no personal handle ships as a default), the guard and its one-command write scope, limsg's parsers, and
the messaging reads. Nothing reaches LinkedIn: pages are scripted, and every write a scripted page receives first
passes the page's real policy, the way the guard would (a refused write never leaves: status 0). No Chrome starts.
"""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from webcli.apps import webcli as webcli_app
from webcli.core import browser, config, daemon, guard
from webcli.core.errors import AuthRequired, NotFound
from webcli.sites.linkedin import author as authors
from webcli.sites.linkedin import boost, fetch, parse
from webcli.sites.linkedin.guard import POLICY, WriteScope, request_allowed, write_policy

from conftest import make_profile
from google_fakes import ScriptedPage

ME = "https://www.linkedin.com/voyager/api/me"
REGISTER = "https://www.linkedin.com/voyager/api/voyagerVideoDashMediaUploadMetadata?action=upload"
UPLOAD = "https://www.linkedin.com/dms-uploads/sp/v2/D4E10AQFake/uploaded-image/0"
CREATE = "https://www.linkedin.com/voyager/api/contentcreation/normShares"
SEND = "https://www.linkedin.com/voyager/api/voyagerMessagingDashMessengerMessages?action=createMessage"
GQL = "https://www.linkedin.com/voyager/api/voyagerMessagingGraphQL/graphql"
ASSET = "urn:li:digitalmediaAsset:D4E10AQFake"
SHARE = "urn:li:share:7384000000000000001"
CONV = "urn:li:msg_conversation:(urn:li:fsd_profile:ACoAAKevnkFake,2-ThreadFake==)"
MINI = "com.linkedin.voyager.identity.shared.MiniProfile"
KEVNK = {"$type": MINI, "entityUrn": "urn:li:fs_miniProfile:ACoAAKevnkFake",
         "dashEntityUrn": "urn:li:fsd_profile:ACoAAKevnkFake", "objectUrn": "urn:li:member:4242",
         "firstName": "Kevin", "lastName": "Kirchner", "publicIdentifier": "kevnk"}
# The incident limsg's check exists for: a second account with the same name.
OTHER = {"$type": MINI, "entityUrn": "urn:li:fs_miniProfile:ACoAAOtherFake",
         "dashEntityUrn": "urn:li:fsd_profile:ACoAAOtherFake", "objectUrn": "urn:li:member:999",
         "firstName": "Kevin", "lastName": "Kirchner", "publicIdentifier": "kevin-kirchner-817709300"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (2).to_bytes(4, "big") + (3).to_bytes(4, "big") + b"\x08\x06"
THREADS = {"included": [
    {"$type": "com.linkedin.messenger.MessagingParticipant", "entityUrn": "urn:li:msg_messagingParticipant:boardy",
     "participantType": {"member": {"firstName": {"text": "Boardy"}, "lastName": {"text": "AI"}}}},
    {"$type": "com.linkedin.messenger.Conversation", "entityUrn": CONV, "lastActivityAt": 1760450400000,
     "unreadCount": 2, "*conversationParticipants": ["urn:li:msg_messagingParticipant:boardy"],
     "lastMessage": {"body": {"text": "Want an intro?"}}},
]}


def me_as(profile: dict, *others: dict) -> dict:
    return {"data": {"*miniProfile": profile["entityUrn"], "$type": "com.linkedin.voyager.common.Me"},
            "included": [*others, profile]}


class LinkedIn:
    """A scripted www.linkedin.com tab. GETs are answered from `gets` (URL prefix -> body, or a list of bodies used in
    turn; a body may be an int status), writes from `writes` ((method, URL prefix) -> (status, body, x-restli-id)).
    Every write passes the page's real policy first, with the send permission the command opened its page with.
    csrf: whether the profile has its JSESSIONID yet. A fresh Chrome has none (fresh=True); loading the messaging page
    sets it, and without it the page's GETs answer -1, as GET_JS does."""

    def __init__(self, gets: dict, writes: dict | None = None, *, fresh: bool = False,
                 redirects: dict | None = None) -> None:
        self.gets, self.writes, self.csrf = gets, writes or {}, not fresh
        self.reads: list[str] = []
        self.sent: list[tuple[str, str, object]] = []
        self.page = ScriptedPage(redirects=redirects, answers={
            "redirect: 'error'": self.write, "redirect: 'manual'": self.get,
            "JSESSIONID=/.test": lambda _arg: self.csrf})
        self.page.options = {}
        goto = self.page.goto

        async def app_page_sets_csrf(url, **options):
            await goto(url, **options)
            self.csrf = self.csrf or self.page.url == fetch.MESSAGING_URL

        self.page.goto = app_page_sets_csrf

    def _answer(self, table: dict, key):
        for prefix, value in table.items():
            if (key[0] == prefix[0] and key[1].startswith(prefix[1])) if isinstance(prefix, tuple) else key.startswith(prefix):
                return value.pop(0) if isinstance(value, list) else value
        raise AssertionError(f"unscripted {key}")

    def get(self, arg):
        if not self.csrf:
            return {"status": -1, "text": ""}
        url = arg[0]
        self.reads.append(url)
        body = self._answer(self.gets, url)
        return {"status": body, "text": ""} if isinstance(body, int) else {"status": 200, "text": json.dumps(body)}

    def write(self, arg):
        url, method, _headers, body, b64 = arg
        request = SimpleNamespace(method=method, url=url, post_data=None if body is None else json.dumps(body))
        state = guard.Guard(policy=self.page.policy, send=bool(self.page.options.get("send")))
        if not self.page.policy.allowed(request, state):
            guard.state.blocked.append(self.page.policy.describe(request))
            return {"status": 0, "text": "", "restliId": None}
        self.sent.append((method, url, body if body is not None else base64.b64decode(b64)))
        status, answer, restli = self._answer(self.writes, (method, url))
        return {"status": status, "text": json.dumps(answer), "restliId": restli}


@pytest.fixture
def tab(monkeypatch):
    """install(LinkedIn(...)) makes browser.site_page yield its page, recording the policy and options."""

    def install(site: LinkedIn) -> LinkedIn:
        @asynccontextmanager
        async def site_page(policy, **options):
            site.page.policy, site.page.options = policy, options
            yield site.page

        monkeypatch.setattr(browser, "site_page", site_page)
        return site

    return install


@pytest.fixture
def png(tmp_path):
    path = tmp_path / "card.png"
    path.write_bytes(PNG)
    return str(path)


def publishing(me_first=KEVNK, me_again=KEVNK) -> LinkedIn:
    decoy = KEVNK if me_first is OTHER else OTHER  # listed first in included: the pointer decides, not the order
    return LinkedIn({ME: [me_as(me_first, decoy), me_as(me_again)]}, {
        ("POST", REGISTER): (200, {"data": {"value": {"urn": ASSET, "singleUploadUrl": UPLOAD,
                                                       "singleUploadHeaders": {"media-type-family": "STILLIMAGE"}}}},
                             None),
        ("PUT", UPLOAD): (201, None, None),
        ("POST", CREATE): (201, {"data": {"status": {"urn": SHARE}}}, None),
    })


def messaging(me_first=KEVNK, me_again=KEVNK) -> LinkedIn:
    return LinkedIn({ME: [me_as(me_first), me_as(me_again)], GQL: THREADS},
                    {("POST", SEND): (201, {"value": {"entityUrn": "urn:li:msg_message:x"}}, None)})


# The author check: post create.

def test_a_post_dry_run_reads_me_once_and_writes_nothing(tab, png):
    site = tab(publishing())
    out = asyncio.run(fetch.post("Shipping today.", png, "kevnk"))
    assert site.sent == [] and site.reads == [ME]
    assert site.page.gotos == ["https://www.linkedin.com/robots.txt"] and site.page.options["send"] is False
    assert out == {"dryRun": True, "loggedIn": True, "author": {
        "name": "Kevin Kirchner", "publicIdentifier": "kevnk", "profileUrn": "urn:li:fsd_profile:ACoAAKevnkFake",
        "memberUrn": "urn:li:member:4242"}, "expectedAuthor": "kevnk", "authorMatches": True,
        "text": "Shipping today.", "textLength": 15, "image": {
            "path": png, "filename": "card.png", "type": "image/png", "bytes": len(PNG), "width": 2, "height": 3}}


def test_confirm_as_the_wrong_account_is_refused_with_zero_writes(tab, png):
    site = tab(publishing(me_first=OTHER))
    out = asyncio.run(fetch.post("Shipping today.", png, "kevnk", confirm=True))
    assert (out["posted"], out["authorMatches"], out["author"]["publicIdentifier"]) == (
        False, False, "kevin-kirchner-817709300")
    assert site.sent == [] and site.reads == [ME]


def test_confirm_as_the_expected_author_registers_uploads_rechecks_and_creates_once(tab, png):
    site = tab(publishing())
    out = asyncio.run(fetch.post("Shipping today.", png, "KevNK", confirm=True))  # the handle ignores case
    assert [(m, u) for m, u, _ in site.sent] == [("POST", REGISTER), ("PUT", UPLOAD), ("POST", CREATE)]
    assert site.reads == [ME, ME]  # the author, then the re-check right before the create
    assert site.sent[0][2] == {"mediaUploadType": "IMAGE_SHARING", "fileSize": len(PNG), "filename": "card.png"}
    assert site.sent[1][2] == PNG
    assert site.sent[2][2]["commentaryV2"]["text"] == "Shipping today."
    assert site.sent[2][2]["media"] == [{"category": "IMAGE", "mediaUrn": ASSET, "tapTargets": []}]
    assert (out["posted"], out["urn"], out["url"], out["image"]["assetUrn"]) == (
        True, SHARE, f"https://www.linkedin.com/feed/update/{SHARE}/", ASSET)
    # The scope is spent: replaying the create (or any of the three) is blocked.
    replay = SimpleNamespace(method="POST", url=CREATE, post_data=json.dumps(site.sent[2][2]))
    assert not site.page.policy.allowed(replay, guard.Guard(send=True))


@pytest.mark.parametrize("second", [OTHER, {**KEVNK, "objectUrn": "urn:li:member:31337"}, 401])
def test_an_account_change_before_the_create_refuses_and_creates_nothing(tab, png, second):
    site = tab(LinkedIn({ME: [me_as(KEVNK), me_as(second) if isinstance(second, dict) else second]},
                        publishing().writes))
    with pytest.raises(AuthRequired, match="REFUSED"):
        asyncio.run(fetch.post("Shipping today.", png, "kevnk", confirm=True))
    assert [(m, u) for m, u, _ in site.sent] == [("POST", REGISTER), ("PUT", UPLOAD)]  # never the create


def test_an_upload_url_off_linkedin_stops_before_the_upload(tab, png):
    site = tab(publishing())
    site.writes[("POST", REGISTER)] = (200, {"value": {"urn": ASSET, "singleUploadUrl": "https://evil.example/u"}}, None)
    with pytest.raises(RuntimeError, match="unexpected URL host"):
        asyncio.run(fetch.post("Shipping today.", png, "kevnk", confirm=True))
    assert [(m, u) for m, u, _ in site.sent] == [("POST", REGISTER)]


def test_a_create_without_a_urn_is_live_and_warns_not_to_rerun(tab, png):
    site = tab(publishing())
    site.writes[("POST", CREATE)] = (201, {"data": {"included": [{"urn": "urn:li:share:999"}]}}, None)
    out = asyncio.run(fetch.post("Shipping today.", png, "kevnk", confirm=True))
    assert out["posted"] is True and out["urn"] is None and "do NOT re-run" in out["warning"]


# The author check: messages send.

def test_a_send_dry_run_resolves_the_thread_and_sends_nothing(tab):
    site = tab(messaging())
    out = asyncio.run(fetch.send("boardy", "ping", "kevnk"))
    assert site.sent == [] and site.page.options["send"] is False
    assert {k: out[k] for k in ("dryRun", "to", "threadId", "entityUrn", "text", "authorMatches")} == {
        "dryRun": True, "to": "Boardy AI", "threadId": "2-ThreadFake==", "entityUrn": CONV, "text": "ping",
        "authorMatches": True}


def test_send_confirm_as_the_wrong_account_is_refused_with_zero_writes(tab):
    site = tab(messaging(me_first=OTHER))
    out = asyncio.run(fetch.send("boardy", "ping", "kevnk", confirm=True))
    assert (out["sent"], out["authorMatches"]) == (False, False) and site.sent == []


def test_send_confirm_as_the_expected_author_sends_once_to_that_thread(tab):
    site = tab(messaging())
    out = asyncio.run(fetch.send("boardy", "ping", "kevnk", confirm=True))
    [(method, url, body)] = site.sent
    assert (method, url) == ("POST", SEND) and site.page.options["send"] is True
    assert body["message"]["conversationUrn"] == CONV and body["message"]["body"]["text"] == "ping"
    assert body["mailboxUrn"] == KEVNK["dashEntityUrn"] and "recipientProfileUrns" not in body
    assert out["sent"] is True and out["status"] == 201
    replay = SimpleNamespace(method="POST", url=SEND, post_data=json.dumps(body))
    assert not site.page.policy.allowed(replay, guard.Guard(send=True))  # one message per --confirm


def test_send_refuses_when_the_account_changes_before_the_send(tab):
    site = tab(messaging(me_again=OTHER))
    with pytest.raises(AuthRequired, match="REFUSED.*Nothing was sent"):
        asyncio.run(fetch.send("boardy", "ping", "kevnk", confirm=True))
    assert site.sent == []


def test_send_needs_a_thread_among_the_newest(tab):
    tab(messaging())
    with pytest.raises(NotFound, match="No thread matching 'nobody'"):
        asyncio.run(fetch.send("nobody", "ping", "kevnk", confirm=True))


# The CLI: dry run by default, --dry-run wins, exit 4 on a mismatch.

@pytest.fixture(autouse=True)
def _configured_author():
    # The package ships no personal default; tests that assume an author configure kevnk here.
    config.save({"sites": {"linkedin": {"expect_author": "kevnk"}}})
    yield


@pytest.fixture
def calls(monkeypatch):
    make_profile("linkedin:default")
    seen, answers = [], {}

    async def call(ident, site, op, *args, **kwargs):
        seen.append((op, args, kwargs))
        value = answers[op]
        return value(*args, **kwargs) if callable(value) else value

    monkeypatch.setattr(daemon, "call", call)
    return seen, answers


def run(*args):
    return CliRunner().invoke(webcli_app.build(), ["linkedin", *args])


def preview(expect: str, signed: dict, **extra) -> dict:
    return {"dryRun": True, "author": parse.parse_me(me_as(signed)), "expectedAuthor": expect,
            "authorMatches": authors.matches(parse.parse_me(me_as(signed)), expect), **extra}


def post_answer(signed=KEVNK):
    def answer(text, path, expect, *, confirm, headed):
        out = {**preview(expect, signed), "dryRun": not confirm, "loggedIn": True, "text": text, "textLength": len(text),
               "image": {"path": path, "filename": "card.png", "type": "image/png", "bytes": 10, "width": 2,
                         "height": 3}}
        if confirm:
            out.update({"posted": True, "urn": SHARE, "url": parse.post_url(SHARE)} if out["authorMatches"]
                       else {"posted": False})
        return out

    return answer


@pytest.mark.parametrize("flags", [[], ["--dry-run"], ["--dry-run", "--confirm"], ["--confirm", "--dry-run"]])
def test_post_create_is_a_dry_run_unless_confirm_alone(calls, png, flags):
    seen, answers = calls
    answers["post"] = post_answer()
    result = run("post", "create", "--text", "Shipping today.", "--image", png, "--format", "json", *flags)
    assert result.exit_code == 0 and json.loads(result.stdout)["dryRun"] is True
    assert seen == [("post", ("Shipping today.", png, "kevnk"), {"confirm": False, "headed": False})]


@pytest.mark.parametrize("flag", ["--confirm", "--yes"])
def test_post_create_confirm_publishes_as_the_configured_author(calls, png, flag):
    seen, answers = calls
    answers["post"] = post_answer()
    result = run("post", "create", "--text", "Shipping today.", "--image", png, "--format", "json", flag)
    assert result.exit_code == 0 and json.loads(result.stdout)["urn"] == SHARE
    assert seen[0][2] == {"confirm": True, "headed": False} and seen[0][1][2] == "kevnk"


def test_post_dry_run_as_the_wrong_account_warns_loudly_and_exits_4(calls, png):
    _, answers = calls
    answers["post"] = post_answer(OTHER)
    result = run("post", "create", "--text", "hi", "--image", png, "--format", "json")
    assert result.exit_code == 4 and "WARNING: AUTHOR MISMATCH" in result.stderr
    assert "--confirm will refuse to post" in result.stderr and json.loads(result.stdout)["authorMatches"] is False


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_post_confirm_as_the_wrong_account_is_refused_and_exits_4(calls, png, fmt):
    _, answers = calls
    answers["post"] = post_answer(OTHER)
    result = run("post", "create", "--text", "hi", "--image", png, "--format", fmt, "--confirm")
    assert result.exit_code == 4 and "REFUSED: Signed in as Kevin Kirchner (" in result.stderr
    assert "urn:li:member:999), but the expected author is kevnk" in result.stderr  # (stderr redacts id-like handles)
    assert "Nothing was uploaded or posted." in result.stderr


def test_post_text_format_shows_the_author_check_and_full_text(calls, png):
    _, answers = calls
    answers["post"] = post_answer()
    result = run("post", "create", "--text", "Line one\nLine two", "--image", png, "--format", "text")
    assert result.exit_code == 0
    assert "Expect: kevnk  MATCH" in result.stdout and result.stdout.rstrip().endswith("Line one\nLine two")


def send_answer(signed=KEVNK):
    def answer(target, text, expect, *, confirm, search_limit, headed):
        out = {**preview(expect, signed), "dryRun": not confirm, "to": "Boardy AI", "threadId": "2-ThreadFake==",
               "entityUrn": CONV, "text": text}
        if confirm:
            out["sent"] = out["authorMatches"]
        return out

    return answer


@pytest.mark.parametrize(("flags", "confirm"), [([], False), (["--dry-run"], False), (["--confirm", "--dry-run"], False),
                                                (["--confirm"], True), (["--yes"], True), (["--send"], True)])
def test_send_is_a_dry_run_unless_confirm_alone(calls, flags, confirm):
    seen, answers = calls
    answers["send"] = send_answer()
    result = run("messages", "send", "Boardy", "--text", "ping", *flags)
    assert result.exit_code == 0
    out = json.loads(result.stdout)  # stdout is only the JSON: notes go to stderr
    assert out["dryRun"] is (not confirm) and out.get("sent", False) is confirm
    assert seen == [("send", ("Boardy", "ping", "kevnk"), {"confirm": confirm, "search_limit": 50, "headed": False})]


def test_send_as_the_wrong_account_exits_4_dry_run_or_not(calls):
    _, answers = calls
    answers["send"] = send_answer(OTHER)
    dry = run("messages", "send", "Boardy", "--text", "ping")
    assert dry.exit_code == 4 and "WARNING: AUTHOR MISMATCH" in dry.stderr
    refused = run("messages", "send", "Boardy", "--text", "ping", "--confirm")
    assert refused.exit_code == 4 and "REFUSED" in refused.stderr and "Nothing was sent." in refused.stderr
    assert json.loads(refused.stdout)["sent"] is False


def test_a_recheck_refusal_from_the_site_exits_4(calls, png):
    _, answers = calls

    def refused(*_args, **_kwargs):
        raise AuthRequired("REFUSED: the signed-in account changed before the write. Nothing was posted.")

    answers["post"] = refused
    result = run("post", "create", "--text", "hi", "--image", png, "--confirm")
    assert result.exit_code == 4 and "REFUSED" in result.stderr


def test_expect_author_flag_env_and_config_reach_the_site(calls, png, monkeypatch):
    seen, answers = calls
    answers["post"] = post_answer()
    run("post", "create", "--text", "hi", "--image", png, "--expect-author", "  someone-else ")
    monkeypatch.setenv("WEBCLI_LINKEDIN_EXPECT_AUTHOR", "from-env")
    run("post", "create", "--text", "hi", "--image", png)
    monkeypatch.delenv("WEBCLI_LINKEDIN_EXPECT_AUTHOR")
    config.save({"sites": {"linkedin": {"expect_author": "from-config"}}})
    answers["send"] = send_answer()
    run("messages", "send", "x", "--text", "hi")
    assert [call[1][2] for call in seen] == ["someone-else", "from-env", "from-config"]


@pytest.mark.parametrize("args", [["post", "create", "--text", "hi", "--image", "{png}", "--expect-author", " "],
                                  ["messages", "send", "x", "--text", "hi", "--expect-author", ""]])
def test_an_empty_expected_author_is_a_usage_error_before_any_call(calls, png, args):
    seen, _ = calls
    assert run(*[a.replace("{png}", png) for a in args]).exit_code == 2 and seen == []


@pytest.mark.parametrize("args", [["post", "create", "--text", "hi", "--image", "{png}", "--confirm"],
                                  ["messages", "send", "x", "--text", "hi", "--confirm"]])
def test_a_write_with_no_author_configured_refuses_before_any_call(calls, png, args):
    seen, _ = calls
    config.save({})  # no --expect-author, no env, no configured author -> refuse
    result = run(*[a.replace("{png}", png) for a in args])
    assert result.exit_code == 2 and seen == []
    assert "No LinkedIn author is configured" in result.stderr


ROBOTS = "https://www.linkedin.com/robots.txt"


def test_status_on_a_fresh_chrome_loads_messaging_once_for_the_csrf_cookie(tab):
    site = tab(LinkedIn({ME: me_as(KEVNK)}, fresh=True))
    assert asyncio.run(fetch.status()) == {"signedIn": True, **parse.parse_me(me_as(KEVNK))}
    assert site.page.gotos == [ROBOTS, fetch.MESSAGING_URL, ROBOTS] and site.reads == [ME]
    assert site.page.policy is POLICY
    assert asyncio.run(fetch.status())["signedIn"] is True and site.page.gotos[3:] == [ROBOTS]  # warm: robots only


def test_a_post_dry_run_on_a_fresh_chrome_lands_the_same_way_and_writes_nothing(tab, png):
    site = tab(publishing())
    site.csrf = False
    out = asyncio.run(fetch.post("Shipping today.", png, "kevnk"))
    assert out["authorMatches"] is True and site.sent == [] and site.reads == [ME]
    assert site.page.gotos == [ROBOTS, fetch.MESSAGING_URL, ROBOTS]


def test_status_signed_out_on_a_fresh_chrome_is_signed_out_without_calling_me(tab):
    site = tab(LinkedIn({ME: me_as(KEVNK)}, fresh=True,
                        redirects={fetch.MESSAGING_URL: "https://www.linkedin.com/uas/login?session_redirect=x"}))
    assert asyncio.run(fetch.status()) == {"signedIn": False} and site.reads == []


@pytest.mark.parametrize(("site", "code"), [
    (lambda: LinkedIn({ME: me_as(KEVNK)}, fresh=True), 0),
    (lambda: LinkedIn({ME: 401}), 4),
    (lambda: LinkedIn({}, fresh=True, redirects={fetch.MESSAGING_URL: "https://www.linkedin.com/authwall?x=1"}), 4),
])
def test_the_status_command_exits_0_signed_in_and_4_signed_out(tab, monkeypatch, site, code):
    make_profile("linkedin:default")
    tab(site())

    async def call(_ident, _site, op, *args, **kwargs):  # the daemon, in-process: the real fetch op
        return await getattr(fetch, op)(*args, **kwargs)

    monkeypatch.setattr(daemon, "call", call)
    result = run("status", "--format", "json")
    assert result.exit_code == code and json.loads(result.stdout)["signedIn"] is (code == 0)
    assert ("Not signed in to LinkedIn." in result.stderr) is (code == 4)


def test_status_says_whether_writes_would_be_allowed(calls):
    _, answers = calls
    answers["status"] = {"signedIn": True, **parse.parse_me(me_as(OTHER))}
    out = json.loads(run("status", "--format", "json").stdout)
    assert (out["expectedAuthor"], out["authorMatches"]) == ("kevnk", False)


def test_expected_author_resolution_order(monkeypatch):
    assert authors.expected() == "kevnk"
    config.save({"sites": {"linkedin": {"expect_author": "from-config"}}})
    assert authors.expected() == "from-config"
    monkeypatch.setenv("WEBCLI_LINKEDIN_EXPECT_AUTHOR", "from-env")
    assert authors.expected() == "from-env"
    assert authors.expected(" flag ") == "flag"
    for bad in ("", "  "):
        with pytest.raises(ValueError):
            authors.expected(bad)
    config.save({"sites": {"linkedin": {"expect_author": 7}}})
    monkeypatch.delenv("WEBCLI_LINKEDIN_EXPECT_AUTHOR")
    with pytest.raises(ValueError):
        authors.expected()


def test_the_author_match_is_the_handle_ignoring_case_and_the_recheck_is_the_whole_account():
    kevnk, other = parse.parse_me(me_as(KEVNK)), parse.parse_me(me_as(OTHER))
    assert authors.matches(kevnk, "KEVNK") and not authors.matches(other, "kevnk")
    assert authors.same_account(kevnk, {**kevnk, "publicIdentifier": "KevNK"})
    assert not authors.same_account(kevnk, {**kevnk, "memberUrn": "urn:li:member:1"})


# The guard.

@pytest.mark.parametrize(("method", "url", "ok"), [
    ("GET", "https://www.linkedin.com/messaging/", True),
    ("GET", "https://www.linkedin.com/voyager/api/me", True),
    ("GET", "https://static.licdn.com/aero-v1/sc/h/x.js", True),
    ("GET", "https://media.licdn.com/dms/image/x", True),
    ("GET", "https://www.linkedin.com/campaign-manager-api/campaignManagerAccounts?q=memberBoostAccounts", True),
    ("GET", "https://www.linkedin.com/campaign-manager-api/x?action=fetchCurrentRunningSpend", False),
    ("GET", "https://www.linkedin.com/voyager/api/x?ACTION=markAllItemsAsSeen", False),
    ("GET", "https://www.linkedin.com/m/logout/", False),
    ("GET", "https://www.linkedin.com/uas/logout?session_full_logout=true", False),
    ("GET", "http://www.linkedin.com/messaging/", False),
    ("GET", "https://www.linkedin.com:8443/messaging/", False),
    ("GET", "https://linkedin.com.evil.example/x", False),
    ("GET", "https://www.google-analytics.com/collect", False),
    ("POST", "https://www.linkedin.com/li/track", False),
    ("POST", "https://www.linkedin.com/voyager/api/messaging/conversations/2-x?action=markAllItemsAsSeen", False),
    ("POST", SEND, False),
    ("POST", REGISTER, False),
    ("POST", CREATE, False),
    ("PUT", UPLOAD, False),
    ("DELETE", "https://www.linkedin.com/voyager/api/x", False),
])
def test_the_read_policy(method, url, ok):
    assert request_allowed(method, url) is ok


def message(text="ping", conv=CONV, mailbox=KEVNK["dashEntityUrn"], **extra) -> str:
    return json.dumps({"message": {"body": {"attributes": [], "text": text}, "conversationUrn": conv},
                       "mailboxUrn": mailbox, **extra})


def test_a_message_scope_passes_one_send_bound_to_its_thread_sender_and_text():
    def scope(armed=True):
        return WriteScope("message", "ping", armed=armed, conversation=CONV, mailbox=KEVNK["dashEntityUrn"])

    assert not request_allowed("POST", SEND, message(), scope=WriteScope("message", "ping", armed=True))  # no thread
    assert not request_allowed("POST", SEND, message(), scope=scope(armed=False))  # the author check hasn't passed
    assert not request_allowed("POST", SEND, message(text="pong"), scope=scope())
    assert not request_allowed("POST", SEND, message(conv="urn:li:msg_conversation:other"), scope=scope())
    assert not request_allowed("POST", SEND, message(mailbox="urn:li:fsd_profile:ACoAAOtherFake"), scope=scope())
    assert not request_allowed("POST", SEND, message(recipientProfileUrns=["urn:li:fsd_profile:x"]), scope=scope())
    assert not request_allowed("POST", SEND + "&x=1", message(), scope=scope())
    assert not request_allowed("POST", REGISTER, message(), scope=scope())
    once = scope()
    assert request_allowed("POST", SEND, message(), scope=once)
    assert not request_allowed("POST", SEND, message(), scope=once)


def test_a_post_scope_binds_the_upload_and_the_create_to_what_register_returned():
    scope = WriteScope("post", "hi", size=10, filename="card.png")
    register = json.dumps({"mediaUploadType": "IMAGE_SHARING", "fileSize": 10, "filename": "card.png"})
    assert not request_allowed("POST", REGISTER, register, scope=scope)  # not armed: no slot before the author check
    scope.armed = True
    create = json.dumps({"commentaryV2": {"text": "hi"}, "media": [{"category": "IMAGE", "mediaUrn": ASSET}]})
    assert not request_allowed("POST", REGISTER, register.replace("10", "11"), scope=scope)
    assert not request_allowed("PUT", UPLOAD, scope=scope)  # before register said where
    assert not request_allowed("POST", CREATE, create, scope=scope)
    assert request_allowed("POST", REGISTER, register, scope=scope)
    assert not request_allowed("POST", REGISTER, register, scope=scope)  # once
    scope.upload_url, scope.asset = UPLOAD, ASSET
    assert not request_allowed("PUT", UPLOAD + "x", scope=scope)
    assert request_allowed("OPTIONS", UPLOAD, scope=scope)  # its CORS preflight, before the upload
    assert request_allowed("PUT", UPLOAD, scope=scope) and not request_allowed("PUT", UPLOAD, scope=scope)
    assert not request_allowed("OPTIONS", UPLOAD, scope=scope)
    assert not request_allowed("POST", CREATE, create.replace('"hi"', '"other"'), scope=scope)
    assert not request_allowed("POST", CREATE, create.replace(ASSET, "urn:li:digitalmediaAsset:x"), scope=scope)
    assert request_allowed("POST", CREATE, create, scope=scope)
    assert not request_allowed("POST", CREATE, create, scope=scope)


def test_a_scope_counts_only_on_a_page_opened_with_send():
    request = SimpleNamespace(method="POST", url=SEND, post_data=message())
    scope = WriteScope("message", "ping", armed=True, conversation=CONV, mailbox=KEVNK["dashEntityUrn"])
    assert not write_policy(scope).allowed(request, guard.Guard(send=False))
    assert not POLICY.allowed(request, guard.Guard(send=True))
    assert write_policy(scope).allowed(request, guard.Guard(send=True))
    blocked = SimpleNamespace(method="POST", url=SEND, post_data=message())
    assert POLICY.describe(blocked) == ("POST www.linkedin.com/voyager/api/voyagerMessagingDashMessengerMessages "
                                        "action=createMessage")  # names the action, never a value


# limsg's parsers.

def test_conversations_and_thread_resolution():
    [row] = parse.conversations(THREADS)
    assert row == {"id": "2-ThreadFake==", "entityUrn": CONV, "peer": "Boardy AI", "preview": "Want an intro?",
                   "time": "2025-10-14T14:00:00Z", "unread": 2}
    assert parse.resolve_thread([row], "BOARDY") == row and parse.resolve_thread([row], "2-threadfake==") == row
    assert parse.resolve_thread([row], "nobody") is None
    with pytest.raises(NotFound, match="2 threads match"):
        parse.resolve_thread([row, {**row, "id": "2-b"}], "boardy")


def test_messages_are_oldest_first_and_label_the_signed_in_sender_me():
    data = {"included": [
        {"$type": "com.linkedin.messenger.MessagingParticipant", "entityUrn": "urn:li:p:boardy",
         "participantType": {"member": {"firstName": {"text": "Boardy"}}}},
        {"$type": "com.linkedin.messenger.Message", "entityUrn": "urn:li:msg:2", "deliveredAt": 1760450460000,
         "body": {"text": "Sure"}, "*sender": "urn:li:fsd_profile:ACoAAKevnkFake"},
        {"$type": "com.linkedin.messenger.Message", "entityUrn": "urn:li:msg:1", "deliveredAt": 1760450400000,
         "body": {"text": "Want an intro?"}, "*sender": "urn:li:p:boardy"},
    ]}
    rows = parse.parse_messages_payload(data, self_urn="urn:li:fsd_profile:ACoAAKevnkFake")
    assert [(r["id"], r["from"], r["text"]) for r in rows] == [("urn:li:msg:1", "Boardy", "Want an intro?"),
                                                              ("urn:li:msg:2", "me", "Sure")]


def test_parse_me_takes_the_profile_the_pointer_names_and_needs_one():
    assert parse.parse_me(me_as(KEVNK, OTHER))["publicIdentifier"] == "kevnk"
    assert parse.parse_me({"included": [KEVNK, OTHER]}) is None  # no pointer, two profiles
    assert parse.parse_me(me_as({**KEVNK, "objectUrn": None})) is None  # no member urn to re-check against


def test_created_urn_comes_only_from_status_value_or_the_restli_header():
    assert parse.parse_created_urn({"data": {"status": {"urn": SHARE}}}) == SHARE
    assert parse.parse_created_urn(None, "urn%3Ali%3Ashare%3A42") == "urn:li:share:42"
    assert parse.parse_created_urn({"included": [{"urn": SHARE}]}) is None


def test_post_inputs():
    assert parse.load_text(" hi ", None) == "hi" and parse.utf16_len("a😀") == 3
    for args in ((None, None), ("a", "b"), ("  ", None), ("x" * 3001, None)):
        with pytest.raises(ValueError):
            parse.load_text(*args)


def test_query_ids_come_from_the_pages_own_messaging_requests(tab):
    qid = "messengerConversations.00112233445566778899aabbccddeeff"
    site = tab(LinkedIn({ME: me_as(KEVNK), GQL: THREADS}))
    site.page.requests = {"https://www.linkedin.com/messaging/": [f"{GQL}?queryId={qid}&variables=(x)",
                                                                  "https://www.linkedin.com/voyager/api/x?queryId=y.1"]}
    rows = asyncio.run(fetch.conversations(5))
    assert [r["peer"] for r in rows] == ["Boardy AI"] and site.page.options.get("send") is None
    assert site.reads[1].startswith(f"{GQL}?queryId={qid}&variables=(mailboxUrn:urn%3Ali%3Afsd_profile%3AACoAAKevnkFake)")
    assert fetch.cached_query_ids()["messengerConversations"] == qid  # kept for `send`, which opens no app page


def test_a_queryid_the_page_used_beats_limsgs_default():
    by_conversation = "messengerMessagesByConversation.00112233445566778899aabbccddeeff"
    assert fetch.query_id_for({}, "messengerMessages") == "messengerMessages.5846eeb71c981f11e0134cb6626cc314"
    assert fetch.query_id_for({"messengerMessagesByConversation": by_conversation}, "messengerMessages",
                              "messengerMessagesByConversation") == by_conversation


SYNC_QID = "messengerMessages.5846eeb71c981f11e0134cb6626cc314"
ANCHOR_QID = "messengerMessages.d8ea76885a52fd5dc5c317078ab7c977"
CONV_Q = "urn%3Ali%3Amsg_conversation%3A%28urn%3Ali%3Afsd_profile%3AACoAAKevnkFake%2C2-ThreadFake%3D%3D%29"
# Messenger GraphQL as LinkedIn answers it now (2026-10-21): the conversation's messages are Message URNs.
THREADS_REFS = {"included": [
    THREADS["included"][0],
    {"$type": "com.linkedin.messenger.Message", "entityUrn": "urn:li:msg_message:(x,1)", "deliveredAt": 1760450400000,
     "body": {"text": "Want an intro?"}, "*sender": "urn:li:msg_messagingParticipant:boardy"},
    {"$type": "com.linkedin.messenger.Conversation", "entityUrn": CONV, "lastActivityAt": 1760450400000,
     "unreadCount": 0, "*conversationParticipants": ["urn:li:msg_messagingParticipant:boardy"],
     "messages": {"*elements": ["urn:li:msg_message:(x,1)"]}},
]}


def test_list_previews_resolve_the_conversations_message_refs():
    [row] = parse.conversations(THREADS_REFS)
    assert row["preview"] == "Want an intro?" and row["peer"] == "Boardy AI"


def test_the_anchor_messages_query_is_kept_apart_from_the_sync_token_one():
    sync = f"{GQL}?queryId={SYNC_QID}&variables=(conversationUrn:{CONV_Q})"
    anchor = f"{GQL}?queryId={ANCHOR_QID}&variables=(deliveredAt:1,conversationUrn:{CONV_Q},countBefore:20,countAfter:0)"
    assert parse.query_key(sync) == ("messengerMessages", SYNC_QID)
    assert parse.query_key(anchor) == ("messengerMessagesByAnchor", ANCHOR_QID)
    assert parse.query_key("https://www.linkedin.com/voyager/api/x?queryId=y.1") is None


def test_message_urls_try_anchor_then_sync_and_never_the_retired_rest_events():
    # 0.5.1 cached the anchor id under messengerMessages: it is still tried, with the anchor variables, last.
    urls = fetch._message_urls({"messengerMessages": ANCHOR_QID}, CONV, 5, now_ms=99)
    assert urls == [f"{GQL}?queryId={ANCHOR_QID}&variables=(conversationUrn:{CONV_Q})",
                    f"{GQL}?queryId={SYNC_QID}&variables=(conversationUrn:{CONV_Q})",
                    f"{GQL}?queryId={ANCHOR_QID}&variables=(deliveredAt:99,conversationUrn:{CONV_Q},countBefore:20,countAfter:0)",
                    f"{GQL}?queryId={SYNC_QID}&variables=(deliveredAt:99,conversationUrn:{CONV_Q},countBefore:20,countAfter:0)"]
    urls = fetch._message_urls({"messengerMessages": SYNC_QID, "messengerMessagesByAnchor": ANCHOR_QID}, CONV, 50, 7)
    assert urls[0] == f"{GQL}?queryId={ANCHOR_QID}&variables=(deliveredAt:7,conversationUrn:{CONV_Q},countBefore:50,countAfter:0)"
    assert urls[1] == f"{GQL}?queryId={SYNC_QID}&variables=(conversationUrn:{CONV_Q})"
    assert not any("/events" in u for u in urls)


def test_history_by_thread_id_reads_the_anchor_query_the_thread_page_used(tab):
    msg = {"included": [THREADS["included"][0], {
        "$type": "com.linkedin.messenger.Message", "entityUrn": "urn:li:msg_message:(x,1)", "deliveredAt": 1760450400000,
        "body": {"text": "Want an intro?"}, "*sender": "urn:li:msg_messagingParticipant:boardy"}]}
    site = tab(LinkedIn({ME: me_as(KEVNK), f"{GQL}?queryId=messengerConversations": THREADS_REFS,
                         f"{GQL}?queryId={ANCHOR_QID}&variables=(deliveredAt:": msg}))
    thread_page = "https://www.linkedin.com/messaging/thread/2-ThreadFake%3D%3D/"
    site.page.requests = {thread_page: [
        f"{GQL}?queryId={SYNC_QID}&variables=(conversationUrn:{CONV_Q})",
        f"{GQL}?queryId={ANCHOR_QID}&variables=(deliveredAt:1,conversationUrn:{CONV_Q},countBefore:20,countAfter:0)"]}
    rows = asyncio.run(fetch.messages("2-ThreadFake==", 10))
    assert [(r["from"], r["time"]) for r in rows] == [("Boardy AI", "2025-10-14T14:00:00Z")]
    assert site.reads[-1].startswith(f"{GQL}?queryId={ANCHOR_QID}&variables=(deliveredAt:")
    assert f"conversationUrn:{CONV_Q},countBefore:20,countAfter:0)" in site.reads[-1]
    assert not any("/events" in u for u in site.reads) and site.page.options.get("send") is None
    assert fetch.cached_query_ids()["messengerMessagesByAnchor"] == ANCHOR_QID


def test_a_signed_out_session_is_auth_required_and_status_says_signed_out(tab):
    tab(LinkedIn({ME: 401}))
    with pytest.raises(AuthRequired):
        asyncio.run(fetch.conversations(5))
    assert asyncio.run(fetch.status()) == {"signedIn": False}


# Boost (limsg's record logic).

def test_boost_analytics_query_and_targets():
    from datetime import date

    assert boost.analytics_path([910747076], date(2026, 10, 8), date(2026, 10, 9)).startswith(
        "campaignManagerAdAnalytics?q=statistics&pivots=List(CAMPAIGN)&timeGranularity=ALL&dateRange=(start:(day:8,"
        "month:10,year:2026),end:(day:9,month:10,year:2026))&campaignIds=List(910747076)&fields=baseMetrics:(")
    assert boost.parse_target("urn:li:sponsoredCampaign:910747076") == ("campaign", 910747076)
    assert boost.parse_target("https://www.linkedin.com/feed/update/urn:li:activity:42/") == ("urn",
                                                                                              "urn:li:activity:42")
    assert boost.read_path("campaignManagerCampaigns/1") and not boost.read_path("x?Action=billingConfiguration")
    assert not boost.read_path("https://evil.example/x") and not boost.read_path("../voyager/api/me")


def test_boost_zero_rows_report_zero_and_null_ratios():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    campaign = {"id": 1, "status": "ACTIVE", "runSchedule": {"start": 1759881600000},
                "totalBudget": {"amount": "50", "currencyCode": "USD"}}
    record = boost.boost_record(campaign, [], (200, {"elements": []}), account=None, date_range=None, now=now)
    assert (record["spend"], record["impressions"], record["ctr"], record["metricsSource"]) == (
        0.0, 0, None, "adAnalytics:no-delivery-rows")
    failed = boost.boost_record(campaign, [], (500, None), account=None, date_range=None, now=now)
    assert failed["spend"] is None and failed["warnings"] == ["adAnalytics HTTP 500"]
