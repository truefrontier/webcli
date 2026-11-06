"""`webcli lint` and `webcli test`: the checks the webcli-tools library's CI runs on a tool, and its offline tests.

Both import the tool's code, so run them on code you trust (your own, or a pull request you've read). The network
block in run_tests() and the import and guard checks in lint() catch honest mistakes; they don't sandbox anything.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import re
import socket
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import unquote_plus

import click

from webcli import scaffold
from webcli.core import manifest_schema
from webcli.core.errors import ConfigError
from webcli.core.guard import Guard
from webcli.core.redact import REDACTED, redact_text

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", ".pytest_cache", ".ruff_cache", "node_modules"}
MAX_SCAN_BYTES = 2_000_000
PROBE_HOST = "webcli-lint.invalid"
WRITE_METHODS = ("PUT", "PATCH", "DELETE")
PLACEHOLDER_HOSTS = ("www.example.com", "example.com")
PLACEHOLDER_MAINTAINER = "github:your-github-login"
# Secrets that are never fine anywhere in a tool: key files, provider API keys and tokens, and credentials pasted
# into a header.
SECRETS = [
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("an AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("a Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("an API key (sk-...)", re.compile(r"\bsk-(?:ant-|proj-|live-)?[A-Za-z0-9_-]{20,}")),
    ("a JWT", re.compile(r"\beyJ[\w-]{10,}\.eyJ[\w-]{10,}\.[\w-]{10,}")),
    ("an Authorization header value", re.compile(r"(?i)\bauthorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic)\s+"
                                                 r"[A-Za-z0-9._~+/=-]{12,}")),
    ("a Cookie header value", re.compile(r"(?i)\b(?:set-)?cookie[\"']?\s*[:=]\s*[\"']?[\w.-]+=[^\s\"';]{8,}")),
]
_EMAIL = re.compile(r"\b[\w.+-]+@([\w-]+(?:\.[\w-]+)+)\b")
_SAFE_EMAIL_DOMAINS = re.compile(r"(?:^|\.)(?:example\.(?:com|org|net)|invalid|test|localhost)$")


def _bare(text: str) -> str:
    """`text` without redaction markers or URL escapes: what redaction removed, beyond its own markers and the
    re-encoding of a URL it rewrote, is what's left over when two of these differ."""
    return unquote_plus(text.replace(REDACTED, ""))


def _finding(check: str, level: str, message: str) -> dict:
    return {"check": check, "level": level, "message": message}


def _files(folder: Path) -> Iterator[Path]:
    for path in sorted(folder.rglob("*")):
        if path.is_file() and not SKIP_DIRS & set(path.relative_to(folder).parts):
            yield path


def secrets(folder: Path) -> list[dict]:
    """Every file: the SECRETS patterns. Fixtures (tests/fixtures/), which get published as they are, also: anything
    webcli's redaction would hide (a token, a session id, a signed link) and email addresses."""
    out = []
    for path in _files(folder):
        rel = path.relative_to(folder).as_posix()
        if path.stat().st_size > MAX_SCAN_BYTES:
            out.append(_finding("secrets", "error", f"{rel}: over {MAX_SCAN_BYTES} bytes; keep fixtures small"))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            out.append(_finding("secrets", "warning", f"{rel}: not text, so not scanned"))
            continue
        fixture = rel.startswith("tests/fixtures/")
        for n, line in enumerate(text.splitlines(), 1):
            what = [label for label, pattern in SECRETS if pattern.search(line)]
            if fixture:
                if _bare(redact_text(line)) != _bare(line):
                    what.append("something webcli's redaction hides (a token, session id, or signed link)")
                if any(not _SAFE_EMAIL_DOMAINS.search(m[1].lower()) for m in _EMAIL.finditer(line)):
                    what.append("an email address")
            out.extend(_finding("secrets", "error", f"{rel}:{n}: looks like {w}") for w in what)
    return out


def imports(folder: Path) -> list[dict]:
    """A tool's Python may import webcli.adapter, but nothing else of webcli's (the rest can change in any
    release). Its own package, the standard library, and other packages are fine."""
    out = []
    for path in _files(folder):
        if path.suffix != ".py":
            continue
        rel = path.relative_to(folder).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except (SyntaxError, UnicodeDecodeError) as exc:
            out.append(_finding("imports", "error", f"{rel}: doesn't parse: {exc}"))
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [f"{node.module}.{alias.name}" if node.module == "webcli" else node.module
                           for alias in node.names]
            else:
                continue
            for module in modules:
                if module.split(".")[0] == "webcli" and module != "webcli.adapter":
                    out.append(_finding("imports", "error", f"{rel}:{node.lineno}: imports {module}; a tool may "
                                                            "import only webcli.adapter from webcli"))
    return out


class _Probe:
    """Enough of a Playwright Request for a Policy to judge: a top-level document request."""

    def __init__(self, method: str, url: str) -> None:
        self.method, self.url, self.headers, self.post_data = method, url, {}, None
        self.resource_type, self.frame, self.redirected_from = "document", None, None

    def is_navigation_request(self) -> bool:
        return True


def _allows(policy, method: str, url: str) -> bool:
    try:
        return bool(policy.allowed(_Probe(method, url), Guard()))
    except Exception:
        return False  # the guard treats a policy that raises as "no"


def guard(site, manifest: manifest_schema.Manifest) -> list[dict]:
    """The tool's own POLICY (in its fetch module): it must refuse a host outside the manifest's hosts, and, for a
    tool with no write commands, every PUT, PATCH, and DELETE to its own hosts."""
    try:
        fetch = importlib.import_module(site.fetch)
    except Exception as exc:
        return [_finding("guard", "error", f"{site.fetch} failed to import: {type(exc).__name__}: {exc}")]
    policy = getattr(fetch, "POLICY", None)
    if policy is None or not callable(getattr(policy, "allowed", None)):
        return [_finding("guard", "warning", f"{site.fetch} has no POLICY, so lint can't check its guard")]
    out = []
    if _allows(policy, "GET", f"https://{PROBE_HOST}/"):
        out.append(_finding("guard", "error", f"POLICY lets the page reach {PROBE_HOST}, a host outside hosts"))
    if not manifest.writes:
        for pattern in manifest.hosts:
            host = "lint." + pattern[2:] if pattern.startswith("*.") else pattern
            for method in WRITE_METHODS:
                if _allows(policy, method, f"https://{host}/webcli-lint"):
                    out.append(_finding("guard", "error", f"POLICY lets a {method} through to {host}, but no "
                                                          "command is marked writes = true"))
    return out


def _leaves(group: click.Command, path: tuple[str, ...] = ()) -> Iterator[tuple[str, click.Command]]:
    if isinstance(group, click.Group):
        for word, command in group.commands.items():
            if word != "agent-help":
                yield from _leaves(command, (*path, word))
    elif path:
        yield " ".join(path), group


def commands(site, manifest: manifest_schema.Manifest) -> list[dict]:
    """The commands people can type are exactly the manifest's, and every one that takes --confirm is marked
    writes = true. Write commands are allowed, but each is a warning: the library reviews every one."""
    try:
        leaves = dict(_leaves(site.cli()))
    except Exception as exc:
        return [_finding("commands", "error", f"the tool's commands failed to load: {type(exc).__name__}: {exc}")]
    listed = {c.name for c in manifest.commands}
    out = [_finding("commands", "error", f'"{n}" is a command but not in webcli-tool.toml\'s [[commands]]')
           for n in sorted(set(leaves) - listed)]
    out += [_finding("commands", "error", f'"{n}" is in webcli-tool.toml\'s [[commands]] but not a command')
            for n in sorted(listed - set(leaves))]
    for name, command in sorted(leaves.items()):
        if any(param.name == "confirm" for param in command.params) and name not in manifest.writes:
            out.append(_finding("writes", "error", f'"{name}" takes --confirm, so it writes: mark it writes = true'))
    out += [_finding("writes", "warning", f'"{n}" writes: it is a dry run without --confirm, and the library '
                                          "reviews every write command")
            for n in sorted(manifest.writes)]
    return out


def placeholders(manifest: manifest_schema.Manifest) -> list[dict]:
    out = [_finding("hosts", "warning", f"{h} is the placeholder host: run `webcli new` with --url, or set the "
                                        "real hosts in webcli-tool.toml and endpoints.py")
           for h in manifest.hosts if h in PLACEHOLDER_HOSTS]
    if PLACEHOLDER_MAINTAINER in manifest.maintainers:
        out.append(_finding("maintainers", "warning", f"replace {PLACEHOLDER_MAINTAINER} with your GitHub login "
                                                      "before you submit"))
    return out


def tests_present(folder: Path) -> list[dict]:
    out = []
    if not list((folder / "tests").glob("test_*.py")):
        out.append(_finding("tests", "error", "tests/ has no test_*.py: the library runs every tool's offline tests"))
    if not list((folder / "tests" / "fixtures").glob("*.json")):
        out.append(_finding("tests", "warning", "tests/fixtures/ has no recorded page (*.json) to test against"))
    return out


def lint(name: str, folder: Path) -> list[dict]:
    """Every finding for the tool at `folder`, errors and warnings. It passes when there are no errors."""
    out = secrets(folder) + imports(folder) + tests_present(folder)
    try:
        manifest = manifest_schema.load(folder)
    except manifest_schema.ManifestError as exc:
        return [_finding("manifest", "error", problem) for problem in exc.problems] + out
    try:
        _, site = scaffold.load(name, folder)
    except ConfigError as exc:
        return [_finding("adapter", "error", str(exc))] + placeholders(manifest) + out
    return guard(site, manifest) + commands(site, manifest) + placeholders(manifest) + out


@contextmanager
def offline() -> Iterator[None]:
    """No new network connections from this process while inside (DNS included)."""

    def refuse(*_args, **_kwargs):
        raise OSError("webcli test runs offline: this test tried to use the network")

    patched = [(socket.socket, "connect"), (socket.socket, "connect_ex"), (socket, "create_connection"),
               (socket, "getaddrinfo")]
    saved = [getattr(owner, attr) for owner, attr in patched]
    for owner, attr in patched:
        setattr(owner, attr, refuse)
    try:
        yield
    finally:
        for (owner, attr), original in zip(patched, saved):
            setattr(owner, attr, original)


def _error(exc: BaseException, path: Path) -> str:
    where = [f for f in traceback.extract_tb(exc.__traceback__) if Path(f.filename) == path]
    line = f" (line {where[-1].lineno})" if where else ""
    return f"{type(exc).__name__}: {exc}{line}"


def run_tests(name: str, folder: Path) -> list[dict]:
    """Each test_* function in tests/test_*.py, called with no arguments, the network blocked: {test, result
    ("passed" or "failed"), error}. The tool loads first, the way core loads it, so a broken tool fails here."""
    scaffold.load(name, folder)
    tests_dir = folder / "tests"
    results = []
    sys.path.insert(0, str(tests_dir))  # tests may import helpers next to them, as under pytest
    try:
        with offline():
            for path in sorted(tests_dir.glob("test_*.py")):
                results += _run_file(name, path)
    finally:
        sys.path.remove(str(tests_dir))
    return results


def _run_file(name: str, path: Path) -> list[dict]:
    module_name = f"_webcli_test_{name}_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        return [{"test": path.name, "result": "failed", "error": f"failed to import: {_error(exc, path)}"}]
    results = []
    for attr, test in vars(module).items():
        if not (attr.startswith("test_") and inspect.isfunction(test) and test.__module__ == module_name):
            continue
        row = {"test": f"{path.name}::{attr}", "result": "passed", "error": None}
        required = [p for p in inspect.signature(test).parameters.values() if p.default is inspect.Parameter.empty]
        if inspect.iscoroutinefunction(test):
            row.update(result="failed", error="async test: webcli test runs plain functions only")
        elif required:
            row.update(result="failed", error="takes arguments: webcli test has no pytest fixtures")
        else:
            try:
                test()
            except (Exception, SystemExit) as exc:
                row.update(result="failed", error=_error(exc, path))
        results.append(row)
    return results
