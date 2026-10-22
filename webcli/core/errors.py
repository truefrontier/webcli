"""The errors every site raises. The daemon rebuilds exactly these across its socket (daemon.rebuild), and
output.fail maps each one to its exit code."""

from __future__ import annotations

import signal


class AuthRequired(RuntimeError):
    """Not signed in, or the session expired: exit 4."""


class ProfileBusy(RuntimeError):
    """Another command, or a Chrome, holds the identity's profile: exit 5. `error`: an optional fixed tag for the JSON
    error ("messages_busy", "messages_open_elsewhere")."""

    def __init__(self, message: str, error: str | None = None) -> None:
        super().__init__(message)
        self.error = error


class Challenged(RuntimeError):
    """A Cloudflare "Verify you are human" check didn't clear by itself. Nothing clicks it, and no window is shown for
    it: a person does, in the window `identity login` opens. Exit 4 (hidden_browser_blocked)."""

    def __init__(self, mode: str, host: str = "the site") -> None:
        self.mode, self.host = mode, host
        super().__init__(f"Cloudflare is asking for a human check on {host} ({mode} Chrome).")


class Interrupted(RuntimeError):
    """SIGTERM or SIGHUP arrived; Chrome (or its tab) was closed and the lock released before this was raised."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"Interrupted by {signal.Signals(signum).name}; the command was cancelled and its Chrome "
                         "tab closed.")


class RateLimited(RuntimeError):
    """The site refused for a usage cap: exit 6. `sent`: the prompt was submitted first. `resets_at`: ISO 8601 or
    None."""

    def __init__(self, message: str, *, sent: bool, resets_at: str | None = None) -> None:
        super().__init__(message)
        self.message, self.sent, self.resets_at = message, sent, resets_at


class NotFound(LookupError):
    """No such chat, model, or record: exit 3."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class UsageError(ValueError):
    """The request can't be done as asked, found only once the site answered (a label name two labels share, a
    system group as a write target): exit 2."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class ConfigError(RuntimeError):
    """Missing or invalid config.toml, identity, or site name: exit 7."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint
