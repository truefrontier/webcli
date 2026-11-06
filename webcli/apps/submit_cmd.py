"""`webcli submit`: send a tool you wrote to the webcli-tools library as a pull request. webcli.py adds every command
in COMMANDS.

What goes out is a snapshot: the tool's files are read once, written to a staging folder, and `lint` and `test` run
on that copy, so the bytes checked are the bytes sent. Without --confirm nothing touches the network. With it, the
library is cloned (public, read-only), the changes are shown, a person answers the checklist, and only then is the
library forked, a branch pushed to the fork, and the pull request opened, all with the GitHub CLI (`gh`).

Every gh and git call goes through _run, so tests replace it and stay offline.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import click

from webcli import __version__, scaffold
from webcli.apps.devtools_run import _emit
from webcli.core import manifest_schema
from webcli.core.errors import ConfigError
from webcli.core.output import (
    EXIT_API,
    EXIT_CONFIG,
    EXIT_OK,
    agent_output_options,
    die,
    note,
)
from webcli.scaffold import checks

LIBRARY = "truefrontier/webcli-tools"
CLONE_URL = f"https://github.com/{LIBRARY}.git"
RULES = f"https://github.com/{LIBRARY}/blob/main/CONTRIBUTING.md"
GH_HINT = "Install the GitHub CLI (https://cli.github.com/, `brew install gh`), then run `gh auth login`."
PUSH_TRIES = 3  # a fork GitHub just created can take a few seconds to accept a push
CHECKLIST = [
    ("I read every file in tests/fixtures/: no cookies, tokens, or anyone's personal details (names, emails, "
     "messages, account IDs)"),
    "hosts in webcli-tool.toml lists every host the tool reaches",
    "I'll maintain this tool: fix it when the site changes, and answer issues about it",
    f"I read the library's rules ({RULES}), and the tool is for people's own accounts within the site's terms",
]


def _run(args: list[str], cwd: Path | None = None, *, check: bool = True) -> subprocess.CompletedProcess:
    """Run gh or git. With check, a failure exits 5 with what the command said."""
    try:
        done = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        die(f"`{args[0]}` isn't installed.", EXIT_CONFIG, hint=GH_HINT if args[0] == "gh" else None)
    if check and done.returncode != 0:
        said = (done.stderr or done.stdout).strip().splitlines()
        die(f"`{' '.join(args[:3])}` failed: {said[-1] if said else f'exit {done.returncode}'}", EXIT_API)
    return done


def snapshot(folder: Path) -> dict[str, bytes]:
    """relative path -> bytes for every file that would be sent: what lint scans (no .git, caches, or virtualenvs)
    minus hidden files (.env, .DS_Store). A link, or a file that isn't UTF-8 text, stops the submit: lint can't
    vouch for either."""
    files, problems = {}, []
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder)
        parts = set(rel.parts)
        if parts & checks.SKIP_DIRS or any(p.startswith(".") for p in rel.parts):
            continue
        if path.is_symlink():
            problems.append(f"{rel.as_posix()} is a link; the library takes regular files only")
        elif path.is_file():
            data = path.read_bytes()
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                problems.append(f"{rel.as_posix()} isn't UTF-8 text, so lint can't scan it; leave it out")
                continue
            files[rel.as_posix()] = data
    if problems:
        raise ConfigError("Not submitting:\n" + "\n".join(f"  - {p}" for p in problems))
    return files


def write_files(dest: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def check(name: str, staged: Path) -> tuple[manifest_schema.Manifest, list[str], int]:
    """(manifest, warnings, tests passed) for the staged copy, or exit 7 with every reason it can't be submitted."""
    findings = checks.lint(name, staged)
    blockers = [f"{f['check']}: {f['message']}" for f in findings
                if f["level"] == "error" or f["check"] in ("hosts", "maintainers")]  # placeholders block here
    warnings = [f"{f['check']}: {f['message']}" for f in findings if f"{f['check']}: {f['message']}" not in blockers]
    passed = 0
    if not any(b.startswith(("manifest:", "adapter:")) for b in blockers):
        try:
            results = checks.run_tests(name, staged)
        except ConfigError as exc:
            results = [{"test": "(loading the tool)", "result": "failed", "error": str(exc)}]
        passed = sum(r["result"] == "passed" for r in results)
        blockers += [f"test {r['test']}: {r['error']}" for r in results if r["result"] != "passed"]
        if not results:
            blockers.append("tests: none ran; the library runs every tool's offline tests")
    if blockers:
        die(f"{name} can't be submitted yet:\n" + "\n".join(f"  - {b}" for b in blockers), EXIT_CONFIG,
            hint=f"Fix each one, then run `webcli submit {name}` again.", problems=blockers)
    return manifest_schema.load(staged), warnings, passed


def plan(name: str, branch: str) -> list[str]:
    """What --confirm runs, for the dry run. The GitHub login isn't known until gh is asked."""
    login = "<your-github-login>"
    return [
        f"git clone --branch main {CLONE_URL}",
        f"(copy the files into tools/{name}/, show what changed, ask the checklist)",
        f"gh repo fork {LIBRARY} --clone=false",
        f"git commit, then git push --force https://github.com/{login}/webcli-tools.git HEAD:{branch}",
        f"gh pr create --repo {LIBRARY} --base main --head {login}:{branch}",
    ]


def _yes(flag: bool) -> str:
    return "**yes**" if flag else "no"


def pr_body(manifest: manifest_schema.Manifest, warnings: list[str], passed: int) -> str:
    commands = "\n".join(f"| `{c.name}` | {c.strategy.value} | {_yes(c.writes)} | {_yes(c.sends_to_people)} |"
                         for c in manifest.commands)
    lines = [
        f"{manifest.summary}",
        "",
        f"- **Version:** {manifest.version}",
        f"- **Identity:** {manifest.default_identity or 'none (never signs in)'}",
        f"- **Hosts:** {', '.join(f'`{h}`' for h in manifest.hosts)}",
        f"- **Maintainers:** {', '.join(manifest.maintainers)}",
        "",
        "| Command | Strategy | Writes | Sends to people |",
        "|---|---|---|---|",
        commands,
        "",
        f"`webcli lint` passed, and {passed} offline tests passed.",
    ]
    if warnings:
        lines += ["", "Lint warnings:", *(f"- {w}" for w in warnings)]
    lines += ["", "The submitter confirmed:", *(f"- [x] {item}" for item in CHECKLIST), "",
              f"Opened with `webcli submit` (webcli {__version__})."]
    return "\n".join(lines)


def _login() -> str:
    _run(["gh", "--version"])
    login = _run(["gh", "api", "user", "--jq", ".login"]).stdout.strip()
    if not login:
        die("`gh` isn't signed in to GitHub.", EXIT_CONFIG, hint="Run `gh auth login`.")
    return login


def _changes(repo: Path, name: str, manifest: manifest_schema.Manifest,
             files: dict[str, bytes]) -> tuple[str, list[str]]:
    """Put the files in the clone's tools/<name>/ (replacing what's there). ("Add" or "Update", git's short status
    lines). Exits when there's nothing new, or an update keeps the library's version."""
    dest = repo / "tools" / name
    action = "Update" if dest.exists() else "Add"
    if action == "Update":
        try:
            theirs = manifest_schema.load(dest).version
        except manifest_schema.ManifestError:
            theirs = None
        if theirs == manifest.version:
            die(f"The library already has {name} {theirs}.", EXIT_CONFIG,
                hint="Raise version in webcli-tool.toml and in ADAPTER, then submit again.")
        shutil.rmtree(dest)
    write_files(dest, files)
    _run(["git", "add", "--all", f"tools/{name}"], repo)
    status = _run(["git", "status", "--porcelain", "--", f"tools/{name}"], repo).stdout.splitlines()
    if not status:
        die(f"The library already has exactly these files for {name}; nothing to submit.", EXIT_CONFIG)
    return action, status


def _open_pr(repo: Path, login: str, branch: str, title: str, body: str) -> tuple[str, bool]:
    """Fork, push the branch to the fork, and open the pull request (or reuse the one already open for the branch).
    (url, created)."""
    _run(["gh", "repo", "fork", LIBRARY, "--clone=false", "--default-branch-only"])
    parent = _run(["gh", "repo", "view", f"{login}/webcli-tools", "--json", "parent", "--jq",
                   ".parent.owner.login + \"/\" + .parent.name"], check=False).stdout.strip()
    if parent != LIBRARY:
        die(f"{login}/webcli-tools isn't a fork of {LIBRARY}, so webcli won't push there.", EXIT_CONFIG,
            hint=f"Rename that repository, or fork {LIBRARY} yourself and submit again.")
    _run(["git", "checkout", "-q", "-b", branch], repo)
    _run(["git", "commit", "-q", "-m", title], repo)
    push = ["git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential", "push", "-q",
            "--force", f"https://github.com/{login}/webcli-tools.git", f"HEAD:refs/heads/{branch}"]
    for attempt in range(PUSH_TRIES):
        done = _run(push, repo, check=attempt == PUSH_TRIES - 1)
        if done.returncode == 0:
            break
        time.sleep(5)
    existing = _run(["gh", "pr", "list", "--repo", LIBRARY, "--head", branch, "--state", "open", "--json",
                     "url,headRepositoryOwner", "--jq",
                     f'[.[] | select(.headRepositoryOwner.login == "{login}") | .url][0] // ""']).stdout.strip()
    if existing:
        return existing, False
    url = _run(["gh", "pr", "create", "--repo", LIBRARY, "--base", "main", "--head", f"{login}:{branch}",
                "--title", title, "--body", body]).stdout.strip().splitlines()
    return (url[-1] if url else ""), True


@click.command("submit", options_metavar="[--confirm] [--format F]",
               short_help="Send a tool to the webcli-tools library as a pull request.")
@click.argument("name")
@click.option("--confirm", "--yes", "confirm", is_flag=True,
              help="Ask the checklist, then fork the library and open the pull request (default: a dry run).")
@agent_output_options(rows=False)
def submit_cmd(name, confirm, fmt, quiet) -> None:
    """Send the tool NAME to the webcli-tools library as a pull request. It runs `webcli lint` and `webcli test` on
    exactly the files it would send (not hidden files, caches, or virtualenvs; links and non-text files stop it),
    and the placeholder host and maintainer stop it too. Without --confirm it's a dry run: it shows the files, the
    checklist, and the plan, and sends nothing. With --confirm it shows what changes in the library, asks you each
    checklist item, then forks truefrontier/webcli-tools, pushes a branch to your fork, and opens the pull request,
    with the GitHub CLI (`gh`, signed in). Exit 0 when done (or for a clean dry run), 7 when the tool can't be
    submitted, 5 when GitHub or git fails, 130 when you answer no. NAME is a tool in dev.toml, or a tool folder's
    path."""
    name, folder = scaffold.find(name)
    files = snapshot(folder)
    with tempfile.TemporaryDirectory(prefix="webcli-submit-") as tmp:
        staged = Path(tmp) / "tool"
        write_files(staged, files)
        manifest, warnings, passed = check(name, staged)
        branch = f"submit/{name}-{manifest.version}"
        steps = plan(name, branch)
        record = {"tool": name, "version": manifest.version, "folder": str(folder), "files": sorted(files),
                  "testsPassed": passed, "warnings": warnings, "checklist": CHECKLIST}
        if not confirm:
            _emit({"dryRun": True, **record, "plan": steps,
                   "hint": f"Nothing was sent. Re-run with --confirm to open the pull request: webcli submit {name} "
                           "--confirm"},
                  [(f"{name} {manifest.version} passes lint and {passed} offline tests. It would send "
                    f"{len(files)} files:"), *(f"  {f}" for f in sorted(files)),
                   *(["Warnings:", *(f"  {w}" for w in warnings)] if warnings else []),
                   "With --confirm you'll be asked to confirm:", *(f"  - {c}" for c in CHECKLIST),
                   "Then it runs:", *(f"  {s}" for s in steps),
                   f"Nothing was sent. Re-run with --confirm to open the pull request: webcli submit {name} --confirm"],
                  fmt)
            raise SystemExit(EXIT_OK)

        login = _login()
        repo = Path(tmp) / "webcli-tools"
        _run(["git", "clone", "-q", "--branch", "main", CLONE_URL, str(repo)])  # full: pushes to a stale fork
        action, status = _changes(repo, name, manifest, files)
        title = f"{action} {name} {manifest.version}"
        click.echo(f"{title}: these files change in {LIBRARY}:", err=True)
        click.echo("\n".join(f"  {line}" for line in status), err=True)
        if f"github:{login}" not in manifest.maintainers:
            click.echo(f"Note: maintainers doesn't list github:{login}, the GitHub account opening this.", err=True)
        for item in CHECKLIST:
            click.confirm(item, abort=True, err=True)
        url, created = _open_pr(repo, login, branch, title, pr_body(manifest, warnings, passed))
    _emit({**record, "branch": f"{login}:{branch}", "pullRequest": url, "created": created},
          [f"{'Opened' if created else 'Updated'} {url}"], fmt)
    note("A maintainer reviews it. Once it's merged and pinned in index.json, anyone can "
         f"`webcli tools install {name}`.", quiet=quiet)
    raise SystemExit(EXIT_OK)


COMMANDS = [submit_cmd]
