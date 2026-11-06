"""Making a webcli tool: the folder `webcli new` writes, the dev.toml list of tools being written, and loading one
from its folder for `webcli dev`, `test`, and `lint`.

A tool folder (the layout the library and core's tool loader expect, docs/tool-trust.md):

    webcli-tool-<name>/
      webcli-tool.toml          the manifest (core/manifest_schema.py)
      webcli_tool_<name>/       the package: __init__ (ADAPTER), cli, endpoints, fetch, guard, parse
      tests/                    offline tests, and fixtures/*.json page snapshots
      conftest.py, README.md

~/.webcli/tools/dev.toml lists the tools being written, one table each: [<name>] path = "/abs/webcli-tool-<name>".
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from webcli.core import config, manifest_schema
from webcli.core.errors import ConfigError, UsageError
from webcli.core.output import write_atomic
from webcli.sites.base import Site

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

TEMPLATES = Path(__file__).parent / "templates" / "tool"
DEV_FILE = "dev.toml"
INDEX_CACHE = "index-cache.json"
PLACEHOLDER_URL = "https://www.example.com/"
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")  # {{name}}; one render() doesn't fill is a KeyError


def package(name: str) -> str:
    return f"webcli_tool_{name}"


def folder_name(name: str) -> str:
    return f"webcli-tool-{name}"


def tools_dir() -> Path:
    return config.home() / "tools"


def parse_url(url: str) -> tuple[str, str]:
    """(host, path) of an https URL a tool may start from, or UsageError."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" or not host or parts.port or "@" in parts.netloc or any(c in url for c in "\"\\ "):
        raise UsageError(f"--url must be a plain https:// URL (no port, user, quotes, or spaces), not {url!r}.")
    return host, parts.path or "/"


def render(*, name: str, provider: str, url: str, hosts: list[str]) -> dict[str, str]:
    """Every file of a new tool folder: relative path -> text."""
    from webcli.core.capture import PAGE_JS

    host, path = parse_url(url)
    values = {"name": name, "provider": provider, "url": url, "host": host, "path": path,
              "hosts_toml": ", ".join(json.dumps(h) for h in hosts),
              "hosts_py": "".join(f"{json.dumps(h)}, " for h in hosts).rstrip(),
              "login": "_login" if provider == name else "None", "page_js": PAGE_JS}
    files = {}
    for template in sorted(TEMPLATES.rglob("*.tmpl")):
        rel = template.relative_to(TEMPLATES).with_suffix("").as_posix()
        if rel.startswith("pkg/"):
            rel = package(name) + rel[3:]
        files[rel] = _PLACEHOLDER.sub(lambda m: values[m[1]], template.read_text(encoding="utf-8"))
    return files


def write(folder: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def dev_tools() -> dict[str, Path]:
    """dev.toml: name -> folder ({} when there is none). An unreadable file is a ConfigError."""
    path = tools_dir() / DEV_FILE
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Can't read {path}: {exc}", hint=f"Fix or remove {path}.") from None
    return {name: Path(entry["path"]).expanduser() for name, entry in data.items()
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)}


def register(name: str, folder: Path) -> Path:
    """Add (or move) [name] in dev.toml, keeping the rest. Returns dev.toml's path."""
    tools = {n: {"path": str(p)} for n, p in dev_tools().items()}
    tools[name] = {"path": str(folder.resolve())}
    path = config.private_dir(config.ensure_home() / "tools") / DEV_FILE
    write_atomic(path, config.dumps(dict(sorted(tools.items()))))
    return path


def library_names() -> set[str]:
    """Tool names in the cached library index (`webcli tools` keeps it), if there is one. Never fetched here."""
    try:
        data = json.loads((tools_dir() / INDEX_CACHE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    tools = data.get("tools") if isinstance(data, dict) else None
    if isinstance(tools, dict):
        return set(tools)
    return {t["name"] for t in tools or [] if isinstance(t, dict) and isinstance(t.get("name"), str)}


def find(target: str) -> tuple[str, Path]:
    """(name, folder) for `webcli dev|test|lint TARGET`: a tool name in dev.toml, or a tool folder's path (the
    library's CI lints tools/<name>/ that way)."""
    path = Path(target).expanduser()
    if ("/" in target or target.startswith(".")) or (path.is_dir() and (path / manifest_schema.MANIFEST_NAME).exists()):
        if not path.is_dir():
            raise ConfigError(f"{target} is not a folder.")
        try:
            return manifest_schema.load(path).name, path.resolve()
        except manifest_schema.ManifestError:
            return path.resolve().name.removeprefix("webcli-tool-"), path.resolve()
    tools = dev_tools()
    if target not in tools:
        raise ConfigError(f"No tool named {target!r} in {tools_dir() / DEV_FILE}.",
                          hint=f"Make one with `webcli new {target}`, or pass the tool's folder.")
    if not tools[target].is_dir():
        raise ConfigError(f"{target}'s folder {tools[target]} is gone.",
                          hint=f"Fix its path in {tools_dir() / DEV_FILE}, or make it again with `webcli new`.")
    return target, tools[target]


def import_tool(name: str, folder: Path) -> object:
    """webcli_tool_<name>.ADAPTER, imported from `folder` by path (never via sys.path, so a stray json.py next to it
    can't shadow anything). Any earlier copy is dropped first, so edits are picked up. The same way core's tool
    loader imports a tool."""
    pkg = package(name)
    init = folder / pkg / "__init__.py"
    if not init.is_file():
        raise ConfigError(f"{folder} has no {pkg}/__init__.py.")
    for loaded in [m for m in sys.modules if m == pkg or m.startswith(pkg + ".")]:
        del sys.modules[loaded]
    spec = importlib.util.spec_from_file_location(pkg, init, submodule_search_locations=[str(init.parent)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[pkg] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(pkg, None)
        raise
    if not hasattr(module, "ADAPTER"):
        raise ConfigError(f"{pkg} has no ADAPTER.")
    return module.ADAPTER


def load(name: str, folder: Path) -> tuple[manifest_schema.Manifest, Site]:
    """The tool's manifest and Site, checked the way core checks an installed tool, or ConfigError."""
    manifest = manifest_schema.load(folder)
    if manifest.name != name:
        raise ConfigError(f"{folder}'s manifest is named {manifest.name!r}, not {name!r}.")
    try:
        site = import_tool(name, folder)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"{package(name)} failed to import: {type(exc).__name__}: {exc}") from exc
    if not isinstance(site, Site) or site.name != name:
        raise ConfigError(f"{package(name)}.ADAPTER is not a Site named {name!r}.")
    if site.kind != "data" or site.llm_cli is not None:
        raise ConfigError("A tool is a webcli data site: kind \"data\", no llm_cli.")
    if site.fetch.split(".")[0] != package(name):
        raise ConfigError(f"Its fetch module {site.fetch!r} is outside {package(name)}.")
    problems = manifest_schema.mismatches(manifest, site)
    if problems:
        raise ConfigError("Its ADAPTER doesn't match webcli-tool.toml: " + "; ".join(problems))
    return manifest, site
