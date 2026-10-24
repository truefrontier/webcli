"""The site adapter registry: entry points in the "webcli.sites" group (so a separate package can add a site), plus
the built-in sites when running from a source tree without installed metadata."""

from __future__ import annotations

import importlib
from functools import lru_cache
from importlib.metadata import entry_points

from webcli.core.errors import ConfigError
from webcli.sites.base import Site

GROUP = "webcli.sites"
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
           "amazon": "webcli.sites.amazon:ADAPTER"}


def _load(target: str) -> Site:
    module, _, attr = target.partition(":")
    return getattr(importlib.import_module(module), attr)


@lru_cache(maxsize=1)
def registry() -> dict[str, Site]:
    """name -> Site, sorted by name."""
    found = {ep.name: ep.value for ep in entry_points(group=GROUP)}
    out = {}
    for name, target in sorted({**BUILTIN, **found}.items()):
        site = _load(target)
        if not isinstance(site, Site) or site.name != name:
            raise ConfigError(f"The {GROUP} entry point {name!r} is not a Site named {name!r}.")
        out[name] = site
    return out


def get(name: str) -> Site:
    try:
        return registry()[name]
    except KeyError:
        raise ConfigError(f"No site named {name!r}.", hint="See `webcli sites`.") from None


def of_kind(kind: str) -> list[Site]:
    return [site for site in registry().values() if site.kind == kind]
