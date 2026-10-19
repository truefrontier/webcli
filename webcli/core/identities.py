"""Identities: one logged-in browser profile each, named provider:account ("google:default", "chatgpt:default").

A site logs in through a provider; every site of that provider can share the provider's identity. Resolution, first
match wins: an explicit --identity, then $WEBCLI_IDENTITY, then [sites.<site>].identity in config.toml, then
"<provider>:default". An identity of the wrong provider for the site is a config error (exit 7), wherever it came
from. More accounts later are just more ids (google:work); nothing here assumes "default".

Login is interactive (a visible Chrome window for a person) and only ever runs from `identity login`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from webcli.core import config
from webcli.core.errors import ConfigError

if TYPE_CHECKING:
    from webcli.sites.base import Site

ENV = "WEBCLI_IDENTITY"
# Strict on purpose: an id becomes a directory name, a socket path, and a launchd label.
_ID = re.compile(r"^([a-z][a-z0-9]{0,31}):([a-z0-9][a-z0-9_.-]{0,63})$")
LABEL_PREFIX = "com.truefrontier.webcli."


@dataclass(frozen=True)
class Identity:
    id: str

    def __post_init__(self) -> None:
        if not _ID.match(self.id) or ".." in self.id:
            raise ConfigError(f"{self.id!r} is not an identity id: use provider:account, e.g. google:default.",
                              hint="Lowercase letters, digits, and _ . - in the account.")

    @property
    def provider(self) -> str:
        return self.id.partition(":")[0]

    @property
    def account(self) -> str:
        return self.id.partition(":")[2]

    @property
    def dir_name(self) -> str:
        """identities/<dir>: ":" becomes "-" (the provider has no "-", so the mapping reverses)."""
        return self.id.replace(":", "-", 1)

    @property
    def path(self) -> Path:
        return config.home() / "identities" / self.dir_name

    @property
    def profile(self) -> Path:
        return self.path / "chrome-profile"

    @property
    def label(self) -> str:
        return LABEL_PREFIX + self.dir_name

    def run_file(self, suffix: str) -> Path:
        """run/<dir><suffix>: ".sock", ".profile.lock", ".serve.lock". Not created: writers call config.run_dir()."""
        return config.home() / "run" / f"{self.dir_name}{suffix}"

    def profile_exists(self) -> bool:
        return self.profile.is_dir() and any(self.profile.iterdir())


def parse(value: str) -> Identity:
    return Identity(value.strip())


def from_dir(name: str) -> Identity:
    provider, _, account = name.partition("-")
    return Identity(f"{provider}:{account}")


def _table(value, where: str, key: str | None = None) -> dict:
    """value[key] (or value) as a TOML table: absent is {}, any other type is a ConfigError (exit 7)."""
    if key is not None:
        value, where = value.get(key), f"{where} [{key}]"
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a table, not {type(value).__name__}.", hint="Fix config.toml.")
    return value


def resolve(site: "Site", explicit: str | None = None) -> Identity:
    """The identity `site` runs as (see the module doc for the order)."""
    if explicit:
        source, value = "--identity", explicit
    elif os.environ.get(ENV, "").strip():
        source, value = f"${ENV}", os.environ[ENV]
    else:
        entry = _table(_table(config.load(), "config.toml", "sites").get(site.name),
                       f"config.toml [sites.{site.name}]")
        source, value = f"config.toml [sites.{site.name}].identity", entry.get("identity", f"{site.provider}:default")
        if not isinstance(value, str):
            raise ConfigError(f"{source} must be a string like {site.provider}:default.")
    identity = parse(value)
    if identity.provider != site.provider:
        raise ConfigError(f"{source} is {identity.id}, a {identity.provider} identity; {site.name} signs in with "
                          f"{site.provider}.", hint=f"Use --identity {site.provider}:<account>, or unset {ENV}.")
    return identity


def known(sites: "list[Site]") -> list[Identity]:
    """Every identity worth reporting: each provider's default, [identities.*] in config.toml, and every profile dir."""
    ids = {f"{site.provider}:default" for site in sites}
    ids |= set(_table(config.load(), "config.toml", "identities"))
    root = config.home() / "identities"
    if root.is_dir():
        for entry in root.iterdir():
            try:
                ids.add(from_dir(entry.name).id)
            except ConfigError:
                continue
    out = []
    for value in sorted(ids):
        try:
            out.append(parse(value))
        except ConfigError:
            continue
    return out


def login_site(identity: Identity, sites: "list[Site]") -> "Site":
    """The site whose sign-in flow logs `identity` in: the first of its provider's sites that has one."""
    for site in sites:
        if site.provider == identity.provider and site.login is not None:
            return site
    raise ConfigError(f"No installed site signs in with {identity.provider}.", hint="See `webcli sites`.")
