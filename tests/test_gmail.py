"""webcli gmail, offline: the strict guard and its one-write scope, the filter rows and create plan, the filters
page read off a scripted tab, and the commands over a faked daemon. Writes are dry runs unless --confirm; the
confirmed paths here run against scripted pages only. Nothing reaches Google; no Chrome starts.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from click.testing import CliRunner

from webcli.apps import webcli as webcli_app
from webcli.core import browser, daemon, guard
from webcli.core.errors import AuthRequired, NotFound, UsageError
from webcli.sites.gmail import fetch, parse
from webcli.sites.gmail.guard import POLICY, WriteScope, request_allowed, write_policy

ACTION = "https://mail.google.com/mail/u/0/"
RAW = [{"id": "z0000001789053538951*2119477363643978977", "criteria": "from:(alerts@example.com)",
        "actionText": "Matches: from:(alerts@example.com)Do this: Skip Inbox, Mark as read"},
       {"id": "1234567890123", "criteria": "has:attachment invoice", "actionText": "Do this: Star it"}]
ROWS = [parse.normalize(r) for r in RAW]


class Request:
    def __init__(self, method: str, url: str, post_data: str | None = None) -> None:
        self.method, self.url, self.post_data = method, url, post_data


def action(act: str, body: dict | None = None, *, path: str = ACTION) -> Request:
    query = urlencode({"ui": "2", "ik": "abc", "at": "AF6bupFAKE", "view": "up", "act": act, "rt": "c"})
    return Request("POST", f"{path}?{query}", urlencode(body or {}))


# The guard.

@pytest.mark.parametrize(("method", "url"), [
    ("GET", "https://mail.google.com/mail/u/0/"),
    ("GET", "https://mail.google.com/mail/u/0/data?zx=1"),
    ("GET", "https://mail.google.com/_/scs/mail-static/_/js/k=gmail.main.en/x.js"),
    ("GET", "https://ssl.gstatic.com/ui/v1/icons/mail/x.png"),
    ("POST", "https://accounts.google.com/RotateCookies"),
])
def test_reads_pass(method, url):
    assert request_allowed(method, url)


@pytest.mark.parametrize(("method", "url", "body"), [
    ("GET", "https://mail.google.com/mail/u/0/?ui=2&ik=abc&act=tr&t=123", None),  # an action on a GET
    ("GET", "https://mail.google.com/mail/u/0/?ui=2&at=AF6bupFAKE&view=up", None),
    ("GET", "https://mail.google.com/mail/u/0/?logout&hl=en", None),
    ("GET", "https://accounts.google.com/Logout", None),
    ("POST", "https://mail.google.com/sync/u/0/i/s?hl=en&c=1", "[[1,null,null,0]]"),
    ("POST", "https://mail.google.com/sync/u/0/i/fd?hl=en&c=2", "[]"),
    ("POST", "https://mail.google.com/mail/u/0/logstreamz", "impressionId=1"),
    ("POST", "https://mail.google.com/mail/u/0/waa", "f.req=[]"),
    ("POST", "https://mail.google.com/mail/u/0/?ui=2&ik=abc&view=tl", ""),  # a POST with no action: still blocked
    ("POST", action("cf").url, action("cf").post_data),  # a write, no scope
    ("POST", action("df").url, action("df").post_data),
    ("POST", "https://appsgenaiserver-pa.clients6.google.com/v1/genai/getSettings", "{}"),
    ("POST", "https://play.google.com/log?format=json", "[]"),
    ("GET", "https://www.google-analytics.com/collect", None),
    ("DELETE", "https://mail.google.com/mail/u/0/", None),
])
def test_everything_else_is_blocked(method, url, body):
    assert not request_allowed(method, url, body)


def test_a_create_scope_passes_exactly_one_matching_write():
    scope = WriteScope("create", ("alerts@example.com",))
    good = action("cf", {"cf1_from": "alerts@example.com", "cf2_ar": "true"})
    assert not request_allowed("POST", good.url, good.post_data, scope=WriteScope("delete", ("x",)))
    assert not request_allowed("POST", *vars_of(action("df", {"cf1_from": "alerts@example.com"})), scope=scope)
    assert not request_allowed("POST", *vars_of(action("cf", {"cf1_from": "other@example.com"})), scope=scope)
    assert not request_allowed("POST", *vars_of(action("cf", {"cf1_from": "alerts@example.com"},
                                                       path="https://mail.google.com/mail/u/1/")), scope=scope)
    assert not request_allowed("POST", good.url.replace("https", "http"), good.post_data, scope=scope)
    two_acts = Request("POST", good.url, good.post_data + "&act=df")
    assert not request_allowed("POST", two_acts.url, two_acts.post_data, scope=scope)
    assert not scope.used
    assert request_allowed("POST", good.url, good.post_data, scope=scope) and scope.used
    assert not request_allowed("POST", good.url, good.post_data, scope=scope)  # one write per command


def vars_of(request: Request) -> tuple[str, str | None]:
    return request.url, request.post_data


def test_a_delete_scope_needs_its_filter_id_as_a_whole_value():
    scope = WriteScope("delete", ("123456",))
    for other in ("9999999999999", "91234567", "1234567", "123456,999", "x123456"):
        assert not request_allowed("POST", *vars_of(action("df", {"tfi": other})), scope=scope), other
    assert request_allowed("POST", *vars_of(action("df", {"tfi": "123456"})), scope=scope)


def test_a_create_scope_needs_each_criterion_as_a_whole_value():
    scope = WriteScope("create", ("a@example.com",))
    assert not request_allowed("POST", *vars_of(action("cf", {"cf1_from": "ba@example.com"})), scope=scope)
    assert not request_allowed("POST", *vars_of(action("cf", {"cf1_from": "a@example.com.evil"})), scope=scope)
    assert request_allowed("POST", *vars_of(action("cf", {"cf1_from": "a@example.com"})), scope=scope)


def check(policy, request, *, send: bool) -> bool:
    guard.state.begin(policy, send=send)
    try:
        return guard.check(request, own_page=True)
    finally:
        guard.state.end()


def test_a_scope_counts_only_on_a_page_opened_with_send():
    write = action("cf", {"cf1_from": "alerts@example.com"})
    assert not check(write_policy(WriteScope("create", ("alerts@example.com",))), write, send=False)
    assert guard.state.blocked == ["POST mail.google.com/mail/u/0/ act=cf"]  # names the act, never the token
    assert not check(POLICY, write, send=True)
    assert check(write_policy(WriteScope("create", ("alerts@example.com",))), write, send=True)


# Rows and the create plan.

def test_rows_from_the_settings_table():
    assert ROWS[0] == {"id": "z0000001789053538951*2119477363643978977", "matches": "from:(alerts@example.com)",
                       "do_this": "Skip Inbox, Mark as read", "from": "(alerts@example.com)", "to": "",
                       "subject": "", "query": "", "hasAttachment": None, "criteria": "from:(alerts@example.com)"}
    assert (ROWS[1]["hasAttachment"], ROWS[1]["query"]) == (True, "invoice")


def test_find_matches_the_exact_id_only():
    assert parse.find(ROWS, "1234567890123") is ROWS[1]
    assert parse.find(ROWS, " 1234567890123 ") is ROWS[1]
    for fragment in ("123", "1234567890", "2119477363643978977", "z000", ""):
        assert parse.find(ROWS, fragment) is None


def test_create_plans_need_a_criterion_and_an_action():
    plan = parse.create_plan({"from": "a@example.com", "to": "", "subject": None, "hasAttachment": False},
                             ["star", "skipInbox", "bogus"])
    assert plan == {"criteria": {"from": "a@example.com"}, "actions": ["skipInbox", "star"]}
    assert parse.needles(plan) == ("a@example.com",)
    assert parse.needles(parse.create_plan({"hasAttachment": True}, ["star"])) == ()
    with pytest.raises(ValueError, match="criterion"):
        parse.create_plan({"to": " "}, ["star"])
    with pytest.raises(ValueError, match="action"):
        parse.create_plan({"from": "a@example.com"}, [])


# The filters page, scripted.

class FiltersPage:
    """Gmail's Settings > Filters: LIST_JS answers `tables` in turn (one per read), and every click is recorded."""

    def __init__(self, *tables, landed: str | None = None, delete_clicks: bool = True) -> None:
        self.tables, self.landed, self.delete_clicks = list(tables), landed, delete_clicks
        self.url, self.clicked, self.typed = "about:blank", [], []
        self.mouse = SimpleNamespace(click=self._mouse)
        self.keyboard = SimpleNamespace(type=self._type)

    async def goto(self, url, **_options):
        self.url = self.landed or url

    async def wait_for_timeout(self, _ms):
        pass

    async def evaluate(self, script: str, arg=None):
        if "span.sA" in script:
            return len(self.tables[0])
        if "create a new filter" in script:
            return True
        if "table.cf tr.r7')).map" in script:
            return self.tables.pop(0) if len(self.tables) > 1 else self.tables[0]
        if "dele.click()" in script:
            self.clicked.append(("delete", arg))
            return self.delete_clicks
        if "input.focus()" in script:
            return {"x": 10, "y": 20}
        if "innerText || '').trim().toLowerCase() === text.toLowerCase())" in script:
            return True  # the dialog shows the label
        if "(e.innerText || e.value || '').trim() === text" in script:
            self.clicked.append(("dom", arg))
            return True
        if "document.title" in script:
            return "kevin@example.com"
        raise AssertionError(f"unscripted evaluate: {script[:60]!r}")

    def get_by_text(self, text: str, exact: bool = False):
        async def click(**_options):
            self.clicked.append(("text", text))

        target = SimpleNamespace(click=click)
        return SimpleNamespace(first=target, last=target)

    async def _mouse(self, x, y):
        pass

    async def _type(self, text, **_options):
        self.typed.append(text)


@pytest.fixture
def pages(monkeypatch):
    opened = []

    def install(page):
        @asynccontextmanager
        async def site_page(policy, *, send=False, **_options):
            opened.append((policy, send))
            yield page

        monkeypatch.setattr(browser, "site_page", site_page)
        return page

    return opened, install


def test_filters_reads_the_table_with_the_read_policy(pages):
    opened, install = pages
    install(FiltersPage(RAW))
    assert asyncio.run(fetch.filters()) == ROWS
    assert opened == [(POLICY, False)]
    install(FiltersPage(RAW, landed="https://accounts.google.com/v3/signin/identifier"))
    with pytest.raises(AuthRequired):
        asyncio.run(fetch.filters())
    assert asyncio.run(fetch.status()) == {"signedIn": False, "email": None}


def test_the_writes_refuse_without_confirm_and_open_no_page(pages):
    opened, install = pages
    install(FiltersPage(RAW))
    with pytest.raises(UsageError):
        asyncio.run(fetch.create_filter({"criteria": {"from": "a@example.com"}, "actions": ["star"]}))
    with pytest.raises(UsageError):
        asyncio.run(fetch.delete_filter("1234567890123"))
    assert opened == []


def test_delete_confirm_clicks_only_the_exact_row_and_checks_it_is_gone(pages):
    opened, install = pages
    page = install(FiltersPage(RAW, [RAW[0]]))  # before, after
    result = asyncio.run(fetch.delete_filter("1234567890123", confirm=True))
    assert result == {"deleted": "1234567890123", "filter": ROWS[1]}
    assert ("delete", "1234567890123") in page.clicked and ("text", "OK") in page.clicked
    policy, send = opened[0]
    assert send is True and policy is not POLICY


def test_delete_confirm_of_an_unknown_or_partial_id_deletes_nothing(pages):
    _, install = pages
    page = install(FiltersPage(RAW))
    with pytest.raises(NotFound):
        asyncio.run(fetch.delete_filter("123456", confirm=True))
    assert page.clicked == []


def test_a_filter_still_listed_after_delete_is_an_error(pages):
    _, install = pages
    install(FiltersPage(RAW, RAW, RAW))
    with pytest.raises(RuntimeError, match="still listed"):
        asyncio.run(fetch.delete_filter("1234567890123", confirm=True))


def test_create_confirm_fills_the_dialog_and_returns_the_new_row(pages):
    opened, install = pages
    new = {"id": "z0000001790000000000*1", "criteria": "from:(new@example.com)", "actionText": "Do this: Star it"}
    page = install(FiltersPage(RAW, [*RAW, new]))  # before, after
    plan = parse.create_plan({"from": "new@example.com"}, ["star"])
    assert asyncio.run(fetch.create_filter(plan, confirm=True)) == parse.normalize(new)
    assert page.typed == ["new@example.com"]
    assert [c for c in page.clicked if c[0] == "text"] == [
        ("text", "Create a new filter"), ("text", "Create filter"), ("text", "Star it"), ("text", "Create filter")]
    assert opened[0][1] is True


def test_create_that_never_shows_up_is_an_error_naming_what_the_guard_blocked(pages):
    _, install = pages
    install(FiltersPage(RAW))
    guard.state.blocked.append("POST mail.google.com/mail/u/0/ act=xf")
    with pytest.raises(RuntimeError, match=r"didn't appear.*act=xf"):
        asyncio.run(fetch.create_filter(parse.create_plan({"from": "x@example.com"}, ["star"]), confirm=True))


# The commands, over a faked daemon.

@pytest.fixture
def calls(monkeypatch):
    seen, answers = [], {"filters": ROWS, "status": {"signedIn": True, "email": "kevin@example.com"}}

    async def call(ident, site, op, *args, **kwargs):
        seen.append((op, args, kwargs))
        value = answers.get(op)
        return value(*args, **kwargs) if callable(value) else value

    monkeypatch.setattr(daemon, "call", call)
    return seen, answers


def run(*args):
    return CliRunner().invoke(webcli_app.build(), ["gmail", *args])


def test_create_is_an_offline_dry_run_by_default(calls, fake_playwright):
    seen, _ = calls
    result = run("filters", "create", "--from", "alerts@example.com", "--skip-inbox", "--mark-as-read")
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "dryRun": True, "action": "create",
        "filter": {"criteria": {"from": "alerts@example.com"}, "actions": ["skipInbox", "markAsRead"]},
        "hint": "Nothing was written. Re-run with --confirm to write."}
    assert seen == [] and fake_playwright == []  # no daemon call, no Chrome
    assert run("filters", "create", "--from", "a@example.com", "--star", "--dry-run").exit_code == 0
    assert seen == []


def test_only_confirm_creates(calls):
    seen, answers = calls
    answers["create_filter"] = lambda plan, **_: {**ROWS[0], "id": "new"}
    result = run("filters", "create", "--from", "alerts@example.com", "--skip-inbox", "--confirm")
    assert result.exit_code == 0 and json.loads(result.stdout)["id"] == "new"
    assert seen == [("create_filter", ({"criteria": {"from": "alerts@example.com"}, "actions": ["skipInbox"]},),
                     {"confirm": True, "headed": False})]


def test_delete_dry_run_only_reads(calls):
    seen, _ = calls
    result = run("filters", "delete", "1234567890123")
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"dryRun": True, "action": "delete", "filter": ROWS[1],
                                         "hint": "Nothing was written. Re-run with --confirm to write."}
    assert [op for op, _, _ in seen] == ["filters"]
    missing = run("filters", "delete", "12345")  # a fragment of an id matches nothing
    assert missing.exit_code == 3 and [op for op, _, _ in seen] == ["filters", "filters"]


def test_only_confirm_deletes(calls):
    seen, answers = calls
    answers["delete_filter"] = lambda filter_id, **_: {"deleted": filter_id, "filter": ROWS[1]}
    result = run("filters", "delete", "1234567890123", "--yes")
    assert result.exit_code == 0 and json.loads(result.stdout)["deleted"] == "1234567890123"
    assert seen == [("delete_filter", ("1234567890123",), {"confirm": True, "headed": False})]


@pytest.mark.parametrize("args", [
    ("filters", "create", "--from", "a@example.com", "--star", "--dry-run", "--confirm"),
    ("filters", "delete", "1234567890123", "--dry-run", "--confirm"),
    ("filters", "create", "--star"),  # no criterion
    ("filters", "create", "--from", "a@example.com"),  # no action
    ("filters", "create", "--from", "a@example.com", "--forward", "x@example.com"),  # dropped: never worked
    ("filters", "create", "--from", "a@example.com", "--add-label", "Label_1"),
])
def test_bad_writes_are_usage_errors_before_any_call(calls, args):
    assert run(*args).exit_code == 2
    assert calls[0] == []


def test_list_and_status(calls):
    seen, answers = calls
    assert json.loads(run("filters", "list").stdout) == ROWS
    assert json.loads(run("filters", "list", "--compact", "--limit", "1").stdout) == [
        {k: ROWS[0][k] for k in ("id", "matches", "do_this", "from", "query")}]
    assert run("status").exit_code == 0
    answers["status"] = {"signedIn": False, "email": None}
    assert run("status").exit_code == 4
