"""What a site adapter declares. A site is a module exposing ADAPTER = Site(...), registered under the
"webcli.sites" entry-point group (see sites/__init__.py)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

import click


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
    provider: str  # the identity provider it signs in with: its default identity is "<provider>:default"
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

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "binary": "llmcli" if self.kind == "llm" else "webcli",
                "provider": self.provider, "defaultIdentity": f"{self.provider}:default", "summary": self.summary,
                "verbs": {name: strategy.value for name, strategy in self.verbs.items()}}
