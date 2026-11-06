"""webcli 1.0 Phase D: `webcli submit`. Offline: every gh and git call goes to a fake _run, so nothing reaches
GitHub."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from webcli import sites
from webcli.apps import submit_cmd
from webcli.apps import webcli as webcli_app


def run(*args: str, input: str | None = None):
    return CliRunner().invoke(webcli_app.build(), list(args), input=input)


@pytest.fixture(autouse=True)
def workdir(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    sites.reload()
    yield work
    sites.reload()
    for name in [m for m in sys.modules if m.startswith(("webcli_tool_", "_webcli_test_"))]:
        del sys.modules[name]


@pytest.fixture
def no_network(monkeypatch):
    def refuse(args, cwd=None, *, check=True):
        raise AssertionError(f"ran {args}")

    monkeypatch.setattr(submit_cmd, "_run", refuse)


def make(maintainer: str = "github:someone") -> Path:
    result = run("new", "toyhttp", "--url", "https://httpbin.org")
    assert result.exit_code == 0, result.output
    folder = Path(json.loads(result.stdout)["folder"])
    manifest = folder / "webcli-tool.toml"
    manifest.write_text(manifest.read_text().replace("github:your-github-login", maintainer))
    return folder


SENT = ["README.md", "conftest.py", "tests/fixtures/page.json", "tests/test_tool.py", "webcli-tool.toml",
        "webcli_tool_toyhttp/__init__.py", "webcli_tool_toyhttp/cli.py", "webcli_tool_toyhttp/endpoints.py",
        "webcli_tool_toyhttp/fetch.py", "webcli_tool_toyhttp/guard.py", "webcli_tool_toyhttp/parse.py"]


# The dry run: checks everything, sends nothing.

def test_dry_run_checks_and_sends_nothing(no_network):
    folder = make()
    (folder / ".env").write_text("TOKEN=abc\n")
    (folder / "__pycache__").mkdir()
    (folder / "__pycache__" / "x.pyc").write_bytes(b"\x00\x01")
    result = run("submit", "toyhttp", "--format", "json")
    assert result.exit_code == 0, result.output
    plan = json.loads(result.stdout)
    assert plan["dryRun"] is True and plan["version"] == "0.1.0" and plan["testsPassed"] == 5
    assert plan["files"] == SENT  # hidden files and caches never go out
    assert plan["warnings"] == [] and len(plan["checklist"]) == 4
    assert plan["plan"][-1] == ("gh pr create --repo truefrontier/webcli-tools --base main "
                                "--head <your-github-login>:submit/toyhttp-0.1.0")
    assert "--confirm" in plan["hint"]


def test_dry_run_takes_a_folder_path(no_network):
    folder = make()
    assert run("submit", str(folder), "--format", "json").exit_code == 0


def test_the_placeholder_maintainer_blocks_it(no_network):
    make(maintainer="github:your-github-login")
    result = run("submit", "toyhttp", "--format", "json")
    assert result.exit_code == 7
    assert "replace github:your-github-login" in json.loads(result.stderr)["problems"][0]


@pytest.mark.parametrize("break_it, why", [
    (lambda f: (f / "tests/fixtures/page.json").write_text('{"token": "ghp_' + "a" * 36 + '"}'),
     "looks like a GitHub token"),
    (lambda f: (f / "tests/test_tool.py").write_text("def test_fails():\n    assert False\n"),
     "test test_tool.py::test_fails"),
    (lambda f: (f / "tests/test_tool.py").write_text("X = 1\n"), "tests: none ran"),
    (lambda f: (f / "webcli-tool.toml").write_text((f / "webcli-tool.toml").read_text().replace(
        'strategy = "cookie"', 'strategy = "magic"', 1)), "manifest:"),
])
def test_lint_errors_and_failing_tests_block_it(no_network, break_it, why):
    break_it(make())
    result = run("submit", "toyhttp", "--format", "json")
    assert result.exit_code == 7
    assert any(why in problem for problem in json.loads(result.stderr)["problems"]), result.stderr


def test_links_and_binary_files_block_it(no_network, tmp_path):
    folder = make()
    (tmp_path / "secret.txt").write_text("not the tool's")
    (folder / "tests" / "fixtures" / "linked.json").symlink_to(tmp_path / "secret.txt")
    (folder / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
    result = run("submit", "toyhttp")
    assert result.exit_code == 7
    assert "tests/fixtures/linked.json is a link" in result.stderr
    assert "logo.png isn't UTF-8 text" in result.stderr


# --confirm: the gh flow, faked.

class FakeGitHub:
    """Answers each gh and git call; `calls` records them. `clone` sets up the library as it is upstream."""

    def __init__(self, *, login="someone", parent="truefrontier/webcli-tools", open_pr="", status=None,
                 library: dict[str, str] | None = None):
        self.calls, self.login, self.parent, self.open_pr = [], login, parent, open_pr
        self.status = ["A  tools/toyhttp/webcli-tool.toml"] if status is None else status
        self.library = library or {}
        self.sent: dict[str, bytes] = {}

    def __call__(self, args, cwd=None, *, check=True):
        self.calls.append(args)
        out = ""
        if args[:2] == ["git", "clone"]:
            for rel, text in self.library.items():
                path = Path(args[-1]) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
        elif args[:2] == ["git", "status"]:
            out = "\n".join(self.status)
            tool = Path(cwd) / "tools" / "toyhttp"
            self.sent = {p.relative_to(tool).as_posix(): p.read_bytes() for p in tool.rglob("*") if p.is_file()}
        elif args[:3] == ["gh", "api", "user"]:
            out = self.login
        elif args[:3] == ["gh", "repo", "view"]:
            out = self.parent
        elif args[:3] == ["gh", "pr", "list"]:
            out = self.open_pr
        elif args[:3] == ["gh", "pr", "create"]:
            out = "https://github.com/truefrontier/webcli-tools/pull/7"
        return subprocess.CompletedProcess(args, 0, out + "\n" if out else "", "")

    def ran(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


YES = "y\n" * len(submit_cmd.CHECKLIST)


def test_confirm_forks_pushes_and_opens_the_pr(monkeypatch):
    folder = make()
    gh = FakeGitHub()
    monkeypatch.setattr(submit_cmd, "_run", gh)
    result = run("submit", "toyhttp", "--confirm", "--format", "json", input=YES)
    assert result.exit_code == 0, result.output
    done = json.loads(result.stdout.splitlines()[-1])
    assert done["pullRequest"] == "https://github.com/truefrontier/webcli-tools/pull/7" and done["created"] is True
    assert done["branch"] == "someone:submit/toyhttp-0.1.0"

    assert gh.ran("git", "clone") == [["git", "clone", "-q", "--branch", "main",
                                       "https://github.com/truefrontier/webcli-tools.git", gh.ran("git", "clone")[0][-1]]]
    assert gh.ran("gh", "repo", "fork")[0][:4] == ["gh", "repo", "fork", "truefrontier/webcli-tools"]
    assert gh.ran("git", "commit") == [["git", "commit", "-q", "-m", "Add toyhttp 0.1.0"]]
    push = next(c for c in gh.calls if "push" in c)
    assert push[-2:] == ["https://github.com/someone/webcli-tools.git", "HEAD:refs/heads/submit/toyhttp-0.1.0"]
    create = gh.ran("gh", "pr", "create")[0]
    assert create[create.index("--head") + 1] == "someone:submit/toyhttp-0.1.0"
    assert create[create.index("--title") + 1] == "Add toyhttp 0.1.0"
    body = create[create.index("--body") + 1]
    assert "| `page` | cookie | no | no |" in body and "`httpbin.org`" in body and body.count("- [x] ") == 4
    # Exactly the files lint checked, byte for byte.
    assert sorted(gh.sent) == SENT
    assert all(gh.sent[rel] == (folder / rel).read_bytes() for rel in SENT)
    # The checklist came after the library was read and before anything was written to GitHub.
    order = [c[:3] for c in gh.calls]
    assert order.index(["git", "status", "--porcelain"]) < order.index(["gh", "repo", "fork"])


def test_answering_no_stops_before_anything_is_sent(monkeypatch):
    make()
    gh = FakeGitHub()
    monkeypatch.setattr(submit_cmd, "_run", gh)
    result = run("submit", "toyhttp", "--confirm", input="y\nn\n")
    assert result.exit_code == 130
    assert not gh.ran("gh", "repo", "fork") and not [c for c in gh.calls if "push" in c]
    assert not gh.ran("gh", "pr", "create")


def test_an_open_pr_for_the_branch_is_updated_not_duplicated(monkeypatch):
    make()
    gh = FakeGitHub(open_pr="https://github.com/truefrontier/webcli-tools/pull/3")
    monkeypatch.setattr(submit_cmd, "_run", gh)
    result = run("submit", "toyhttp", "--confirm", "--format", "json", input=YES)
    assert result.exit_code == 0, result.output
    done = json.loads(result.stdout.splitlines()[-1])
    assert done["pullRequest"].endswith("/pull/3") and done["created"] is False
    assert not gh.ran("gh", "pr", "create")


def test_it_wont_push_to_a_repo_that_isnt_the_librarys_fork(monkeypatch):
    make()
    gh = FakeGitHub(parent="/")
    monkeypatch.setattr(submit_cmd, "_run", gh)
    result = run("submit", "toyhttp", "--confirm", input=YES)
    assert result.exit_code == 7 and "isn't a fork of truefrontier/webcli-tools" in result.stderr
    assert not [c for c in gh.calls if "push" in c]


def test_an_update_must_raise_the_version(monkeypatch):
    make()
    upstream = (Path("webcli-tool-toyhttp") / "webcli-tool.toml").read_text()
    gh = FakeGitHub(library={"tools/toyhttp/webcli-tool.toml": upstream})
    monkeypatch.setattr(submit_cmd, "_run", gh)
    result = run("submit", "toyhttp", "--confirm", input=YES)
    assert result.exit_code == 7 and "already has toyhttp 0.1.0" in result.stderr
    assert not gh.ran("gh", "repo", "fork")


def test_nothing_new_stops_it(monkeypatch):
    make()
    gh = FakeGitHub(status=[])
    monkeypatch.setattr(submit_cmd, "_run", gh)
    result = run("submit", "toyhttp", "--confirm", input=YES)
    assert result.exit_code == 7 and "nothing to submit" in result.stderr


def test_no_gh_says_how_to_get_it(monkeypatch):
    make()
    monkeypatch.setattr(submit_cmd.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    result = run("submit", "toyhttp", "--confirm", input=YES)
    assert result.exit_code == 7 and "`gh` isn't installed" in result.stderr and "cli.github.com" in result.stderr
