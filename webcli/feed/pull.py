"""`webcli feed pull`: run each site's own read-only command (`webcli <site> <read> --format json`, one at a time, a
child process each) and write one newsfeed.items/1 file, atomically.

Signed in first, the shared default: each adapter reads as its identity and falls back to its public feed only when
that identity is missing or expired (perplexity, gnews); its rows then say auth "public", and the file's sources
entry says auth "public" with needs_login true. An adapter with no public feed (youtube, linkedin) exits 4 signed
out: its entry is {site, status: "needs_login", hint}. A site that fails any other way is skipped and logged. Either
way the file is still written with what came back, and pull exits 5 (partial). A public fallback is not a failure
(it returned rows): it exits 0 and only the sources entry and a stderr hint say to log in.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from webcli.core import config
from webcli.feed.normalize import SCHEMA, imported, item, merge

SITES = ("youtube", "gnews", "perplexity", "linkedin", "x")
DEFAULT_SITES = ("youtube", "gnews", "perplexity", "linkedin")
DEFAULT_LIMIT = 20
SITE_TIMEOUT_S = 300
# site -> [(read label, argv after `webcli <site>`)]. {limit} and {since} are filled in.
READS = {
    "youtube": [("uploads", ["uploads", "--since", "{since}", "--limit", "{limit}"]),
                ("home", ["home", "--limit", "{limit}"])],
    "gnews": [("foryou", ["foryou", "--limit", "{limit}"])],
    "perplexity": [("discover", ["discover", "--limit", "{limit}"])],
    "linkedin": [("feed", ["feed", "--limit", "{limit}"])],
}
LIMIT_CAPS = {"youtube": 500, "gnews": 60, "perplexity": 100, "linkedin": 50}
LOGIN_HINTS = {"perplexity": "webcli perplexity login", "linkedin": "webcli identity login linkedin:default",
               "youtube": "webcli identity login google:default", "gnews": "webcli identity login google:default"}
EXIT_AUTH = 4


def feeds_dir() -> Path:
    return config.home() / "feeds"


def config_path() -> Path:
    return config.home() / "feed.toml"


def load_config(path: Path | None = None) -> dict:
    """feed.toml: sites = [...], limit_per_site = N, [youtube] since = "48h", [x] file = "...". Missing: {}."""
    path = path or config_path()
    if not path.exists():
        return {}
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    return data if isinstance(data, dict) else {}


def default_out(now: datetime | None = None) -> Path:
    now = now or datetime.now().astimezone()
    return feeds_dir() / f"{now:%Y-%m-%d-%H%M}.json"


def write_atomic(path: Path, doc: dict) -> Path:
    """Write via a temp file in the same directory + rename, 0600; the directory is created 0700."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(doc, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def latest(directory: Path | None = None) -> Path | None:
    files = sorted(p for p in (directory or feeds_dir()).glob("*.json") if not p.name.startswith("."))
    return files[-1] if files else None


Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _run(argv: list[str]) -> "subprocess.CompletedProcess[str]":
    env = {**os.environ, "WEBCLI_FORMAT": "json"}
    return subprocess.run([sys.executable, "-m", "webcli.apps.webcli", *argv], capture_output=True, text=True,
                          timeout=SITE_TIMEOUT_S, env=env)


def _error_text(stderr: str) -> str:
    for line in reversed((stderr or "").strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("error"):
            return str(data["error"])[:300]
    return (stderr or "").strip().splitlines()[-1][:300] if (stderr or "").strip() else "no output"


def pull_site(site: str, limit: int, since: str, run: Runner = _run) -> tuple[dict, list[dict]]:
    """({site, status, auth, needs_login, count, hint?, error?, reads}, items) for one site."""
    entry: dict = {"site": site, "status": "ok", "auth": "signed_in", "needs_login": False, "count": 0, "reads": []}
    items: list[dict] = []
    limit = max(1, min(limit, LIMIT_CAPS.get(site, limit)))
    for read, template in READS[site]:
        argv = [site, *(a.format(limit=limit, since=since) for a in template), "--format", "json"]
        try:
            done = run(argv)
        except subprocess.TimeoutExpired:
            entry["reads"].append({"read": read, "status": "error", "error": f"timed out after {SITE_TIMEOUT_S}s"})
            continue
        if done.returncode == EXIT_AUTH:
            entry["reads"].append({"read": read, "status": "needs_login"})
            continue
        if done.returncode == 3:  # nothing there: fine, zero rows
            entry["reads"].append({"read": read, "status": "ok", "count": 0})
            continue
        try:
            rows = json.loads(done.stdout) if done.returncode == 0 else None
        except ValueError:
            rows = None
        if not isinstance(rows, list):
            entry["reads"].append({"read": read, "status": "error", "error": _error_text(done.stderr)
                                   if done.returncode else "unreadable output"})
            continue
        got = [it for it in (item(site, read, r) for r in rows) if it]
        if any(it["meta"]["auth"] == "public" for it in got):
            entry["auth"], entry["needs_login"] = "public", True
        items += got
        entry["reads"].append({"read": read, "status": "ok", "count": len(got)})
    statuses = {r["status"] for r in entry["reads"]}
    if statuses == {"needs_login"}:
        entry.update(status="needs_login", auth=None, needs_login=True)
    elif "ok" not in statuses:
        entry["status"] = "error"
        entry["error"] = "; ".join(r.get("error", r["status"]) for r in entry["reads"])
    elif statuses != {"ok"}:
        entry["status"] = "partial"
        entry["needs_login"] = entry["needs_login"] or "needs_login" in statuses
    if entry["needs_login"]:
        entry["hint"] = LOGIN_HINTS.get(site, f"webcli identity login {site}:default")
    items = merge(items)
    entry["count"] = len(items)
    return entry, items


def pull_x(path: str | None) -> tuple[dict, list[dict]]:
    if not path:
        return {"site": "x", "status": "skipped", "reason": "no --x-file (x is imported, never read live)",
                "count": 0}, []
    file = Path(path).expanduser()
    try:
        items = imported(json.loads(file.read_text()))
    except FileNotFoundError:
        return {"site": "x", "status": "skipped", "reason": f"{file} is missing", "count": 0}, []
    except (ValueError, OSError) as exc:
        return {"site": "x", "status": "error", "error": f"{file.name}: {exc}", "count": 0}, []
    return {"site": "x", "status": "ok", "auth": "signed_in", "needs_login": False, "count": len(items),
            "file": str(file)}, items


def pull(sites: list[str], limit: int, *, since: str = "48h", x_file: str | None = None, run: Runner = _run,
         progress: Callable[[str], None] = lambda _m: None) -> dict:
    """The newsfeed.items/1 document for `sites`, in that order (never raises for one site's failure)."""
    entries, items = [], []
    for site in sites:
        progress(f"feed: reading {site}")
        entry, got = pull_x(x_file) if site == "x" else pull_site(site, limit, since, run)
        entries.append(entry)
        items += got
        progress(f"feed: {site} {entry['status']} ({entry['count']})")
    return {"schema": SCHEMA, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")
            .replace("+00:00", "Z"), "source": "webcli feed", "sources": entries, "items": items}


def partial(doc: dict) -> bool:
    """True when any site (or one of its reads) failed or needed a login and returned nothing. A public fallback
    returned rows, so it isn't partial."""
    return any(e["status"] not in ("ok", "skipped") for e in doc["sources"])
