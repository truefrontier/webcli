"""webcli contacts, offline: the fail-closed guard and its one write scope, the Person parser, the reads and the
label writes against a fake Contacts backend, and the commands over a faked daemon.

The fake backend answers the page's batchexecute calls from memory, and every call goes through the real guard
first (the command's policy, with send on only when the command opened its page that way): a request the guard
blocks fails in the page, as a real blocked fetch does. Nothing reaches Google; no Chrome starts.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from contextlib import asynccontextmanager
from urllib.parse import urlencode

import pytest
from click.testing import CliRunner

from webcli.apps import webcli as webcli_app
from webcli.core import browser, daemon, guard
from webcli.core.errors import AuthRequired, NotFound, UsageError
from webcli.sites.contacts import export, fetch, parse
from webcli.sites.contacts.endpoints import OTHER_WRITES, RPC, WRITE_RPC
from webcli.sites.contacts.guard import POLICY, WriteScope, request_allowed, write_allowed, write_policy

BATCH = "https://contacts.google.com/_/ContactsUi/data/batchexecute"
LABELS = [["L1", "Newsletter", 2, False], ["L2", "Family friends", 0, False], ["L3", "Dupe", 1, False],
          ["L4", "dupe ", 1, False], ["friends", "Friends", 0, True]]


class Request:
    def __init__(self, method: str, url: str, post_data: str | None = None) -> None:
        self.method, self.url, self.post_data = method, url, post_data


def batch_request(rpcid: str, payload, *, url_ids: str | None = None) -> Request:
    body = urlencode({"f.req": json.dumps([[[rpcid, json.dumps(payload), None, "generic"]]]), "at": "AJvFAKEat:1"})
    return Request("POST", f"{BATCH}?{urlencode({'rpcids': url_ids or rpcid, 'rt': 'c'})}", body)


def answer(rpcid: str, payload) -> str:
    return ")]}'\n\n123\n" + json.dumps([["wrb.fr", rpcid, json.dumps(payload), None, None, None, "generic"]])


def person(cid: str, name: str, groups=(), *, saved: bool = True, email: str | None = None) -> list:
    """A Person row: id, meta (memberships at [8]), names, and optionally an email (field 10)."""
    memberships = (["myContacts"] if saved else []) + list(groups)
    row = [cid, [None] * 8 + [memberships], [[[True], name]]] + [None] * 6
    row.append([[[True], email, "home"]] if email else None)
    return row


# The guard.

@pytest.mark.parametrize(("method", "url", "body"), [
    ("GET", "https://contacts.google.com/", None),
    ("GET", "https://www.gstatic.com/_/x.js", None),
    ("POST", "https://accounts.google.com/RotateCookies", None),
    *[("POST", batch_request(rpcid, []).url, batch_request(rpcid, []).post_data) for rpcid in RPC.values()],
])
def test_listed_reads_pass(method, url, body):
    assert request_allowed(method, url, body)


@pytest.mark.parametrize(("method", "url", "body"), [
    *[("POST", batch_request(rpcid, []).url, batch_request(rpcid, []).post_data) for rpcid in OTHER_WRITES],
    ("POST", batch_request("ls96Jc", []).url, batch_request("ls96Jc", []).post_data),  # a write, no scope
    ("POST", batch_request("ouU0xd", ["x", None, None, None, 1, 0]).url,
     batch_request("ouU0xd", ["x", None, None, None, 1, 0]).post_data),
    ("POST", batch_request("GKlgB", [], url_ids="YQqBqc").url, batch_request("GKlgB", []).post_data),  # disagree
    ("POST", f"{BATCH}?rpcids=GKlgB", urlencode({"f.req": json.dumps([[["GKlgB", "[]"], ["GKlgB", "[]"]]])})),
    ("POST", f"{BATCH}?rpcids=GKlgB", urlencode({"f.req": json.dumps([[["GKlgB", "[]"]]])}) + "&f.req=[]"),
    ("POST", f"{BATCH}?rpcids=GKlgB", "not a form"),
    ("POST", BATCH.replace("https", "http") + "?rpcids=GKlgB", batch_request("GKlgB", []).post_data),
    ("POST", BATCH.replace(".com/", ".com:8443/") + "?rpcids=GKlgB", batch_request("GKlgB", []).post_data),
    ("GET", "https://contacts.google.com/_/ContactsUi/data/batchexecute?rpcids=GKlgB", None),
    ("POST", "https://people-pa.clients6.google.com/v2/people:batchGet", "{}"),
    ("POST", "https://peoplestack-pa.clients6.google.com/$rpc/peoplestack.PeopleStackAutocompleteService/Warmup", ""),
    ("POST", "https://play.google.com/log?format=json", "[]"),
    ("POST", "https://contacts.google.com/_/ContactsUi/jserror", "x"),
    ("GET", "https://accounts.google.com/Logout", None),
    ("GET", "https://www.google-analytics.com/collect", None),
    ("DELETE", "https://contacts.google.com/", None),
])
def test_writes_unknown_rpcids_and_other_hosts_are_blocked(method, url, body):
    assert not request_allowed(method, url, body)


def scoped(**kwargs) -> WriteScope:
    return WriteScope(label_id="L1", action="add", keys=frozenset({"7b", "1c8"}), **kwargs)


def membership(keys, add=("L1",), remove=None, people=None, flags=(1, 0)):
    return [list(keys), people, list(add) if add else None, list(remove) if remove else None, None, None, *flags]


@pytest.mark.parametrize(("payload", "ok"), [
    (membership(["7b"]), True),
    (membership(["7b", "1c8"]), True),
    (membership(["7b"], add=("L2",)), False),  # another label
    (membership(["7b"], add=None, remove=("L1",)), False),  # the other direction
    (membership(["7b"], add=("L1",), remove=("L1",)), False),
    (membership(["7b", "ff"]), False),  # a contact the selector didn't pick
    (membership(["7B"]), False),  # not lowercase hex
    (membership([]), False),
    (membership(["7b"], people=[["c123"]]), False),  # extra fields
    (membership(["7b"], flags=(1, 1)), False),
    (membership(["7b"])[:7], False),
    ("not a list", False),
])
def test_a_membership_write_passes_only_inside_its_scope(payload, ok):
    assert write_allowed("ls96Jc", json.dumps(payload), scoped()) is ok
    assert write_allowed("ls96Jc", json.dumps(payload), None) is False


def test_more_than_1000_keys_is_refused():
    keys = [format(i, "x") for i in range(1, 1002)]
    assert not write_allowed("ls96Jc", json.dumps(membership(keys)),
                             WriteScope(label_id="L1", action="add", keys=frozenset(keys)))


def test_a_label_create_passes_once_under_exactly_its_name():
    scope = WriteScope(create_name="New label")
    assert not write_allowed("ouU0xd", json.dumps(["Other", None, None, None, 1, 0]), scope)
    assert not write_allowed("ouU0xd", json.dumps(["New label", None, [["7b"]], None, 1, 0]), scope)  # with members
    assert write_allowed("ouU0xd", json.dumps(["New label", None, None, None, 1, 0]), scope)
    assert scope.create_name is None
    assert not write_allowed("ouU0xd", json.dumps(["New label", None, None, None, 1, 0]), scope)  # used up


def check(policy, request, *, send: bool) -> bool:
    guard.state.begin(policy, send=send)
    try:
        return guard.check(request, own_page=True)
    finally:
        guard.state.end()


def test_a_scope_counts_only_on_a_page_opened_with_send():
    write = batch_request("ls96Jc", membership(["7b"]))
    assert check(write_policy(scoped()), write, send=True)
    assert not check(write_policy(scoped()), write, send=False)  # a dry run's page
    assert guard.state.blocked == ["POST contacts.google.com/_/ContactsUi/data/batchexecute rpcids=ls96Jc"]
    assert not check(POLICY, write, send=True)  # no scope
    guard.state.begin(write_policy(scoped()), send=True)
    guard.state.end()
    assert not guard.check(write, own_page=True)  # the command ended: nothing passes


def test_a_batch_with_one_bad_entry_is_blocked_whole():
    body = urlencode({"f.req": json.dumps([[["ls96Jc", json.dumps(membership(["7b"])), None, "generic"],
                                            ["Loo3me", json.dumps([["L1"], 1]), None, "generic"]]])})
    assert not request_allowed("POST", f"{BATCH}?rpcids=ls96Jc,Loo3me", body, scope=scoped())


# The parser.

def test_a_person_row_becomes_a_contact():
    row = person("c123", "Ada Lovelace", ["L1"], email="ada@example.com")
    c = parse.contact(row, parse.group_names(parse.groups([LABELS])))
    assert (c["id"], c["name"], c["source"], c["labels"], c["labelIds"]) == (
        "c123", "Ada Lovelace", "contacts", ["Newsletter"], ["L1"])
    assert c["emails"] == [{"value": "ada@example.com", "type": "home"}] and c["resourceName"] == "people/c123"
    assert parse.row(c) == {"id": "c123", "name": "Ada Lovelace", "email": "ada@example.com", "phone": None,
                            "org": None, "labels": ["Newsletter"], "source": "contacts"}
    assert parse.contact(person("c9", "X", saved=False))["source"] == "other"
    assert parse.is_empty(["c1", [None] * 9]) and not parse.is_empty(row)


def test_pages_groups_and_rpc_payloads():
    assert parse.contacts_page([[person("c1", "A")], None, "tok"]) == ([person("c1", "A")], "tok")
    assert parse.other_page([[person("c1", "A")], ""])[1] is None
    assert [g["name"] for g in parse.groups([LABELS])] == ["Newsletter", "Family friends", "Dupe", "dupe ", "Friends"]
    assert parse.rpc_payload(answer("GKlgB", [LABELS]), "GKlgB") == [LABELS]
    failed = ")]}'\n" + json.dumps([["wrb.fr", "GKlgB", None, None, None, [8], "generic"]])
    with pytest.raises(parse.RpcError) as exc:
        parse.rpc_payload(failed, "GKlgB")
    assert exc.value.code == 8
    with pytest.raises(parse.RpcError):
        parse.rpc_payload("<html>sign in</html>", "GKlgB")


@pytest.mark.parametrize(("value", "expected"), [
    ("c123", ("id", "c123")), ("people/c123", ("id", "c123")),
    ("https://contacts.google.com/u/0/person/c456", ("id", "c456")),
    ("ada@example.com", ("email", "ada@example.com")), ("+1 (202) 555-0100", ("phone", "+1 (202) 555-0100")),
])
def test_lookup_keys(value, expected):
    assert parse.lookup_key(value) == expected


def test_label_keys_and_payloads():
    assert parse.label_key("  Business   Pro ") == "business pro" and parse.clean_label_name(" A  b ") == "A b"
    assert parse.contact_key("c123") == "7b" and parse.contact_key("people/c456") == "1c8"
    assert parse.membership_payload(["c123"], "L1", "add") == [["7b"], None, ["L1"], None, None, None, 1, 0]
    assert parse.membership_payload(["c123"], "L1", "remove") == [["7b"], None, None, ["L1"], None, None, 1, 0]
    with pytest.raises(ValueError):
        parse.lookup_key("not a thing")
    with pytest.raises(ValueError):
        parse.membership_payload([], "L1", "add")


# The reads and the label writes, against a fake Contacts backend behind the real guard.

class FakeContacts:
    """contacts.google.com's batchexecute, in memory. `calls`: every (rpcid, payload) that reached it, in order."""

    def __init__(self, people: list[list], *, labels=None, rate_limit_at: int | None = None) -> None:
        self.people = {p[0]: p for p in people}
        self.labels = [list(g) for g in (labels or LABELS)]
        self.calls: list[tuple[str, object]] = []
        self.rate_limit_at = rate_limit_at  # this membership call (0 = the first) answers 429

    def members(self, cid: str) -> list:
        return self.people[cid][1][8]

    def handle(self, rpcid: str, payload) -> dict:
        self.calls.append((rpcid, payload))
        if rpcid == "GKlgB":
            return {"status": 200, "body": answer(rpcid, [self.labels])}
        if rpcid == "YQqBqc":
            rows = list(self.people.values())
            return {"status": 200, "body": answer(rpcid, [[r for r in rows if "myContacts" in r[1][8]]])}
        if rpcid == "fcwrWc":
            query = payload[1].lower()
            hits = [r for r in self.people.values() if query in r[2][0][1].lower()]
            mine = [r for r in hits if "myContacts" in r[1][8]]
            return {"status": 200, "body": answer(rpcid, [mine, [r for r in hits if r not in mine]])}
        if rpcid == "rptSGc":
            return {"status": 200, "body": answer(rpcid, [[self.people[c] for c in payload[0] if c in self.people]])}
        if rpcid == "ls96Jc":
            if self.rate_limit_at is not None and len(self.writes("ls96Jc")) - 1 == self.rate_limit_at:
                return {"status": 429, "body": ""}
            keys, _, add, remove = payload[:4]
            for cid in self.people:
                if parse.contact_key(cid) in keys:
                    groups = self.members(cid)
                    if add:
                        groups.append(add[0])
                    elif remove[0] in groups:
                        groups.remove(remove[0])
            return {"status": 200, "body": answer(rpcid, [])}
        if rpcid == "ouU0xd":
            self.labels.insert(0, ["L9", payload[0], 0, False])
            return {"status": 200, "body": answer(rpcid, [["L9"]])}
        raise AssertionError(f"unexpected rpc {rpcid}")

    def writes(self, rpcid: str | None = None) -> list:
        return [(r, p) for r, p in self.calls if r in ("ls96Jc", "ouU0xd") and (rpcid is None or r == rpcid)]


class ContactsPage:
    """A signed-in contacts.google.com tab whose in-page fetches go through the guard to a FakeContacts."""

    def __init__(self, backend: FakeContacts) -> None:
        self.backend, self.url = backend, "about:blank"

    async def goto(self, url, **_options):
        self.url = url

    async def wait_for_function(self, *_args, **_options):
        pass

    async def evaluate(self, script: str, arg=None):
        if "batchexecute" in script:
            request = batch_request(arg["rpcid"], arg["payload"])
            if not guard.check(request, own_page=True):
                self.backend.calls.append(("BLOCKED", arg["rpcid"]))
                return {"status": -1, "body": "TypeError"}  # a fetch the guard aborted
            await asyncio.sleep(0)
            return self.backend.handle(arg["rpcid"], arg["payload"])
        if "cfb2h" in script:
            return "boq_contactsuiserver_20261010.00_p0"
        if "oPEP7c" in script:
            return "kevin@example.com"
        raise AssertionError(f"unscripted evaluate: {script[:60]!r}")


@pytest.fixture
def backend(monkeypatch):
    """Install a FakeContacts behind browser.site_page; `opened` records each page's (policy, send)."""
    state = {"backend": FakeContacts([]), "opened": []}

    @asynccontextmanager
    async def site_page(policy, *, send=False, **_options):
        state["opened"].append((policy, send))
        guard.state.begin(policy, send=send)
        try:
            yield ContactsPage(state["backend"])
        finally:
            guard.state.end()

    monkeypatch.setattr(browser, "site_page", site_page)
    monkeypatch.setattr(fetch, "BATCH_PAUSE", (0, 0))
    return state


def use(state, people, **kwargs) -> FakeContacts:
    state["backend"] = FakeContacts(people, **kwargs)
    return state["backend"]


def test_reads_never_write_and_use_the_read_policy(backend):
    fake = use(backend, [person("c1", "Ada", ["L1"]), person("c2", "Bob"), person("c3", "Cy", saved=False)])
    assert [c["id"] for c in asyncio.run(fetch.contacts())["contacts"]] == ["c1", "c2"]
    assert [c["id"] for c in asyncio.run(fetch.contacts(label="newsletter"))["contacts"]] == ["c1"]
    assert asyncio.run(fetch.get("c2"))["name"] == "Bob"
    assert [c["source"] for c in asyncio.run(fetch.search("c"))] == ["other"]
    assert asyncio.run(fetch.status()) == {"signedIn": True, "email": "kevin@example.com", "contactsApi": "ok"}
    assert all(policy is POLICY and send is False for policy, send in backend["opened"])
    assert fake.writes() == []
    with pytest.raises(NotFound):
        asyncio.run(fetch.get("c404"))


def test_label_add_is_a_dry_run_by_default_and_writes_nothing(backend):
    fake = use(backend, [person("c1", "Ada", ["L1"]), person("c2", "Bob"), person("c3", "Cy")])
    result = asyncio.run(fetch.label_change("add", "Newsletter", ids=["c1", "c2", "c3"]))
    assert {k: result[k] for k in ("label", "labelId", "action", "matched", "wouldChange", "changed",
                                   "alreadyLabeled", "failed", "dryRun", "createLabel")} == {
        "label": "Newsletter", "labelId": "L1", "action": "add", "matched": 3, "wouldChange": 2, "changed": 0,
        "alreadyLabeled": 1, "failed": 0, "dryRun": True, "createLabel": False}
    assert result["contacts"] == [{"id": "c1", "name": "Ada", "change": False},
                                  {"id": "c2", "name": "Bob", "change": True},
                                  {"id": "c3", "name": "Cy", "change": True}]
    assert "accepted" not in result and fake.writes() == []
    assert backend["opened"] == [(POLICY, False)]  # the dry run's page can't write
    assert fake.members("c2") == ["myContacts"]  # nothing changed


def test_label_add_confirm_batches_200_at_a_time_and_reads_back(backend):
    people = [person(f"c{1000 + i}", f"P{i}", ["L2"]) for i in range(460)]
    for p in people[:10]:
        p[1][8].append("L1")  # 10 already labeled: skipped
    fake = use(backend, people)
    result = asyncio.run(fetch.label_change("add", "newsletter ", from_label="Family friends", confirm=True))
    writes = fake.writes("ls96Jc")
    assert [len(p[0]) for _, p in writes] == [200, 200, 50]
    todo_keys = {parse.contact_key(p[0]) for p in people[10:]}
    assert all(p[2:] == [["L1"], None, None, None, 1, 0] and set(p[0]) <= todo_keys for _, p in writes)
    assert sorted(k for _, p in writes for k in p[0]) == sorted(todo_keys)  # each once
    assert (result["matched"], result["wouldChange"], result["alreadyLabeled"], result["accepted"],
            result["changed"], result["failed"], result["dryRun"]) == (460, 450, 10, 450, 450, 0, False)
    assert "stopped" not in result and ("BLOCKED", "ls96Jc") not in fake.calls
    policy, send = backend["opened"][0]
    assert send is True and policy is not POLICY
    rerun = asyncio.run(fetch.label_change("add", "Newsletter", from_label="Family friends", confirm=True))
    assert (rerun["wouldChange"], rerun["alreadyLabeled"]) == (0, 460) and len(fake.writes()) == 3  # idempotent


def test_label_remove_confirm_removes_only_the_selected(backend):
    fake = use(backend, [person("c1", "Ada", ["L1"]), person("c2", "Bob", ["L1"]), person("c3", "Cy")])
    dry = asyncio.run(fetch.label_change("remove", "L1", ids=["c1", "c3"]))
    assert (dry["wouldChange"], dry["alreadyAbsent"], dry["dryRun"]) == (1, 1, True) and fake.writes() == []
    done = asyncio.run(fetch.label_change("remove", "L1", ids=["c1", "c3"], confirm=True))
    assert fake.writes() == [("ls96Jc", [[parse.contact_key("c1")], None, None, ["L1"], None, None, 1, 0])]
    assert (done["changed"], done["alreadyAbsent"]) == (1, 1)
    assert fake.members("c1") == ["myContacts"] and "L1" in fake.members("c2")


def test_create_missing_creates_once_then_labels(backend):
    fake = use(backend, [person("c1", "Ada"), person("c2", "Bob")])
    with pytest.raises(NotFound, match="No label"):
        asyncio.run(fetch.label_change("add", "Brand New", ids=["c1"]))
    dry = asyncio.run(fetch.label_change("add", "  Brand   New ", ids=["c1"], create_missing=True))
    assert (dry["createLabel"], dry["label"], dry["labelId"], dry["wouldChange"]) == (True, "Brand New", None, 1)
    assert fake.writes() == []
    done = asyncio.run(fetch.label_change("add", "Brand New", ids=["c1", "c2"], create_missing=True, confirm=True))
    assert [r for r, _ in fake.writes()] == ["ouU0xd", "ls96Jc"]
    assert fake.writes()[0][1] == ["Brand New", None, None, None, 1, 0]
    assert (done["labelId"], done["changed"]) == ("L9", 2)


def test_system_groups_ambiguous_names_and_missing_labels(backend):
    use(backend, [person("c1", "Ada")])
    with pytest.raises(UsageError, match="system group"):
        asyncio.run(fetch.label_change("add", "Friends", ids=["c1"], confirm=True))
    with pytest.raises(UsageError, match="system group"):
        asyncio.run(fetch.label_change("add", "starred", ids=["c1"]))
    with pytest.raises(UsageError) as ambiguous:
        asyncio.run(fetch.label_change("add", "DUPE", ids=["c1"]))
    assert "L3 (Dupe)" in ambiguous.value.hint and "L4 (dupe )" in ambiguous.value.hint
    with pytest.raises(NotFound):
        asyncio.run(fetch.label_change("remove", "Nope", ids=["c1"], confirm=True))
    assert backend["backend"].writes() == []


def test_other_contacts_are_failures_or_skipped(backend):
    use(backend, [person("c1", "Ada"), person("c2", "Adam", saved=False)])
    by_ids = asyncio.run(fetch.label_change("add", "L1", ids=["c1", "c2", "c404"]))
    assert by_ids["matched"] == 1 and [f["id"] for f in by_ids["failures"]] == ["c2", "c404"]
    by_query = asyncio.run(fetch.label_change("add", "L1", query="ada"))
    assert by_query["matched"] == 1 and by_query["skippedOtherContacts"] == 1


def test_a_rate_limit_mid_run_stops_with_the_result_so_far(backend):
    fake = use(backend, [person(f"c{2000 + i}", f"P{i}", ["L2"]) for i in range(300)], rate_limit_at=1)
    result = asyncio.run(fetch.label_change("add", "L1", from_label="L2", confirm=True))
    assert result["stopped"]["code"] == 6 and "rate-limited" in result["stopped"]["error"]
    assert (result["accepted"], result["changed"], result["wouldChange"]) == (200, 200, 300)
    assert len(fake.writes("ls96Jc")) == 2  # the second call was refused; no third was sent


def test_selector_and_action_are_checked_before_any_page(backend):
    with pytest.raises(UsageError):
        asyncio.run(fetch.label_change("add", "L1", ids=["c1"], query="x"))
    with pytest.raises(UsageError):
        asyncio.run(fetch.label_change("delete", "L1", ids=["c1"]))
    assert backend["opened"] == []


def test_signed_out_is_auth_required(monkeypatch):
    class SignedOut(ContactsPage):
        async def goto(self, url, **_options):
            self.url = "https://accounts.google.com/v3/signin/identifier"

    @asynccontextmanager
    async def site_page(policy, **_options):
        yield SignedOut(FakeContacts([]))

    monkeypatch.setattr(browser, "site_page", site_page)
    with pytest.raises(AuthRequired):
        asyncio.run(fetch.labels())
    assert asyncio.run(fetch.status()) == {"signedIn": False, "email": None, "contactsApi": None}


# The commands, over a faked daemon.

CONTACT = parse.contact(person("c1", "Ada", ["L1"], email="ada@example.com"), {"L1": "Newsletter"})
DRY = {"label": "Newsletter", "labelId": "L1", "action": "add", "matched": 1, "wouldChange": 1, "changed": 0,
       "alreadyLabeled": 0, "failed": 0, "failures": [], "dryRun": True, "createLabel": False,
       "skippedOtherContacts": 0, "contacts": [{"id": "c1", "name": "Ada", "change": True}]}


@pytest.fixture
def calls(monkeypatch):
    seen, answers = [], {}

    async def call(ident, site, op, *args, **kwargs):
        seen.append((op, args, kwargs))
        value = answers.get(op)
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return value(*args, **kwargs)
        return value

    monkeypatch.setattr(daemon, "call", call)
    return seen, answers


def run(*args):
    return CliRunner().invoke(webcli_app.build(), ["contacts", *args])


def test_label_add_defaults_to_a_dry_run(calls):
    seen, answers = calls
    answers["label_change"] = lambda *a, **k: {**DRY, "dryRun": not k["confirm"]}
    result = run("label", "add", "Newsletter", "--ids", "c1")
    assert result.exit_code == 0 and json.loads(result.stdout)["dryRun"] is True
    assert seen == [("label_change", ("add", "Newsletter"), {"from_label": None, "query": None, "ids": ["c1"],
                                                               "confirm": False, "create_missing": False,
                                                               "headed": False})]
    assert run("label", "add", "Newsletter", "--ids", "c1", "--dry-run").exit_code == 0
    assert seen[-1][2]["confirm"] is False


def test_only_confirm_asks_for_the_write(calls):
    seen, answers = calls
    answers["label_change"] = lambda *a, **k: {**DRY, "dryRun": False, "changed": 1, "accepted": 1}
    for flag in ("--confirm", "--yes"):
        assert run("label", "remove", "Newsletter", "--from-label", "Old", flag).exit_code == 0
        assert seen[-1][2]["confirm"] is True and seen[-1][1] == ("remove", "Newsletter")
    assert "create_missing" in seen[-1][2] and seen[-1][2]["create_missing"] is False


@pytest.mark.parametrize("args", [
    ("label", "add", "L", "--ids", "c1", "--dry-run", "--confirm"),
    ("label", "add", "L"),  # no selector
    ("label", "add", "L", "--ids", "c1", "--query", "x"),  # two
    ("label", "remove", "L", "--from-label", "A", "--query", "x"),
    ("label", "add", "L", "--ids", "ada@example.com"),  # not an id
    ("label", "add", "  ", "--ids", "c1"),
    ("label", "remove", "L", "--ids", "c1", "--create-missing"),  # remove never creates
    ("get", "not a contact"),
])
def test_bad_label_requests_are_usage_errors_before_any_call(calls, args):
    assert run(*args).exit_code == 2
    assert calls[0] == []


def test_ids_accept_commas_repeats_and_people_prefixes(calls):
    seen, answers = calls
    answers["label_change"] = DRY
    run("label", "add", "L", "--ids", "c1,people/c2", "--ids", "c3")
    assert seen[-1][2]["ids"] == ["c1", "c2", "c3"]


def test_a_stopped_run_prints_the_result_then_exits_with_its_code(calls):
    _, answers = calls
    answers["label_change"] = {**DRY, "dryRun": False, "accepted": 200, "changed": 200, "wouldChange": 300,
                               "stopped": {"code": 6, "error": "Google rate-limited ls96Jc (HTTP 429)."}}
    result = run("label", "add", "L", "--from-label", "A", "--confirm")
    assert result.exit_code == 6 and json.loads(result.stdout)["accepted"] == 200
    assert "Rerun the same command" in json.loads(result.stderr)["hint"]
    answers["label_change"] = {**DRY, "dryRun": False, "failed": 1,
                               "failures": [{"id": "c1", "error": "reads back unchanged"}]}
    assert run("label", "add", "L", "--ids", "c1", "--confirm").exit_code == 5
    assert run("label", "add", "L", "--ids", "c1").exit_code == 0  # a dry run with failures is still a dry run


def test_usage_errors_from_the_site_survive_the_daemon_and_exit_2(calls):
    _, answers = calls
    error = UsageError("'Dupe' matches 2 labels; pass the label id instead.", hint="Candidates: L3 (Dupe), L4 (dupe)")
    answers["label_change"] = daemon.rebuild(daemon.error_payload(error))
    result = run("label", "add", "Dupe", "--ids", "c1")
    assert result.exit_code == 2 and json.loads(result.stderr)["hint"] == "Candidates: L3 (Dupe), L4 (dupe)"


def test_text_output_says_dry_run(calls):
    _, answers = calls
    answers["label_change"] = DRY
    result = run("label", "add", "Newsletter", "--ids", "c1", "--format", "table")
    assert result.stdout.startswith("DRY RUN: would add to 'Newsletter' (L1): 1 of 1 matched")
    assert "Re-run with --confirm" in result.stdout


def test_list_search_get_labels_and_status(calls):
    seen, answers = calls
    answers.update(contacts={"contacts": [CONTACT], "label": None}, search=[CONTACT], get=CONTACT,
                   labels=parse.groups([LABELS]), status={"signedIn": True, "email": None, "contactsApi": "ok"})
    assert json.loads(run("list").stdout) == [parse.row(CONTACT)]
    assert run("list", "--label", "Newsletter", "--csv").stdout.splitlines()[1] == \
        "c1,Ada,ada@example.com,,,Newsletter,contacts"
    assert seen[-1] == ("contacts", (None, "Newsletter"), {"headed": False})
    assert json.loads(run("search", "ada", "--compact").stdout) == [
        {"id": "c1", "name": "Ada", "email": "ada@example.com", "phone": None}]
    assert json.loads(run("get", "ada@example.com").stdout)["id"] == "c1"
    assert [g["id"] for g in json.loads(run("labels").stdout)] == ["L1", "L2", "L3", "L4", "friends"]
    assert run("status").exit_code == 0
    answers["status"] = {"signedIn": True, "email": None, "contactsApi": "error"}
    assert run("status").exit_code == 5
    answers["status"] = {"signedIn": False, "email": None, "contactsApi": None}
    assert run("status").exit_code == 4
    answers["search"] = []
    assert run("search", "zzz").exit_code == 3


def test_export_writes_a_private_vcard(calls, tmp_path):
    _, answers = calls
    answers["contacts"] = {"contacts": [CONTACT], "label": None}
    out = tmp_path / "all.vcf"
    result = run("export", "--format", "vcf", "--out", str(out))
    assert result.exit_code == 0 and json.loads(result.stdout) == {"path": str(out), "format": "vcf", "count": 1}
    assert out.read_bytes().startswith(b"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Ada\r\n")
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    default = json.loads(run("export", "--format", "csv").stdout)["path"]
    assert "/sites/contacts/exports/contacts-" in default and default.endswith(".csv")
    assert run("export", "--out", str(tmp_path / "missing" / "x.json")).exit_code == 2


def test_csv_export_cells_cannot_run_as_formulas():
    row = export.csv_row({**CONTACT, "name": "=HYPERLINK(1)", "phones": [{"value": "+1 202-555-0100"}]})
    assert row["name"] == "'=HYPERLINK(1)" and row["phones"] == "+1 202-555-0100"
