"""The webcli-tools library's index.json: fetch it, cache it, and download a tool at its pinned commit.

    index.json (docs/webcli-tools-index.md has the format)
      {"tools": [{"name", "version", "summary", "commit", "sha256", "path", "verified"}       a community tool
                 | {"name", "version", "summary", "builtin": true, "tier": "official",
                    "identity", "hosts", "commands", ...}],                                   an official built-in
       "revoked": [name | {"name", "versions", "reason"}]}

An official built-in ships inside webcli. Its entry is a catalog entry, so `webcli tools search` and `info` can show
it: it has no commit, sha256, or path, and nothing downloads it.

The index is the trust root: it pins each community tool to a commit and to the sha256 of its files (tree_sha256), and
it carries the revoked list core reads from the cache (core/revoked.py). A fetched index that isn't the right shape
is refused and the old cache kept, so a broken index never clears a revocation.

$WEBCLI_TOOLS_INDEX_URL points at another index (any URL urllib opens, file:// too) and $WEBCLI_TOOLS_ARCHIVE_URL at
another repo's tarballs ({commit} in it): a fork, or a test's fake.

    python -m webcli.core.index_client sha256 tools/<name>   # the hash index.json pins, for the library's CI
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import tarfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from webcli import __version__
from webcli.core import config, output
from webcli.core.errors import ConfigError
from webcli.core.manifest_schema import _NAME, _VERSION

URL_ENV = "WEBCLI_TOOLS_INDEX_URL"
ARCHIVE_ENV = "WEBCLI_TOOLS_ARCHIVE_URL"
DEFAULT_URL = "https://raw.githubusercontent.com/truefrontier/webcli-tools/main/index.json"
DEFAULT_REPO = "https://github.com/truefrontier/webcli-tools"
CORE_REPO = "https://github.com/truefrontier/webcli"
DEFAULT_ARCHIVE = "https://codeload.github.com/truefrontier/webcli-tools/tar.gz/{commit}"
CACHE = "index-cache.json"
INDEX_LIMIT = 5_000_000
ARCHIVE_LIMIT = 50_000_000  # the download, and separately what one tool's files expand to
# The whole archive, the library's other folders included: tarfile reads (and keeps) every member's header, and
# skipping a member still decompresses it. So a 50 MB download can't make install hold millions of headers or
# inflate gigabytes it never writes.
MEMBER_LIMIT = 20_000
UNPACKED_LIMIT = 250_000_000
TIMEOUT = 30
_HEX = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class LibraryError(ConfigError):
    """The library or its download can't be used (unreachable, the wrong shape, a hash that doesn't match): exit 7."""


@dataclass(frozen=True)
class Entry:
    name: str
    version: str
    summary: str
    commit: str = ""  # 40-hex: never a branch or tag. "" for a built-in
    sha256: str = ""  # tree_sha256 of the tool's folder at that commit
    path: str = ""  # the tool's folder in the repo: tools/<name>
    verified: bool = False
    # An official built-in: listed so people can find it, never downloaded. Only a built-in sets these.
    builtin: bool = False
    requires_webcli: str = ""  # the webcli releases that ship it
    identity: str = ""  # its provider ("google"), or "none"
    hosts: tuple[str, ...] = ()
    commands: tuple[tuple[str, str, bool], ...] = ()  # (name, strategy, writes)
    docs: str = ""


@dataclass(frozen=True)
class Index:
    url: str
    tools: dict[str, Entry]
    data: dict  # the whole index as fetched: what the cache holds


def url() -> str:
    return os.environ.get(URL_ENV, "").strip() or DEFAULT_URL


def archive_url(entry: Entry) -> str:
    if entry.builtin:
        raise LibraryError(f"{entry.name} is built into webcli; there's nothing to download.")
    return (os.environ.get(ARCHIVE_ENV, "").strip() or DEFAULT_ARCHIVE).replace("{commit}", entry.commit)


def source(entry: Entry) -> str:
    """Where a person can read the tool's code at its pinned commit."""
    if entry.builtin:
        return entry.docs or CORE_REPO
    if os.environ.get(ARCHIVE_ENV, "").strip():
        return f"{archive_url(entry)} ({entry.path}/)"
    return f"{DEFAULT_REPO}/tree/{entry.commit}/{entry.path}"


def compare(old_commit: str | None, entry: Entry) -> str | None:
    """A link to what changed between two pinned commits, for the library's own repo."""
    if not old_commit or os.environ.get(ARCHIVE_ENV, "").strip():
        return None
    return f"{DEFAULT_REPO}/compare/{old_commit}...{entry.commit}"


def cache_path() -> Path:
    return config.home() / "tools" / CACHE


def _get(target: str, limit: int) -> bytes:
    request = urllib.request.Request(target, headers={"User-Agent": f"webcli/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read(limit + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LibraryError(f"Can't download {target}: {getattr(exc, 'reason', exc)}",
                          hint="Check your connection, or $" + URL_ENV + ".") from None
    if len(body) > limit:
        raise LibraryError(f"{target} is over {limit // 1_000_000} MB; not using it.")
    return body


def _revoked_ok(items: object) -> bool:
    """Names, or {"name", "versions": ["0.1.0", ...]}: what core/revoked.py reads, held stricter (real tool names and
    versions only), since an entry that can never match would quietly un-revoke."""
    def ok(item: object) -> bool:
        if isinstance(item, str):
            return bool(_NAME.fullmatch(item))
        versions = item.get("versions", []) if isinstance(item, dict) else None
        return isinstance(item, dict) and isinstance(item.get("name"), str) and bool(_NAME.fullmatch(item["name"])) \
            and isinstance(versions, list) and all(isinstance(v, str) and _VERSION.fullmatch(v) for v in versions)

    return isinstance(items, list) and all(ok(i) for i in items)


def parse(data: object, source: str) -> Index:
    """The Index for a parsed index.json, or LibraryError naming what's wrong with it."""
    def bad(why: str) -> LibraryError:
        return LibraryError(f"{source} is not a valid webcli-tools index: {why}",
                           hint=f"Report it at {DEFAULT_REPO}/issues, or point ${URL_ENV} at another index.")

    if not isinstance(data, dict) or not isinstance(data.get("tools"), list):
        raise bad('it needs a "tools" list')
    if "revoked" not in data or not _revoked_ok(data["revoked"]):  # required: a cut-off index must not un-revoke
        raise bad('it needs a "revoked" list of names or {"name", "versions": [strings]}')
    tools: dict[str, Entry] = {}
    for raw in data["tools"]:
        name = raw.get("name") if isinstance(raw, dict) else None
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in tools:
            raise bad(f"tool {name!r}: needs a unique name (lowercase letters and digits, no hyphens)")
        version_ok = isinstance(raw.get("version"), str) and _VERSION.fullmatch(raw["version"])
        if raw.get("builtin") is True:
            hosts, commands = raw.get("hosts"), raw.get("commands")
            checks = {"version": version_ok,
                      "summary": isinstance(raw.get("summary", ""), str),
                      "tier": raw.get("tier") == "official",
                      "identity": isinstance(raw.get("identity"), str) and _NAME.fullmatch(raw["identity"]),
                      "hosts": isinstance(hosts, list) and all(isinstance(h, str) for h in hosts),
                      "commands": isinstance(commands, list) and all(
                          isinstance(c, dict) and isinstance(c.get("name"), str) and c["name"]
                          and isinstance(c.get("strategy"), str) and isinstance(c.get("writes"), bool)
                          for c in commands),
                      "requires_webcli": isinstance(raw.get("requires_webcli", ""), str),
                      "docs": isinstance(raw.get("docs", ""), str),
                      "verified": isinstance(raw.get("verified", False), bool),
                      "commit, sha256, or path (a built-in has none)": not {"commit", "sha256", "path"} & raw.keys()}
            wrong = [key for key, ok in checks.items() if not ok]
            if wrong:
                raise bad(f"built-in {name!r}: bad {', '.join(wrong)}")
            tools[name] = Entry(name=name, version=raw["version"], summary=raw.get("summary", ""),
                                verified=raw.get("verified", False), builtin=True,
                                requires_webcli=raw.get("requires_webcli", ""), identity=raw["identity"],
                                hosts=tuple(hosts), docs=raw.get("docs", ""),
                                commands=tuple((c["name"], c["strategy"], c["writes"]) for c in commands))
            continue
        path = raw.get("path", f"tools/{name}")
        parts = PurePosixPath(path).parts if isinstance(path, str) else ()
        checks = {"version": version_ok,
                  "summary": isinstance(raw.get("summary", ""), str),
                  "commit": isinstance(raw.get("commit"), str) and _HEX.fullmatch(raw["commit"]),
                  "sha256": isinstance(raw.get("sha256"), str) and _SHA256.fullmatch(raw["sha256"]),
                  "path": parts and not path.startswith("/") and ".." not in parts,
                  "verified": isinstance(raw.get("verified", False), bool),
                  "tier": raw.get("tier", "community") == "community",
                  "builtin": raw.get("builtin", False) is False}
        wrong = [key for key, ok in checks.items() if not ok]
        if wrong:
            raise bad(f"tool {name!r}: bad {', '.join(wrong)} (commit: 40 hex digits; sha256: 64)")
        tools[name] = Entry(name=name, version=raw["version"], summary=raw.get("summary", ""),
                            commit=raw["commit"], sha256=raw["sha256"], path=str(PurePosixPath(path)),
                            verified=raw.get("verified", False))
    return Index(url=source, tools=tools, data=data)


def fetch(*, cached_ok: bool = True) -> Index:
    """The library's index, fresh, and saved as the cache. Offline (or a broken index) with cached_ok: the cached
    copy, with a warning. A broken index never replaces the cache."""
    source = url()
    try:
        body = _get(source, INDEX_LIMIT)
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise LibraryError(f"{source} is not JSON: {exc}") from None
        index = parse(data, source)
    except LibraryError as exc:
        old = cached() if cached_ok else None
        if old is None:
            raise
        output.warn(f"webcli: {exc} Using the copy cached {_age(cache_path())}.")
        return old
    config.private_dir(config.ensure_home() / "tools")
    output.write_atomic(cache_path(), json.dumps(data, indent=1) + "\n")
    return index


def cached() -> Index | None:
    """The cached index, or None when there's none (or it can't be read: revoked.py fails closed on that file)."""
    try:
        return parse(json.loads(cache_path().read_text(encoding="utf-8")), str(cache_path()))
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _age(path: Path) -> str:
    import time

    try:
        hours = (time.time() - path.stat().st_mtime) / 3600
    except OSError:
        return "earlier"
    return f"{hours:.0f} hours ago" if hours >= 1 else "less than an hour ago"


def tree_sha256(folder: Path, *, added: tuple[str, ...] = ()) -> str:
    """sha256 over "<path>\\0<sha256 of its bytes>\\n" for every file under `folder`, sorted by path ("/"-separated,
    relative to folder). Links and other special files are refused: an installed tool is regular files only.
    With `added` (the top-level files install puts in a tool's folder), it hashes an installed tool as it was
    downloaded: without those, and without the __pycache__/ folders Python adds."""
    files = []
    for path in folder.rglob("*"):
        rel = path.relative_to(folder).as_posix()
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise LibraryError(f"{folder}: {rel} is a link or special file; a tool holds regular files only.")
        if added and (rel in added or "__pycache__" in path.relative_to(folder).parts):
            continue
        if path.is_file():
            files.append((rel, path))
    digest = hashlib.sha256()
    for rel, path in sorted(files):
        digest.update(f"{rel}\0{hashlib.sha256(path.read_bytes()).hexdigest()}\n".encode())
    return digest.hexdigest()


def download(entry: Entry, dest: Path) -> None:
    """Put `entry`'s folder, at its pinned commit, into `dest` (new, empty) and check it against entry.sha256. Raises
    LibraryError (and leaves dest for the caller to remove) when anything is off. The hash is checked on what landed
    on disk, so duplicate or case-colliding members can only fail it, never slip past it."""
    target = archive_url(entry)
    body = _get(target, ARCHIVE_LIMIT)
    prefix = PurePosixPath(entry.path)
    count = size = members = unpacked = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:*") as tar:
            for member in tar:
                # Checked before the loop moves on, which is when tarfile reads past this member's data.
                members += 1
                unpacked += member.size
                if members > MEMBER_LIMIT:
                    raise LibraryError(f"{target} holds more than {MEMBER_LIMIT} files; not installing it.")
                if unpacked > UNPACKED_LIMIT:
                    raise LibraryError(f"{target} expands past {UNPACKED_LIMIT // 1_000_000} MB; not installing it.")
                parts = PurePosixPath(member.name).parts[1:]  # the archive's top folder: <repo>-<commit>/
                if len(parts) <= len(prefix.parts) or PurePosixPath(*parts[:len(prefix.parts)]) != prefix:
                    continue
                rel = parts[len(prefix.parts):]
                if member.isdir():
                    continue
                if not member.isfile() or ".." in rel or rel[0].startswith("/"):
                    raise LibraryError(f"{entry.path}/{'/'.join(rel)} in {target} is a link or special file; "
                                      "not installing it.")
                size += member.size
                if size > ARCHIVE_LIMIT:
                    raise LibraryError(f"{entry.path}/ in {target} expands past {ARCHIVE_LIMIT // 1_000_000} MB; "
                                      "not installing it.")
                out = dest.joinpath(*rel)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(tar.extractfile(member).read())
                count += 1
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise LibraryError(f"Can't read {target}: {exc}") from None
    if not count:
        raise LibraryError(f"{target} has no {entry.path}/.", hint="The index may point at the wrong commit.")
    got = tree_sha256(dest)
    if got != entry.sha256:
        raise LibraryError(f"{entry.name} {entry.version} at {entry.commit[:12]} doesn't match the index's sha256 "
                          f"(got {got[:16]}…, want {entry.sha256[:16]}…). Not installing it.",
                          hint="The download or the library changed under the index. Try again later, and report "
                               f"it at {DEFAULT_REPO}/issues if it keeps happening.")


def main(argv: list[str] | None = None) -> int:
    """Hash a folder the way install does. Point it at a `git archive` export, not a checkout, so export-ignore,
    export-subst, and eol attributes can't make the two differ."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 2 or args[0] != "sha256":
        print("usage: python -m webcli.core.index_client sha256 <folder> ...", file=sys.stderr)
        return 2
    for folder in args[1:]:
        print(f"{tree_sha256(Path(folder))}  {folder}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
