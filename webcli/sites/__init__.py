"""The site registry: the built-in sites (tier "official"), then the community tools in ~/.webcli/tools/ (tier
"community"; core/tools_registry.py loads them). Official sites come first, so where both could serve an identity
(identities.login_site), the official one does.

A community tool is skipped, with a warning on stderr and a line in `webcli doctor`, never an error for the whole
CLI, when: its name is a built-in site, a built-in site's provider (google, meta), a webcli command, or already taken;
it is revoked; or anything about it is broken. A disabled tool is skipped quietly. A site's tier comes from where it
was loaded, never from the site itself.

The "webcli.sites" entry points are webcli's own built-ins. Another package's entry point isn't loaded: community
tools come with a webcli-tool.toml manifest, through `webcli tools install` or `webcli new`.
"""

from __future__ import annotations

import importlib
from functools import lru_cache
from importlib.metadata import entry_points
from typing import NamedTuple

from webcli.core.errors import ConfigError
from webcli.sites.base import Site

GROUP = "webcli.sites"
OFFICIAL, COMMUNITY = "official", "community"
BUILTIN = {"gemini": "webcli.sites.gemini:ADAPTER", "chatgpt": "webcli.sites.chatgpt:ADAPTER",
           "muse": "webcli.sites.muse:ADAPTER", "aa": "webcli.sites.aa:ADAPTER",
           "walkie": "webcli.sites.walkie:ADAPTER", "keep": "webcli.sites.keep:ADAPTER",
           "photos": "webcli.sites.photos:ADAPTER", "youtube": "webcli.sites.youtube:ADAPTER",
           "recorder": "webcli.sites.recorder:ADAPTER", "contacts": "webcli.sites.contacts:ADAPTER",
           "gmail": "webcli.sites.gmail:ADAPTER", "loom": "webcli.sites.loom:ADAPTER",
           "facebook": "webcli.sites.facebook:ADAPTER", "linkedin": "webcli.sites.linkedin:ADAPTER",
           "messages": "webcli.sites.messages:ADAPTER",
           "whatsapp": "webcli.sites.whatsapp:ADAPTER",
           "notebooklm": "webcli.sites.notebooklm:ADAPTER",
           "amazon": "webcli.sites.amazon:ADAPTER",
           "perplexity": "webcli.sites.perplexity:ADAPTER",
           "gnews": "webcli.sites.gnews:ADAPTER",
           "gsc": "webcli.sites.gsc:ADAPTER"}


class Loaded(NamedTuple):
    sites: dict[str, Site]
    tiers: dict[str, str]
    skipped: list[dict]  # {name, source, reason}: community tools not loaded, for `doctor`
    candidates: dict  # name -> the tools_registry.Candidate each community tool was loaded from
    skipped_from: list  # the Candidate behind each `skipped` entry (None for another package's entry point)


def _load(target: str) -> Site:
    module, _, attr = target.partition(":")
    return getattr(importlib.import_module(module), attr)


def _skip(skipped: list[dict], name: str, source: str, reason: str, *, quiet: bool = False) -> None:
    from webcli.core.output import warn

    skipped.append({"name": name, "source": source, "reason": reason})
    if not quiet:
        warn(f"webcli: skipped the {name} tool ({source}): {reason}")


@lru_cache(maxsize=1)
def loaded() -> Loaded:
    from webcli.core import manifest_schema, tools_registry

    sites: dict[str, Site] = {}
    skipped: list[dict] = []
    candidates: dict = {}
    skipped_from: list = []

    def skip(name: str, source: str, reason: str, candidate=None, *, quiet: bool = False) -> None:
        _skip(skipped, name, source, reason, quiet=quiet)
        skipped_from.append(candidate)

    for name, target in sorted(BUILTIN.items()):
        site = _load(target)
        if not isinstance(site, Site) or site.name != name:
            raise ConfigError(f"The built-in site {name!r} is not a Site named {name!r}.")
        sites[name] = site
    tiers = dict.fromkeys(sites, OFFICIAL)
    for ep in entry_points(group=GROUP):  # webcli's own (even a stale install's) are BUILTIN's; anyone else's: skipped
        if BUILTIN.get(ep.name) != ep.value and getattr(ep.dist, "name", None) != "truefrontier-webcli":
            skip(ep.name, f"entry point {ep.value}",
                  "another package's entry point; community tools install with `webcli tools install`")
    reserved = manifest_schema._reserved()
    for candidate in tools_registry.candidates():
        name, source = candidate.name, f"{candidate.source} {candidate.folder}"
        if name in reserved or name in sites:
            what = "a webcli built-in or command" if name in reserved else "another tool's name"
            skip(name, source, f"{name!r} is {what}; a tool can't replace it", candidate)
        elif tools_registry.is_disabled(candidate.folder):
            skip(name, source, "disabled", candidate, quiet=True)
        else:
            try:
                sites[name] = tools_registry.load(candidate)
            except (Exception, SystemExit) as exc:
                skip(name, source, str(exc) or type(exc).__name__, candidate)
            else:
                tiers[name] = COMMUNITY
                candidates[name] = candidate
    return Loaded(sites, tiers, skipped, candidates, skipped_from)


def registry() -> dict[str, Site]:
    """name -> Site: the official sites by name, then the community tools."""
    return loaded().sites


def reload() -> None:
    """Forget the loaded registry (a tool was installed, removed, or changed): the next call loads it again."""
    loaded.cache_clear()


def tier_of(name: str) -> str | None:
    """"official" or "community" for a loaded site; None for a name not in the registry."""
    return loaded().tiers.get(name)


def skipped() -> list[dict]:
    return loaded().skipped


BUILT_IN = {"source": "builtin", "verified": None, "commit": None, "sha256": None, "grants": [], "revoked": False,
            "disabled": False}


def provenance(name: str, granted: dict[str, list[str]]) -> dict:
    """Where a loaded site came from (`webcli sites` JSON): a built-in, or the community tool's own candidate, the one
    core loaded (tools_registry.provenance). `granted` is grants.list_grants()."""
    from webcli.core import tools_registry

    candidate = loaded().candidates.get(name)
    return tools_registry.provenance(candidate, granted) if candidate else dict(BUILT_IN)


def skipped_tools(granted: dict[str, list[str]]) -> list[dict]:
    """Every community tool core didn't load (revoked, disabled, shadowed, broken), with the same provenance as a
    loaded one, read without importing any of its code."""
    from webcli.core import tools_registry

    out = []
    for entry, candidate in zip(loaded().skipped, loaded().skipped_from):
        facts = tools_registry.provenance(candidate, granted) if candidate else \
            {"source": "entrypoint", "verified": False, "commit": None, "sha256": None, "grants": [],
             "revoked": False, "disabled": False}
        out.append({"name": entry["name"], "reason": entry["reason"], **facts})
    return out


def get(name: str) -> Site:
    try:
        return registry()[name]
    except KeyError:
        raise ConfigError(f"No site named {name!r}.", hint="See `webcli sites`.") from None


def of_kind(kind: str) -> list[Site]:
    return [site for site in registry().values() if site.kind == kind]


def hosts_limit(name: str) -> tuple[str, ...] | None:
    """The hosts core holds `name`'s pages to: a community tool's Site.hosts (its manifest's; empty allows nothing);
    None for an official site, which keeps its own Policy only."""
    return registry()[name].hosts if tier_of(name) == COMMUNITY else None
