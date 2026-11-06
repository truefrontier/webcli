"""The webcli-tool.toml manifest: load it, check it, and return it as a Manifest. The one validator `webcli new`,
`webcli lint`, `webcli tools install`, `webcli submit`, core's tool loader, and the library's CI all call, so a
manifest means the same thing everywhere. The rules, in plain words: docs/webcli-tool-manifest.md.

The manifest has no schema version of its own: its `adapter_api` says which adapter API (webcli.adapter) it was
written for, and a change to these rules that breaks a valid manifest bumps ADAPTER_API.

A command's name is its path under the tool's group, words split by one space (`webcli <tool> label add` is
"label add"); Site.verbs, Site.writes, and Site.sends_to_people use the same names. The request checks catch honest
mistakes, nothing more: a manifest that passes proves nothing about what the code does. Core's own checks at run
time (the host envelope, write gating) are what hold a tool to it. Only the form of requires_webcli is checked here;
whether the running webcli satisfies it is checked where a tool is installed or loaded.

    python -m webcli.core.manifest_schema path/to/webcli-tool.toml ...   # exit 0 if every file is valid, else 7
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from webcli.adapter import (
    NO_IDENTITY,
    SUPPORTED_ADAPTER_APIS,
    Site,
    Strategy,
    host_matches,
)
from webcli.core.errors import ConfigError

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

MANIFEST_NAME = "webcli-tool.toml"
DOCS = "https://github.com/truefrontier/webcli/blob/main/docs/webcli-tool-manifest.md"
# Top-level `webcli` commands a tool can't be named after (it would be hidden behind the command). Built-in site
# names are refused too (see _reserved).
CORE_COMMANDS = frozenset({"sites", "feed", "identity", "doctor", "serve", "agent-help",
                           "new", "dev", "test", "lint", "tools", "submit"})

# A tool's name is also its identity provider (<name>:default), so it follows identities.py's provider rule.
_NAME = re.compile(r"[a-z][a-z0-9]{1,31}")
_PROVIDER = re.compile(r"[a-z][a-z0-9]{0,31}")
_VERSION = re.compile(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?")
_SPECIFIER = re.compile(r" *(?:~=|===?|!=|<=|>=|<|>) *[0-9][0-9A-Za-z.*+!]* *"
                        r"(?:, *(?:~=|===?|!=|<=|>=|<|>) *[0-9][0-9A-Za-z.*+!]* *)*")
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST = re.compile(rf"(?:\*\.)?{_LABEL}(?:\.{_LABEL})+")
_MAINTAINER = re.compile(r"github:[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_COMMAND = re.compile(r"[a-z][a-z0-9-]*(?: [a-z][a-z0-9-]*)?")
METHODS = ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE")
WRITE_METHODS = ("PUT", "PATCH", "DELETE")

TOP_KEYS = {"name", "version", "adapter_api", "requires_webcli", "summary", "identity", "hosts", "maintainers",
            "license", "commands"}
REQUIRED_KEYS = TOP_KEYS - {"requires_webcli", "identity"}
COMMAND_KEYS = {"name", "strategy", "writes", "sends_to_people", "requests"}


class ManifestError(ConfigError):
    """A manifest that can't be read or breaks the rules: exit 7. `problems` lists every rule it breaks, one line
    each."""

    def __init__(self, source: str, problems: list[str]) -> None:
        self.source, self.problems = source, problems
        super().__init__(f"{source} is not a valid tool manifest:\n" + "\n".join(f"  - {p}" for p in problems),
                         hint=f"The rules: {DOCS}")


@dataclass(frozen=True)
class Command:
    name: str  # as typed after the tool's name: "list", or "label add"
    strategy: Strategy
    writes: bool = False
    sends_to_people: bool = False
    requests: tuple[str, ...] = ()  # "POST www.loom.com/graphql op=GetLoomsForLibrary (query)"


@dataclass(frozen=True)
class Manifest:
    name: str
    version: str
    adapter_api: int
    summary: str
    identity: str  # the identity provider ("loom"), or NO_IDENTITY for a tool that never signs in
    hosts: tuple[str, ...]
    maintainers: tuple[str, ...]
    license: str
    commands: tuple[Command, ...]
    requires_webcli: str = ""

    @property
    def writes(self) -> frozenset[str]:
        return frozenset(c.name for c in self.commands if c.writes)

    @property
    def sends_to_people(self) -> frozenset[str]:
        return frozenset(c.name for c in self.commands if c.sends_to_people)

    @property
    def default_identity(self) -> str | None:
        return None if self.identity == NO_IDENTITY else f"{self.identity}:default"


def _reserved() -> frozenset[str]:
    # Imported here, not at the top: the site registry may load manifests itself.
    from webcli.sites import BUILTIN

    return CORE_COMMANDS | frozenset(BUILTIN)


def _text(data: dict, key: str, problems: list[str], where: str = "") -> str | None:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or "\n" in value:
        problems.append(f"{where}{key}: must be one line of text")
        return None
    return value


def _strings(data: dict, key: str, problems: list[str], *, empty_ok: bool = False, where: str = "") -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        problems.append(f"{where}{key}: must be a list of strings")
        return []
    if not value and not empty_ok:
        problems.append(f"{where}{key}: must list at least one")
    return value


def _command(raw: Any, n: int, hosts: list[str], problems: list[str]) -> Command | None:
    if not isinstance(raw, dict):
        problems.append(f"command #{n}: must be a [[commands]] table")
        return None
    name = raw.get("name")
    where = f'command "{name}": ' if isinstance(name, str) else f"command #{n}: "
    before = len(problems)
    problems.extend(f"{where}unknown key {k!r}" for k in sorted(set(raw) - COMMAND_KEYS))
    if not isinstance(name, str) or not _COMMAND.fullmatch(name):
        problems.append(f"{where}name: must be one or two lowercase words (\"list\", \"label add\")")
    strategies = [s.value for s in Strategy]
    strategy = raw.get("strategy")
    if strategy not in strategies:
        problems.append(f"{where}strategy: must be one of {', '.join(strategies)}")
    flags = {}
    for key in ("writes", "sends_to_people"):
        flags[key] = raw.get(key, False)
        if not isinstance(flags[key], bool):
            problems.append(f"{where}{key}: must be true or false")
    if flags["sends_to_people"] is True and flags["writes"] is not True:
        problems.append(f"{where}sends_to_people = true needs writes = true (sending to a person is a write)")
    requests = _strings(raw, "requests", problems, empty_ok=True, where=where)
    for request in requests:
        parts = request.split()
        method, target = (parts[0], parts[1]) if len(parts) >= 2 else ("", "")
        host = target.split("/", 1)[0]
        if method not in METHODS or not host:
            problems.append(f"{where}request {request!r}: must start with a method and host/path "
                            "(\"GET www.example.com/api/items\")")
        elif not any(host_matches(pattern, host) for pattern in hosts):
            problems.append(f"{where}request {request!r}: host {host} is not in hosts")
        elif flags["writes"] is False and (method in WRITE_METHODS or "(mutation)" in request):
            problems.append(f"{where}request {request!r}: a read command (writes = false) can't make a write "
                            "request; mark the command writes = true")
    if len(problems) > before:
        return None
    return Command(name=name, strategy=Strategy(strategy), writes=flags["writes"],
                   sends_to_people=flags["sends_to_people"], requests=tuple(requests))


def validate(data: dict, source: str = MANIFEST_NAME) -> Manifest:
    """The Manifest for `data` (a parsed webcli-tool.toml), or ManifestError listing every rule it breaks."""
    problems: list[str] = []
    problems.extend(f"unknown key {k!r}" for k in sorted(set(data) - TOP_KEYS))
    problems.extend(f"{k}: missing" for k in sorted(REQUIRED_KEYS - set(data)))

    name = data.get("name")
    if "name" in data:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            problems.append("name: must be 2-32 lowercase letters and digits, starting with a letter (no hyphens: "
                            "it is also the tool's identity, <name>:default)")
        elif name in _reserved():
            problems.append(f"name: {name!r} is a built-in site or a webcli command; pick another name")
    version = data.get("version")
    if "version" in data and (not isinstance(version, str) or not _VERSION.fullmatch(version)):
        problems.append("version: must be X.Y.Z (\"0.1.0\"), optionally with a pre-release (\"0.2.0rc1\")")
    api = data.get("adapter_api")
    if "adapter_api" in data and (type(api) is not int or api not in SUPPORTED_ADAPTER_APIS):
        problems.append(f"adapter_api: this webcli supports {', '.join(map(str, SUPPORTED_ADAPTER_APIS))}")
    requires = data.get("requires_webcli", "")
    if not isinstance(requires, str) or (requires and not _SPECIFIER.fullmatch(requires)):
        problems.append("requires_webcli: must be a version range (\">=1.0,<2\")")
    summary = _text(data, "summary", problems) if "summary" in data else None
    license_ = _text(data, "license", problems) if "license" in data else None
    identity = data.get("identity", name)
    if "identity" in data and (not isinstance(identity, str) or not _PROVIDER.fullmatch(identity)):
        problems.append("identity: must be a provider name (\"loom\", or \"none\" for no sign-in), not "
                        "provider:account")

    hosts = _strings(data, "hosts", problems) if "hosts" in data else []
    for host in hosts:
        if not _HOST.fullmatch(host):
            problems.append(f"hosts: {host!r} is not a lowercase host name (\"www.example.com\", "
                            "\"*.example.com\"; no scheme, port, or path)")
    problems.extend(f"hosts: {h!r} is listed twice" for h in sorted({h for h in hosts if hosts.count(h) > 1}))
    maintainers = _strings(data, "maintainers", problems) if "maintainers" in data else []
    problems.extend(f"maintainers: {m!r} must be github:<login>"
                    for m in maintainers if not _MAINTAINER.fullmatch(m))

    commands: list[Command] = []
    raw_commands = data.get("commands", [])
    if not isinstance(raw_commands, list):
        problems.append("commands: must be [[commands]] tables")
    elif "commands" in data and not raw_commands:
        problems.append("commands: must list at least one")
    else:
        for n, raw in enumerate(raw_commands, 1):
            command = _command(raw, n, hosts, problems)
            if command:
                commands.append(command)
        names = [c.name for c in commands]
        problems.extend(f'command "{n}": listed twice' for n in sorted({n for n in names if names.count(n) > 1}))

    if problems:
        raise ManifestError(source, problems)
    return Manifest(name=name, version=version, adapter_api=api, summary=summary, identity=identity,
                    hosts=tuple(hosts), maintainers=tuple(maintainers), license=license_,
                    commands=tuple(commands), requires_webcli=requires)


def mismatches(manifest: Manifest, site: Site) -> list[str]:
    """How a tool's loaded ADAPTER differs from its manifest, one line each ([] when they agree). The manifest is
    what a person agreed to at install, so it is the authority: a tool whose Site says anything else isn't
    loaded."""
    verbs = {name: strategy.value for name, strategy in site.verbs.items()}
    expected = {
        "name": (site.name, manifest.name),
        "provider (identity)": (site.provider, manifest.identity),
        "adapter_api": (site.adapter_api, manifest.adapter_api),
        "version": (site.version, manifest.version),
        "hosts": (sorted(site.hosts), sorted(manifest.hosts)),
        "verbs (commands)": (verbs, {c.name: c.strategy.value for c in manifest.commands}),
        "writes": (sorted(site.writes), sorted(manifest.writes)),
        "sends_to_people": (sorted(site.sends_to_people), sorted(manifest.sends_to_people)),
    }
    return [f"{field}: the adapter says {got!r}, the manifest {want!r}"
            for field, (got, want) in expected.items() if got != want]


def loads(text: str, source: str = MANIFEST_NAME) -> Manifest:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(source, [f"not valid TOML: {exc}"]) from None
    return validate(data, source)


def load(path: Path | str) -> Manifest:
    """The manifest at `path` (a webcli-tool.toml, or the folder holding one)."""
    path = Path(path)
    if path.is_dir():
        path = path / MANIFEST_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(str(path), [f"can't read it: {exc}"]) from None
    return loads(text, str(path))


def main(argv: list[str] | None = None) -> int:
    paths = sys.argv[1:] if argv is None else argv
    if not paths:
        print(f"usage: python -m webcli.core.manifest_schema {MANIFEST_NAME} ...", file=sys.stderr)
        return 2
    code = 0
    for path in paths:
        try:
            manifest = load(path)
        except ManifestError as exc:
            print(exc, file=sys.stderr)
            code = 7
        else:
            print(f"ok {path} ({manifest.name} {manifest.version})")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
