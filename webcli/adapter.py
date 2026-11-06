"""The adapter API: everything a webcli tool outside core may import. Tools import from webcli.adapter, never from
webcli.sites.base, webcli.core.*, or any other module, which can change in any release without notice.

ADAPTER_API is this module's version. It goes up only when something here changes in a way that breaks a tool
(a name removed or renamed, a signature or return shape changed). Adding a name doesn't change it. Core loads tools
built for any API in SUPPORTED_ADAPTER_APIS (the current one and, once there is one, the one before it); a tool built
for another is listed as needing an update and isn't loaded.

What a tool declares: ADAPTER = Site(...) (with adapter_api, hosts, version, writes, sends_to_people) in code, and the
same facts in its webcli-tool.toml manifest (docs/webcli-tool-manifest.md). docs/adapter-api-v1.md has the rest.
"""

from __future__ import annotations

from webcli.core.browser import site_page
from webcli.core.errors import (
    AuthRequired,
    Challenged,
    ConfigError,
    Interrupted,
    NotFound,
    ProfileBusy,
    RateLimited,
    UsageError,
)
from webcli.core.guard import Guard, Policy, describe, host_matches, path_matches
from webcli.core.output import (
    EXIT_API,
    EXIT_AUTH,
    EXIT_CONFIG,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_RATE_LIMITED,
    EXIT_SENT_UNVERIFIED,
    EXIT_USAGE,
    agent_output_options,
    confirm_write,
    die,
    emit_json,
    emit_rows,
    fail,
    note,
    warn,
    write_options,
)
from webcli.core.redact import redact_text, redact_url, redact_value, shape
from webcli.sites.base import NO_IDENTITY, Site, Strategy
from webcli.sites.data import add_agent_help, call, emit_record, login_hint, site_group

ADAPTER_API = 1
SUPPORTED_ADAPTER_APIS = (ADAPTER_API,)

__all__ = [
    "ADAPTER_API", "SUPPORTED_ADAPTER_APIS",
    # declaring a site
    "Site", "Strategy", "NO_IDENTITY",
    # the request guard: a site's Policy, and helpers for writing one
    "Policy", "Guard", "describe", "host_matches", "path_matches",
    # errors: raise these, never your own exit codes
    "AuthRequired", "Challenged", "ConfigError", "Interrupted", "NotFound", "ProfileBusy", "RateLimited", "UsageError",
    # output and exit codes
    "EXIT_OK", "EXIT_USAGE", "EXIT_NOT_FOUND", "EXIT_AUTH", "EXIT_API", "EXIT_RATE_LIMITED", "EXIT_CONFIG",
    "EXIT_SENT_UNVERIFIED", "agent_output_options", "emit_json", "emit_rows", "die", "fail", "note", "warn",
    # writes: --dry-run by default, --confirm (alias --yes) to do it
    "write_options", "confirm_write",
    # redaction, for fixtures and anything printed
    "redact_text", "redact_value", "redact_url", "shape",
    # a browser tool: its `webcli <tool>` group, running its fetch ops in the identity's hidden Chrome, the page an
    # op works in (or, mode="visible" with sign_in=True, the sign-in window), and its agent-help
    "site_group", "call", "site_page", "emit_record", "login_hint", "add_agent_help",
]
