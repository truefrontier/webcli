"""webcli 1.0 Phase B: `webcli new`, `dev`, `test`, `lint`, and guided capture. Offline: no network, no Chrome (capture
runs against a fake page)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from webcli import scaffold, sites
from webcli.apps import webcli as webcli_app
from webcli.core import capture, config, daemon
from webcli.core.redact import REDACTED


def run(*args: str):
    return CliRunner().invoke(webcli_app.build(), list(args))


def out(result) -> dict:
    return json.loads(result.stdout)


@pytest.fixture(autouse=True)
def workdir(tmp_path, monkeypatch):
    work = tmp_path / "work"  # beside conftest's WEBCLI_HOME (tmp_path/home), not around it
    work.mkdir()
    monkeypatch.chdir(work)
    sites.reload()
    yield work
    sites.reload()  # the registry is cached per process: don't hand this test's tools to the next test
    for name in [m for m in sys.modules if m.startswith(("webcli_tool_", "_webcli_test_"))]:
        del sys.modules[name]


def make(name: str = "toyhttp", *args: str) -> Path:
    result = run("new", name, *(args or ("--url", "https://httpbin.org")))
    assert result.exit_code == 0, result.output
    return Path(out(result)["folder"])


def lint(name: str):
    result = run("lint", name, "--format", "json")
    return result.exit_code, out(result)["findings"]


def errors(findings: list[dict]) -> list[str]:
    return [f"{f['check']}: {f['message']}" for f in findings if f["level"] == "error"]


# The Definition of Done: new -> lint -> test, offline.

@pytest.mark.parametrize("args", [(), ("--url", "https://httpbin.org")])
def test_new_then_lint_then_test_pass_offline(workdir, args):
    result = run("new", "toyexample", *args, "--format", "json")
    assert result.exit_code == 0, result.output
    made = out(result)
    folder = workdir / "webcli-tool-toyexample"
    assert made["folder"] == str(folder) and made["identity"] == "toyexample:default"
    assert made["hosts"] == (["httpbin.org"] if args else ["www.example.com"])
    assert made["next"][-1].startswith("webcli submit toyexample")
    assert sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()) == [
        "README.md", "conftest.py", "tests/fixtures/page.json", "tests/test_tool.py", "webcli-tool.toml",
        "webcli_tool_toyexample/__init__.py", "webcli_tool_toyexample/cli.py", "webcli_tool_toyexample/endpoints.py",
        "webcli_tool_toyexample/fetch.py", "webcli_tool_toyexample/guard.py", "webcli_tool_toyexample/parse.py"]
    assert scaffold.dev_tools() == {"toyexample": folder}

    code, findings = lint("toyexample")
    assert code == 0 and errors(findings) == []
    expected = {"maintainers"} | ({"hosts"} if not args else set())
    assert {f["check"] for f in findings} == expected

    result = run("test", "toyexample", "--format", "json")
    assert result.exit_code == 0, result.output
    assert (out(result)["passed"], out(result)["failed"]) == (5, 0)


def test_lint_and_test_take_a_folder_path_too(workdir):
    folder = make()
    assert run("lint", str(folder), "--format", "json").exit_code == 0
    assert run("test", "./webcli-tool-toyhttp", "--format", "json").exit_code == 0


def test_dev_toml_keeps_other_tools(workdir):
    make("toyone")
    make("toytwo")
    text = (config.home() / "tools" / "dev.toml").read_text()
    assert text == (f'[toyone]\npath = "{workdir}/webcli-tool-toyone"\n\n'
                    f'[toytwo]\npath = "{workdir}/webcli-tool-toytwo"\n')


# Names and identities.

@pytest.mark.parametrize(("name", "why"), [
    ("my-tool", "no hyphens"), ("Toy", "no hyphens"), ("x", "no hyphens"),
    ("gmail", "built-in site"), ("google", "built-in site, provider"), ("meta", "built-in site, provider"),
    ("lint", "webcli command"), ("tools", "webcli command"), ("none", "built-in site, provider"),
])
def test_new_refuses_names_a_tool_cant_have(workdir, name, why):
    result = run("new", name)
    assert result.exit_code == 2 and why in result.stderr
    assert not list(workdir.iterdir())


def test_new_refuses_a_name_the_library_already_has(workdir):
    tools = config.private_dir(config.ensure_home() / "tools")
    (tools / "index-cache.json").write_text(json.dumps({"tools": [{"name": "toyhttp"}], "revoked": []}))
    result = run("new", "toyhttp")
    assert result.exit_code == 2 and "webcli tools install toyhttp" in result.stderr


def test_new_never_overwrites(workdir):
    make()
    (workdir / "webcli-tool-toyhttp" / "README.md").write_text("mine")
    result = run("new", "toyhttp")
    assert result.exit_code == 2 and "already exists" in result.stderr
    assert (workdir / "webcli-tool-toyhttp" / "README.md").read_text() == "mine"


def test_new_refuses_a_name_already_being_written_elsewhere(workdir, monkeypatch):
    make()
    other = workdir / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    result = run("new", "toyhttp")
    assert result.exit_code == 2 and "already writing a tool named 'toyhttp'" in result.stderr


def test_new_refuses_a_url_that_isnt_plain_https(workdir):
    for url in ("http://httpbin.org", "https://httpbin.org:8443/", "https://u@httpbin.org/", "httpbin.org"):
        result = run("new", "toyhttp", "--url", url)
        assert result.exit_code == 2 and "--url must be a plain https:// URL" in result.stderr


def test_another_provider_warns_needs_a_grant_and_has_no_sign_in_of_its_own(workdir):
    result = run("new", "acme", "--identity", "google:work", "--url", "https://acme.example.org/app")
    assert result.exit_code == 0
    assert "webcli tools grant acme google:default" in result.stderr
    assert "--identity google:work" in result.stderr
    assert out(result)["identity"] == "google:default"
    folder = Path(out(result)["folder"])
    assert 'identity = "google"' in (folder / "webcli-tool.toml").read_text()
    _, site = scaffold.load("acme", folder)
    assert (site.provider, site.login, site.hosts) == ("google", None, ("acme.example.org",))
    assert lint("acme")[0] == 0


def test_a_tools_own_provider_gets_a_sign_in_held_to_its_hosts(workdir):
    folder = make()
    _, site = scaffold.load("toyhttp", folder)
    assert site.login is not None
    from webcli_tool_toyhttp import guard

    assert guard.SIGN_IN_POLICY.allowed(Req("POST", "https://httpbin.org/login"), None)
    assert not guard.SIGN_IN_POLICY.allowed(Req("GET", "https://accounts.example.net/"), None)


def test_identity_none_is_refused(workdir):
    result = run("new", "toyhttp", "--identity", "none")
    assert result.exit_code == 2 and "always has an identity" in result.stderr


# lint: each check catches what it should.

def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


def test_lint_catches_a_write_command_the_manifest_calls_a_read(workdir):
    folder = make()
    edit(folder / "webcli_tool_toyhttp" / "cli.py", "add_agent_help(group, notes=NOTES, writes=WRITES)", '''
from webcli.adapter import write_options


@group.command("star")
@write_options
def star_cmd(dry_run, confirm):
    """Star it."""


add_agent_help(group, notes=NOTES, writes=WRITES)''')
    code, findings = lint("toyhttp")
    assert code == 7
    assert errors(findings) == ['commands: "star" is a command but not in webcli-tool.toml\'s [[commands]]',
                                'writes: "star" takes --confirm, so it writes: mark it writes = true']


def test_lint_catches_a_guard_that_leaves_the_hosts_or_writes(workdir):
    folder = make()
    edit(folder / "webcli_tool_toyhttp" / "guard.py",
         'return method.upper() in ("GET", "HEAD") and on_hosts(url)', "return True")
    code, findings = lint("toyhttp")
    assert code == 7
    assert errors(findings) == [
        "guard: POLICY lets the page reach webcli-lint.invalid, a host outside hosts",
        "guard: POLICY lets a PUT through to httpbin.org, but no command is marked writes = true",
        "guard: POLICY lets a PATCH through to httpbin.org, but no command is marked writes = true",
        "guard: POLICY lets a DELETE through to httpbin.org, but no command is marked writes = true"]


def test_lint_catches_imports_from_webcli_internals(workdir):
    folder = make()
    edit(folder / "webcli_tool_toyhttp" / "parse.py", "from __future__ import annotations",
         "from __future__ import annotations\n\nfrom webcli.core import browser  # noqa\nfrom webcli import sites  # noqa")
    code, findings = lint("toyhttp")
    assert code == 7
    assert errors(findings) == [
        "imports: webcli_tool_toyhttp/parse.py:6: imports webcli.core; a tool may import only webcli.adapter from webcli",
        "imports: webcli_tool_toyhttp/parse.py:7: imports webcli.sites; a tool may import only webcli.adapter from webcli"]


def test_lint_catches_secrets_and_personal_details_in_fixtures(workdir):
    folder = make()
    (folder / "tests" / "fixtures" / "leak.json").write_text(json.dumps({
        "url": "https://httpbin.org/", "title": "Signed in as kevin@realmail.net", "loginForm": False,
        "headings": [], "links": [{"text": "x", "href": "https://httpbin.org/?session_token=abcdef123456"}]},
        indent=1))
    (folder / "webcli_tool_toyhttp" / "keys.py").write_text('KEY = "AKIAABCDEFGHIJKLMNOP"\nOK = "user@example.com"\n')
    code, findings = lint("toyhttp")
    assert code == 7
    assert errors(findings) == [
        "secrets: tests/fixtures/leak.json:3: looks like an email address",
        "secrets: tests/fixtures/leak.json:9: looks like something webcli's redaction hides (a token, session id, "
        "or signed link)",
        "secrets: webcli_tool_toyhttp/keys.py:1: looks like an AWS access key"]


def test_lint_catches_an_adapter_that_disagrees_with_its_manifest(workdir):
    folder = make()
    edit(folder / "webcli-tool.toml", 'hosts = ["httpbin.org"]', 'hosts = ["httpbin.org", "cdn.httpbin.org"]')
    code, findings = lint("toyhttp")
    assert code == 7
    assert errors(findings) == ["adapter: Its ADAPTER doesn't match webcli-tool.toml: hosts: the adapter says "
                                "['httpbin.org'], the manifest ['cdn.httpbin.org', 'httpbin.org']"]


@pytest.mark.parametrize("name", ["google", "meta", "none"])
def test_lint_refuses_a_tool_named_for_a_provider_or_none(workdir, name):
    # `new` refuses these names, so make toyhttp and rename it throughout: a tool that is right in every other way.
    # The library's CI lints tools/<name>/ by path. Lint has to refuse it there: core would never load it.
    folder = make()
    (folder / "webcli_tool_toyhttp").rename(folder / f"webcli_tool_{name}")
    for path in [p for p in folder.rglob("*") if p.is_file()]:
        path.write_text(path.read_text().replace("toyhttp", name))
    result = run("lint", str(folder), "--format", "json")
    assert result.exit_code == 7
    assert errors(out(result)["findings"]) == [
        f"manifest: name: {name!r} is a built-in site, provider, or webcli command; pick another name"]


def test_lint_lists_every_manifest_problem(workdir):
    folder = make()
    edit(folder / "webcli-tool.toml", 'version = "0.1.0"', 'version = "one"\nwrite = true')
    code, findings = lint("toyhttp")
    assert code == 7
    assert errors(findings) == ["manifest: unknown key 'write'",
                                'manifest: version: must be X.Y.Z ("0.1.0"), optionally with a pre-release ("0.2.0rc1")']


def test_lint_needs_tests(workdir):
    folder = make()
    (folder / "tests" / "test_tool.py").unlink()
    code, findings = lint("toyhttp")
    assert code == 7 and errors(findings) == [
        "tests: tests/ has no test_*.py: the library runs every tool's offline tests"]


# test: offline, plain functions.

def test_test_reports_failures_and_blocks_the_network(workdir):
    folder = make()
    (folder / "tests" / "test_more.py").write_text('''
import socket


def test_fails():
    assert 1 == 2, "one is not two"


def test_reaches_out():
    socket.create_connection(("httpbin.org", 443), timeout=1)


def test_wants_a_fixture(tmp_path):
    pass


async def test_is_async():
    pass
''')
    result = run("test", "toyhttp", "--format", "json")
    assert result.exit_code == 7 and "4 of 9 tests failed" in result.stderr
    failed = {r["test"]: r["error"] for r in out(result)["tests"] if r["result"] == "failed"}
    assert failed == {
        "test_more.py::test_fails": "AssertionError: one is not two (line 6)",
        "test_more.py::test_reaches_out": "OSError: webcli test runs offline: this test tried to use the network "
                                          "(line 10)",
        "test_more.py::test_wants_a_fixture": "takes arguments: webcli test has no pytest fixtures",
        "test_more.py::test_is_async": "async test: webcli test runs plain functions only"}
    import socket

    assert socket.create_connection.__name__ == "create_connection"  # restored


def test_test_with_no_tests_exits_3(workdir):
    folder = make()
    (folder / "tests" / "test_tool.py").write_text("X = 1\n")
    result = run("test", "toyhttp")
    assert result.exit_code == 3 and "has no tests" in result.stderr


def test_test_refuses_a_tool_that_doesnt_load(workdir):
    folder = make()
    edit(folder / "webcli_tool_toyhttp" / "__init__.py", 'version="0.1.0"', 'version="0.2.0"')
    result = run("test", "toyhttp")
    assert result.exit_code == 7 and "version: the adapter says '0.2.0', the manifest '0.1.0'" in result.stderr


# dev: runs the tool's commands, in this process, picking up edits.

def test_dev_runs_the_tool_as_if_installed(workdir, monkeypatch):
    folder = make()
    calls = []

    async def fake_call(ident, site, op, *args, **kwargs):
        daemon.site_op(ident, site, op)  # resolves through the registry and core's trust checks, as the daemon does
        calls.append((ident.id, site, op, args, daemon.enabled()))
        return [{"kind": "heading", "text": "httpbin", "url": None}]

    monkeypatch.setattr(daemon, "call", fake_call)
    monkeypatch.delenv("WEBCLI_DAEMON")
    result = run("dev", "toyhttp", "--", "page", "--limit", "5", "--format", "json")
    assert result.exit_code == 0, result.output
    assert out(result) == [{"kind": "heading", "text": "httpbin", "url": None}]
    assert calls == [("toyhttp:default", "toyhttp", "page", (5,), False)]
    assert sites.tier_of("toyhttp") == sites.COMMUNITY and sites.hosts_limit("toyhttp") == ("httpbin.org",)

    edit(folder / "webcli_tool_toyhttp" / "cli.py", "Max rows.", "Max rows, edited.")
    assert "Max rows, edited." in run("dev", "toyhttp", "page", "--help").stdout


def test_dev_without_a_tool_says_how_to_make_one(workdir):
    result = run("dev", "nope", "--", "page")
    assert result.exit_code == 7 and "webcli new nope" in result.stderr


def test_dev_refuses_a_tool_core_skipped(workdir):
    folder = make()
    edit(folder / "webcli-tool.toml", 'version = "0.1.0"', 'version = "0.2.0"')
    result = run("dev", "toyhttp", "--", "page")
    assert result.exit_code == 7 and "webcli didn't load the toyhttp tool" in result.stderr
    assert "version: the adapter says '0.1.0', the manifest '0.2.0'" in result.stderr


def test_dev_reports_a_tool_that_doesnt_load(workdir):
    folder = make()
    edit(folder / "webcli_tool_toyhttp" / "parse.py", "def rows(", "def rows(:")
    result = run("dev", "toyhttp", "--", "page")
    assert result.exit_code == 7 and "toyhttp's commands failed to load: SyntaxError" in result.stderr


# Guided capture: read-only, proposals, scrubbed fixtures.

class Req:
    def __init__(self, method: str, url: str, post_data: str | None = None) -> None:
        self.method, self.url, self.post_data = method, url, post_data


def test_capture_lets_only_reads_out():
    policy = capture.read_only_policy("toyhttp")
    assert [policy.allowed(Req(m, "https://anywhere.example/x"), None) for m in
            ("GET", "HEAD", "OPTIONS", "get", "POST", "PUT", "PATCH", "DELETE")] == \
        [True, True, True, True, False, False, False, False]


def test_request_lines_name_graphql_operations_and_never_values():
    body = json.dumps({"operationName": "ListNotes", "query": "query ListNotes { notes { id } }",
                       "variables": {"secret": "s3cr3t-value-123456"}})
    assert capture.request_line("POST", "https://notes.example.com/graphql", body) == \
        "POST notes.example.com/graphql op=ListNotes (query)"
    persisted = json.dumps({"operationName": "Star", "extensions": {"persistedQuery": {"sha256Hash": "ab" * 32}}})
    assert capture.request_line("POST", "https://notes.example.com/graphql", persisted) == \
        "POST notes.example.com/graphql op=Star (unknown)"
    assert capture.request_line("POST", "https://notes.example.com/api?x=1", "a=1&b=2") == \
        "POST notes.example.com/api ?x keys=['a', 'b']"


def test_propose_keeps_first_party_hosts_and_reads():
    data = {"traffic": ["GET www.notes.example.com/", "GET api.notes.example.com/v1/me", "OPTIONS api.notes.example.com/v1/me",
                        "GET cdn.thirdparty.net/lib.js", "POST www.notes.example.com/graphql op=Star (mutation)",
                        "GET notes.example.com.evil.net/x"],
            "blocked": ["POST www.notes.example.com/graphql"]}
    assert capture.propose("https://www.notes.example.com/home", data) == {
        "hosts": ["www.notes.example.com", "api.notes.example.com"],
        "otherHosts": ["cdn.thirdparty.net", "notes.example.com.evil.net"],
        "reads": ["GET www.notes.example.com/", "GET api.notes.example.com/v1/me"],
        "blocked": ["POST www.notes.example.com/graphql"]}


def test_fixture_scrubs_tokens_and_emails_and_keeps_it_small():
    snapshot = {"url": "https://notes.example.com/?session=abc123def456ghi789jk", "title": "Hi kevin@realmail.net",
                "loginForm": 0, "headings": [f"h{i}" for i in range(30)],
                "links": [{"text": "me", "href": "https://notes.example.com/u/9f8e7d6c5b4a39281706f5e4d3c2b1a0"}] * 30}
    clean = capture.fixture(snapshot)
    assert clean["url"].startswith(f"https://notes.example.com/?session={REDACTED}") and "abc123" not in clean["url"]
    assert clean["title"] == "Hi user@example.com"
    assert clean["loginForm"] is False
    assert len(clean["headings"]) == len(clean["links"]) == capture.FIXTURE_ITEMS
    assert clean["links"][0] == {"text": "me", "href": f"https://notes.example.com/u/{REDACTED}"}
    assert capture.fixture(None) is None


class FakeContext:
    def __init__(self) -> None:
        self.handlers = []

    def on(self, event, handler):
        assert event == "request"
        self.handlers.append(handler)


class FakePage:
    """Loads `url`, makes two requests, and is closed by the person after `polls` snapshots."""

    def __init__(self, polls: int) -> None:
        self.context, self.polls, self.url = FakeContext(), polls, None

    async def goto(self, url, **_options):
        self.url = url
        for handler in self.context.handlers:
            handler(Req("GET", url))
            handler(Req("POST", "https://notes.example.com/api/like", '{"id": 1}'))

    async def evaluate(self, js):
        assert js == capture.PAGE_JS
        self.polls -= 1
        return {"url": self.url, "title": "Notes", "loginForm": False, "headings": ["Notes"], "links": []}

    def is_closed(self):
        return self.polls <= 0


def test_record_watches_until_the_window_closes():
    data = asyncio.run(capture.record(FakePage(polls=2), "https://notes.example.com/", timeout=60, poll=0))
    assert data == {"url": "https://notes.example.com/", "traffic": [
        "GET notes.example.com/", "POST notes.example.com/api/like keys=['id']"], "blocked": [],
        "snapshot": {"url": "https://notes.example.com/", "title": "Notes", "loginForm": False,
                     "headings": ["Notes"], "links": []}}


CAPTURED = {"url": "https://www.notes.example.com/", "blocked": ["POST www.notes.example.com/graphql"],
            "traffic": ["GET www.notes.example.com/", "GET static.notes.example.com/app.js", "GET ads.tracker.net/p"],
            "snapshot": {"url": "https://www.notes.example.com/?sid=abc123def456ghi789jk", "title": "Notes",
                         "loginForm": False, "headings": ["Your notes"],
                         "links": [{"text": "Mail kevin@realmail.net", "href": "https://www.notes.example.com/contact"}]}}


def test_new_with_capture_starts_the_tool_from_what_it_saw(workdir, monkeypatch):
    seen = []
    monkeypatch.setattr(capture, "guided", lambda name, url, timeout: seen.append((name, url, timeout)) or CAPTURED)
    result = run("new", "notes", "--url", "https://www.notes.example.com/", "--capture", "--timeout", "30",
                 "--format", "json")
    assert result.exit_code == 0, result.output
    assert seen == [("notes", "https://www.notes.example.com/", 30)]
    made = out(result)
    assert made["hosts"] == ["www.notes.example.com", "static.notes.example.com"]
    assert made["capture"]["proposals"]["otherHosts"] == ["ads.tracker.net"]
    assert made["capture"]["proposals"]["blocked"] == ["POST www.notes.example.com/graphql"]
    record = Path(made["capture"]["record"])
    assert record.parent == config.home() / "sites" / "notes" / "capture" and record.stat().st_mode & 0o777 == 0o600
    folder = Path(made["folder"])
    fixture = json.loads((folder / made["capture"]["fixture"]).read_text())
    assert fixture["url"].startswith(f"https://www.notes.example.com/?sid={REDACTED}") and "abc123" not in fixture["url"]
    assert fixture["links"] == [{"text": "Mail user@example.com", "href": "https://www.notes.example.com/contact"}]
    assert 'hosts = ["www.notes.example.com", "static.notes.example.com"]' in (folder / "webcli-tool.toml").read_text()
    assert lint("notes")[0] == 0
    assert run("test", "notes").exit_code == 0


def test_capture_again_on_an_existing_tool_adds_a_page_and_suggests_hosts(workdir, monkeypatch):
    folder = make("notes", "--url", "https://www.notes.example.com/")
    toml = (folder / "webcli-tool.toml").read_text()
    monkeypatch.setattr(capture, "guided", lambda name, url, timeout: CAPTURED)
    result = run("new", "notes", "--capture", "--format", "json")
    assert result.exit_code == 0, result.output
    made = out(result)
    assert made["recaptured"] is True and made["missingHosts"] == ["static.notes.example.com"]
    assert (folder / "webcli-tool.toml").read_text() == toml
    assert len(list((folder / "tests" / "fixtures").glob("*.json"))) == 2


def test_a_failed_capture_writes_nothing(workdir, monkeypatch):
    def broken(*_args):
        raise RuntimeError("Chrome isn't installed")

    monkeypatch.setattr(capture, "guided", broken)
    result = run("new", "notes", "--url", "https://notes.example.com/", "--capture")
    assert result.exit_code == 5 and "Capture failed: RuntimeError: Chrome isn't installed" in result.stderr
    assert not list(workdir.iterdir()) and scaffold.dev_tools() == {}
