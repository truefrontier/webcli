"""What a site adapter declares. A site is a module exposing ADAPTER = Site(...), registered under the
"webcli.sites" entry-point group (see sites/__init__.py). Tools outside core import these from webcli.adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

import click

NO_IDENTITY = "none"  # the provider of a site that never signs in (aa, walkie): no identity, no browser


class Strategy(Enum):
    """How a verb gets its data (OpenCLI's four, matched to what the sites already do)."""

    PUBLIC = "public"  # plain HTTP, no login
    COOKIE = "cookie"  # in-page fetch(..., {credentials: "include"}) against the site's own API
    INTERCEPT = "intercept"  # read the site's own XHR/fetch responses
    UI = "ui"  # drive the DOM (the composer)


@dataclass(frozen=True)
class Site:
    name: str
    kind: str  # "llm" (an `llmcli` site) or "data" (a `webcli` site)
    provider: str  # the identity provider it signs in with: its default identity is "<provider>:default"; NO_IDENTITY
    # for a site with no sign-in
    summary: str
    cli: Callable[[], click.Group]  # the site's command group (imported lazily)
    fetch: str  # module whose functions the identity daemon may run
    ops: tuple[str, ...]  # those functions; each takes headed=
    verbs: dict[str, Strategy] = field(default_factory=dict)
    # async login(identity, timeout, on_state) -> {signedIn, ...}: the provider's interactive sign-in, a visible
    # window for a person. Only `identity login` calls it.
    login: Callable[..., Awaitable[dict]] | None = None
    # async status(headed=False) -> {signedIn, ...} for `doctor` / `identity status` (a daemon op).
    status_op: str | None = "status"
    # A data site that also offers a chat surface under `llmcli <site>` (notebooklm's ask): its llmcli group,
    # imported lazily. It runs the same fetch ops in the same daemon; the group itself limits which.
    llm_cli: Callable[[], click.Group] | None = None
    # Adapter API v1 (webcli.adapter, docs/adapter-api-v1.md). All optional; a built-in that sets none still works.
    # There is no `tier`: core sets official/community when it loads a site, never the site itself.
    adapter_api: int = 1  # the webcli.adapter.ADAPTER_API the site was built against
    hosts: tuple[str, ...] = ()  # every host the site's pages may reach: "www.loom.com", "*.loom.com"
    version: str = ""  # the site's own version, X.Y.Z; "" for a built-in, which ships with webcli
    writes: frozenset[str] = frozenset()  # verbs that change something on the site
    sends_to_people: frozenset[str] = frozenset()  # of those, the ones that reach another person (a DM, a post)

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "binary": "llmcli" if self.kind == "llm" else "webcli",
                "alsoLlmcli": self.kind != "llm" and self.llm_cli is not None,
                "provider": self.provider,
                "defaultIdentity": None if self.provider == NO_IDENTITY else f"{self.provider}:default",
                "summary": self.summary,
                "verbs": {name: strategy.value for name, strategy in self.verbs.items()},
                "adapterApi": self.adapter_api,
                **({"version": self.version} if self.version else {}),
                **({"hosts": list(self.hosts)} if self.hosts else {}),
                **({"writes": sorted(self.writes)} if self.writes else {}),
                **({"sendsToPeople": sorted(self.sends_to_people)} if self.sends_to_people else {})}
