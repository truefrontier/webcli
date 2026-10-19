"""The one data home, ~/.webcli (or $WEBCLI_HOME), and its config.toml.

    ~/.webcli/
      config.toml
      identities/<provider>-<account>/chrome-profile/   one logged-in browser profile per identity
      run/        0700: <identity>.sock daemon sockets (0600), profile and serve locks
      logs/       redacted, rotated
      sites/<site>/   per-site state (last_chat, captures)

Everything is created lazily. The home itself gets the H4 hardening (hardening.apply) the first time it is created.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from webcli.core import hardening
from webcli.core.errors import ConfigError

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

HOME_ENV = "WEBCLI_HOME"
CONFIG_NAME = "config.toml"


def home() -> Path:
    """The data home's path (not created)."""
    return Path(os.environ.get(HOME_ENV) or Path.home() / ".webcli").expanduser()


def private_dir(path: Path) -> Path:
    """mkdir -p with mode 0700 for `path` and every parent it creates (mkdir's own parents=True leaves those at
    the umask's mode), and fix `path`'s mode if it already existed (umask-proof). A path under a home that doesn't
    exist yet creates the home first, through ensure_home, so its H4 hardening is never skipped."""
    root = home()
    if root in path.parents and not root.exists():
        ensure_home()
    missing = []
    while not path.exists() and path != path.parent:
        missing.append(path)
        path = path.parent
    for created in reversed(missing):
        created.mkdir(mode=0o700, exist_ok=True)
        created.chmod(0o700)
    path = missing[0] if missing else path
    path.chmod(0o700)
    return path


def ensure_home() -> Path:
    """The data home, created (0700) on first use; a fresh home gets the H4 hardening."""
    root = home()
    fresh = not root.exists()
    private_dir(root)
    if fresh:
        hardening.apply(root)
    return root


def run_dir() -> Path:
    return private_dir(ensure_home() / "run")


def logs_dir() -> Path:
    return private_dir(ensure_home() / "logs")


def site_dir(site: str) -> Path:
    return private_dir(ensure_home() / "sites" / site)


def load() -> dict[str, Any]:
    """config.toml as a dict ({} when there is none). Unreadable or invalid TOML is a ConfigError (exit 7)."""
    path = home() / CONFIG_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError(f"Can't read {path}: {exc}") from None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}", hint=f"Fix or remove {path}.") from None


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(key: str) -> str:
    return key if _BARE_KEY.match(key) else '"' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    raise ConfigError(f"config.toml can't hold a {type(value).__name__}")


def dumps(data: dict[str, Any]) -> str:
    """TOML for config.toml's shapes: scalars and lists at the top, then tables of them, one level of nesting."""
    lines = [f"{_key(k)} = {_value(v)}" for k, v in data.items() if not isinstance(v, dict)]

    def table(prefix: list[str], body: dict) -> None:
        scalars = {k: v for k, v in body.items() if not isinstance(v, dict)}
        if scalars or not body:
            lines.extend(["", f"[{'.'.join(_key(p) for p in prefix)}]"])
            lines.extend(f"{_key(k)} = {_value(v)}" for k, v in scalars.items())
        for k, v in body.items():
            if isinstance(v, dict):
                table([*prefix, k], v)

    for k, v in data.items():
        if isinstance(v, dict):
            table([k], v)
    return "\n".join(lines).lstrip("\n") + "\n"


def save(data: dict[str, Any]) -> Path:
    """Write config.toml atomically (0600)."""
    from webcli.core.output import write_atomic

    path = ensure_home() / CONFIG_NAME
    write_atomic(path, dumps(data))
    return path
