"""Identity grants: which of your identities a community tool may use besides its own.

A community tool always may use its own identities, <tool>:<account> (a tool can't be named after a built-in site or
provider, so those are never yours from elsewhere). Anything else, like google:default, needs a grant:
`webcli tools grant <tool> <identity>`. The daemon checks on every op (daemon.site_op).

    ~/.webcli/tools/grants.toml   (0600)
    sometool = ["google:default"]
"""

from __future__ import annotations

from pathlib import Path

from webcli.core import config, output
from webcli.core.errors import ConfigError
from webcli.core.identities import Identity
from webcli.core.manifest_schema import _NAME

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

FILE = "grants.toml"


def path() -> Path:
    return config.home() / "tools" / FILE


def list_grants() -> dict[str, list[str]]:
    """tool -> the identities granted to it. An unreadable file is a ConfigError (exit 7): no grant is assumed."""
    file = path()
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Can't read {file}: {exc}", hint=f"Fix or remove {file}.") from None
    for tool, ids in data.items():
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ConfigError(f"{file}: {tool} must be a list of identities.", hint=f"Fix or remove {file}.")
    return {tool: sorted(set(ids)) for tool, ids in sorted(data.items()) if ids}


def is_granted(tool: str, identity: str) -> bool:
    return identity in list_grants().get(tool, [])


def _save(grants: dict[str, list[str]]) -> None:
    config.private_dir(config.ensure_home() / "tools")
    output.write_atomic(path(), config.dumps({tool: ids for tool, ids in sorted(grants.items()) if ids}))


def grant(tool: str, identity: str) -> bool:
    """Let `tool` use `identity`. False if it already could."""
    if not _NAME.fullmatch(tool):
        raise ConfigError(f"{tool!r} is not a tool name.")
    identity = Identity(identity.strip()).id
    grants = list_grants()
    if identity in grants.get(tool, []):
        return False
    grants[tool] = sorted({*grants.get(tool, []), identity})
    _save(grants)
    return True


def revoke_grant(tool: str, identity: str | None = None) -> bool:
    """Take back `tool`'s grant of `identity` (None: all of its grants, for `webcli tools remove`). False if there
    was nothing to take back."""
    grants = list_grants()
    had = grants.get(tool, [])
    keep = [] if identity is None else [i for i in had if i != identity.strip()]
    if keep == had:
        return False
    grants[tool] = keep
    _save(grants)
    return True
