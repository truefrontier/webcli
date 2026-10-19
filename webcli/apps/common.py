"""Commands both binaries share: `identity list|login|status`, `serve --identity`, and the login / serve runners the
site groups delegate to."""

from __future__ import annotations

from typing import Any

import click

from webcli.core import browser, daemon, identities, output
from webcli.core.errors import ConfigError
from webcli.core.identities import Identity
from webcli.core.output import EXIT_AUTH, EXIT_OK, EXIT_USAGE, agent_output_options, die, emit_json, emit_rows
from webcli.core.redact import redact_text


def _sites() -> list:
    from webcli import sites

    return list(sites.registry().values())


def _stderr():
    from rich.console import Console

    return Console(stderr=True)


def login_and_report(ident: Identity, timeout: int) -> None:
    """The one place a visible Chrome opens for sign-in: the provider's sign-in site, with the identity's daemon
    paused (its Chrome closed) meanwhile. Needs a person; never run unattended."""
    from rich.markup import escape

    site = identities.login_site(ident, _sites())
    console = _stderr()
    console.print(f"Opening a visible Chrome window to sign {escape(ident.id)} in through {site.name}. "
                  f"Waiting up to {timeout}s…")

    def on_state(state: str) -> None:
        if state == "cloudflare":
            console.print("Cloudflare is checking the browser: click [bold]Verify you are human[/bold] in the Chrome "
                          "window if it asks. Still waiting.")
        else:
            console.print("Waiting for you to sign in in the Chrome window…")

    browser.identity = ident
    try:
        info = browser.run(daemon.paused(ident, lambda: site.login(timeout, on_state)))
    except Exception as exc:
        output.fail(exc, site=site.name, login=f"retry: webcli identity login {ident.id}", chats="the chat list")
    who = " ".join(x for x in (info.get("name"), f"<{info['email']}>" if info.get("email") else None) if x)
    plan = f" (plan: {escape(str(info['plan']))})" if info.get("plan") else ""
    console.print(f"[green]Signed in[/green]{' as ' + escape(who) if who else ''}{plan}. Profile: {ident.profile}")
    if info.get("api") not in (None, "ok"):
        console.print(f"[yellow]Chat list check failed:[/yellow] {escape(redact_text(info['api']))}")
    raise SystemExit(EXIT_OK)


def run_serve(ident: Identity, *, show_status: bool, stop: bool, install_launchd: bool,
              uninstall_launchd: bool) -> None:
    if sum((show_status, stop, install_launchd, uninstall_launchd)) > 1:
        die("Give one of --status, --stop, --install-launchd, --uninstall-launchd.", EXIT_USAGE)
    try:
        if show_status:
            emit_json(daemon.status(ident))
        elif stop:
            emit_json(browser.run(daemon.stop_daemon(ident)))
        elif install_launchd:
            emit_json(daemon.install_launchd(ident))
        elif uninstall_launchd:
            emit_json(daemon.uninstall_launchd(ident))
        else:
            raise SystemExit(daemon.serve(ident))
    except (OSError, RuntimeError) as exc:
        die(str(exc), output.EXIT_API)
    raise SystemExit(EXIT_OK)


def serve_command() -> click.Command:
    @click.command("serve")
    @click.option("--identity", "identity_id", metavar="PROVIDER:ACCOUNT", required=True,
                  help="The identity whose daemon to run or manage, e.g. google:default.")
    @click.option("--status", "show_status", is_flag=True, help="Print whether the daemon runs, its Chrome, launchd.")
    @click.option("--stop", is_flag=True, help="Stop the daemon after its current command (commands restart it).")
    @click.option("--install-launchd", is_flag=True,
                  help="Install and load the launchd agent com.truefrontier.webcli.<identity> (starts at login).")
    @click.option("--uninstall-launchd", is_flag=True, help="Unload and remove the launchd agent.")
    def serve_cmd(identity_id: str, show_status: bool, stop: bool, install_launchd: bool,
                  uninstall_launchd: bool) -> None:
        """Run an identity's daemon: one hidden (headless) Chrome every command on that identity shares. Commands
        start it on demand; logs go to ~/.webcli/logs (redacted)."""
        run_serve(identities.parse(identity_id), show_status=show_status, stop=stop, install_launchd=install_launchd,
                  uninstall_launchd=uninstall_launchd)

    return serve_cmd


def signed_in(ident: Identity, *, browser_check: bool = True) -> dict:
    """{signedIn, via, ...}: no profile means signed out without opening Chrome; otherwise the provider's site asks,
    headless (in the running daemon, else in this process: a check never starts a daemon)."""
    try:
        site = identities.login_site(ident, _sites())
    except ConfigError as exc:
        return {"signedIn": None, "error": str(exc)}
    if not ident.profile_exists():
        return {"signedIn": False, "via": site.name, "reason": "no profile yet"}
    if not browser_check or not site.status_op:
        return {"signedIn": None, "via": site.name, "reason": "not checked"}
    browser.identity = ident
    try:
        info = browser.run(daemon.call(ident, site.name, site.status_op, headed=False, start_daemon=False))
    except Exception as exc:
        return {"signedIn": None, "via": site.name, "error": redact_text(str(exc)) or type(exc).__name__}
    return {"signedIn": bool(info.get("signedIn")), "via": site.name}


def identity_group() -> click.Group:
    @click.group("identity", cls=click.Group)
    def group() -> None:
        """Logged-in browser profiles (provider:account). Only `login` ever shows a window."""

    @group.command("list")
    @agent_output_options()
    def list_cmd(fmt, compact, select, as_csv, quiet) -> None:
        """Every known identity: each provider's default, config.toml's [identities], and existing profiles."""
        rows = [{"id": i.id, "provider": i.provider, "account": i.account, "profile": str(i.profile),
                 "profileExists": i.profile_exists(), "label": i.label} for i in identities.known(_sites())]
        emit_rows(rows, title="Identities", fields=list(rows[0]) if rows else ["id"],
                  compact_fields=("id", "profileExists"), columns=["id", "provider", "profileExists", "profile"],
                  noun="identities", fmt=fmt, compact=compact, select=select, quiet=quiet, as_csv=as_csv)
        raise SystemExit(EXIT_OK)

    @group.command("login")
    @click.argument("identity_id", metavar="PROVIDER:ACCOUNT")
    @click.option("--timeout", default=600, show_default=True, type=click.IntRange(min=10), help="Seconds to wait.")
    def login_cmd(identity_id: str, timeout: int) -> None:
        """Open a VISIBLE Chrome window so a person can sign the identity in (and pass Cloudflare's check). The only
        command that shows a window by itself; never run it unattended."""
        login_and_report(identities.parse(identity_id), timeout)

    @group.command("status")
    @click.argument("identity_id", metavar="[PROVIDER:ACCOUNT]", required=False)
    @click.option("--no-browser", is_flag=True, help="Don't check sign-in with a (headless) Chrome.")
    @agent_output_options(rows=False)
    def status_cmd(identity_id: str | None, no_browser: bool, fmt: str | None, quiet: bool) -> None:
        """Whether each identity (or the one given) has a profile, a daemon, and a signed-in session (checked
        headless). With an ID, exit 4 when it is signed out."""
        targets = [identities.parse(identity_id)] if identity_id else identities.known(_sites())
        rows = [identity_report(i, browser_check=not no_browser) for i in targets]
        emit_json(rows[0] if identity_id else rows)
        if identity_id and rows[0]["signedIn"] is False:
            die(f"{identity_id} is not signed in.", EXIT_AUTH, hint=f"A person runs: {output.APP} identity login {identity_id}")
        raise SystemExit(EXIT_OK)

    return group


def identity_report(ident: Identity, *, browser_check: bool) -> dict[str, Any]:
    info = browser.run(daemon.daemon_info(ident))
    return {"id": ident.id, "profile": str(ident.profile), "profileExists": ident.profile_exists(),
            **signed_in(ident, browser_check=browser_check),
            "daemon": {"running": bool(info.get("running")), "pid": info.get("pid"), "chrome": info.get("chrome"),
                       "launchd": {"label": ident.label, "installed": daemon.plist_path(ident).exists()}}}


def chrome_info() -> dict:
    """Chrome's install, channel, headless default, and the default args dropped (H2)."""
    import plistlib

    try:
        version = plistlib.loads(browser.CHROME_PLIST.read_bytes())["CFBundleShortVersionString"]
    except (OSError, KeyError, ValueError, plistlib.InvalidFileException):
        version = None
    return {"installed": version is not None, "version": version, "channel": "chrome",
            "headlessByDefault": True, "ignoredDefaultArgs": browser.IGNORED_DEFAULT_ARGS}


def doctor_command() -> click.Command:
    """`doctor`: one health report over the shared data home. Registered on both webcli and llmcli."""
    from webcli.core import config, hardening

    @click.command("doctor")
    @click.option("--fix", is_flag=True, help="Re-apply the H4 hardening to the data home first.")
    @click.option("--no-browser", is_flag=True, help="Don't check sign-in with a (headless) Chrome.")
    def doctor_cmd(fix: bool, no_browser: bool) -> None:
        """Health report (JSON): data home hardening, FileVault, Chrome, every identity's profile, daemon, and
        sign-in. Shared by webcli and llmcli (one data home). Never shows a window."""
        from webcli import __version__, sites

        root = config.ensure_home()
        home = hardening.apply(root) if fix else hardening.check(root)
        known = identities.known(list(sites.registry().values()))
        report = {
            "version": __version__,
            "home": home,
            "chrome": chrome_info(),
            "identities": [identity_report(i, browser_check=not no_browser) for i in known],
            "sites": {"llmcli": [s.name for s in sites.of_kind("llm")],
                      "webcli": [s.name for s in sites.of_kind("data")]},
        }
        report["ok"] = bool(home["ok"] and report["chrome"]["installed"])
        emit_json(report)
        raise SystemExit(EXIT_OK)

    return doctor_cmd
