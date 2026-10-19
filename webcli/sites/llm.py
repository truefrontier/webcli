"""What every `llmcli <site>` group shares: the group itself (--identity, --lock-wait), prompt and output-file
handling, the last-chat file for `-p -c`, the uniform ask JSON, and the login / serve / agent-help commands.

The uniform ask JSON (every llm site, null/empty when a site lacks one): answer, chatId, requestedModel,
servedModel, servedModelDisplay, fallback, images, citations, canvas.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

import click

from webcli import __version__
from webcli.core import browser, config, daemon, output
from webcli.core.identities import Identity, resolve
from webcli.core.output import EXIT_API, EXIT_USAGE, die, write_atomic
from webcli.sites.base import Site

UNIFORM = {"answer": None, "chatId": None, "requestedModel": None, "servedModel": None, "servedModelDisplay": None,
           "fallback": False, "images": [], "citations": [], "canvas": None}
# images: the saved files' paths ([] without --image-out); each site's imageFiles says what each one is.


def env_help() -> dict[str, str]:
    return {
        output.FORMAT_ENV: "json | table | text | md. Default output when --format is not given (set json in tmux: "
                           "panes are TTYs). `-p` always prints the answer as text unless --output-format json.",
        browser.LOCK_WAIT_ENV: "Seconds to wait for the identity's profile lock or daemon turn (default 30).",
        "WEBCLI_IDENTITY": "Identity to run as (provider:account) when --identity is not given; a different "
                           "provider's identity is a config error (exit 7).",
        daemon.DAEMON_ENV: "0: run Chrome (still hidden) in this process instead of the identity daemon, pausing the "
                           "daemon meanwhile.",
        daemon.RECYCLE_ENV: "Hours before the daemon relaunches its Chrome (default 6).",
        config.HOME_ENV: "The data home (default ~/.webcli).",
    }


def uniform(result: dict) -> dict:
    """The ask result with every uniform field present."""
    for key, value in UNIFORM.items():
        result.setdefault(key, value.copy() if isinstance(value, list) else value)
    return result


def identity() -> Identity:
    return browser.current()


def login_hint(site: Site) -> str:
    """What to run to sign the command's identity in (a person, at a visible window)."""
    ident = identity()
    flag = "" if ident.id == f"{site.provider}:default" else f" --identity {ident.id}"
    return f"a person runs: llmcli {site.name}{flag} login (or webcli identity login {ident.id})"


def _wait_arg(ctx: click.Context, param: click.Parameter, value: float) -> float:
    try:
        return browser.parse_wait(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from None


def site_group(site: Site, help_text: str) -> click.Group:
    """`llmcli <site>`: resolves the identity (exit 7 when it can't) and the lock wait before any command runs."""

    @click.group(cls=output.AgentGroup, name=site.name, help=help_text)
    @click.version_option(__version__, prog_name=f"llmcli {site.name}")
    @click.option("--identity", "identity_id", metavar="PROVIDER:ACCOUNT", default=None,
                  help=f"Run as this identity (default {site.provider}:default; env WEBCLI_IDENTITY).")
    @click.option("--lock-wait", type=float, default=30.0, envvar=browser.LOCK_WAIT_ENV, show_default=True,
                  callback=_wait_arg,
                  help=f"Seconds to wait when another command holds the identity's Chrome (env "
                       f"{browser.LOCK_WAIT_ENV}).")
    def group(identity_id: str | None, lock_wait: float) -> None:
        output.env_format()  # a bad format env is a usage error before any command does work
        browser.lock_wait = lock_wait
        browser.identity = resolve(site, identity_id)

    return group


def prompt_arg(ctx: click.Context | None, param: click.Parameter | None, value: str) -> str:
    """`-` reads stdin. Never waits on a terminal: that would be a TTY prompt."""
    if value == "-":
        if sys.stdin.isatty():
            raise click.BadParameter("`-` reads the prompt from stdin, but stdin is a terminal. Pipe it in.")
        value = sys.stdin.read()
    value = value.strip()
    if not value:
        raise click.BadParameter("The prompt is empty.")
    return value


def print_prompt(words: tuple[str, ...], prompt_file: Path | None) -> str:
    """-p's prompt: --prompt-file (`-`: stdin), else WORDS joined by spaces, else stdin (`-` or no words)."""
    if prompt_file and words:
        die("Give the prompt as words or --prompt-file, not both.", EXIT_USAGE)
    if not prompt_file:
        return prompt_arg(None, None, " ".join(words) if words else "-")
    if str(prompt_file) == "-":
        return prompt_arg(None, None, "-")
    try:
        text = prompt_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        die(f"Can't read --prompt-file: {exc}", EXIT_USAGE)
    if not text:
        die("The prompt is empty.", EXIT_USAGE)
    return text


def check_output_file(output_file: Path | None) -> None:
    if output_file and str(output_file) != "-" and not output_file.parent.is_dir():
        die(f"--output-file: no such directory: {output_file.parent}", EXIT_USAGE)


def emit_answer(output_file: Path | None, text: str, write: Callable[[Path, str], None]) -> None:
    """-p's output: --output-file PATH (atomically), or stdout (also for `-`)."""
    if output_file and str(output_file) != "-":
        write(output_file, text)
    else:
        click.echo(text, nl=False)


def writer(site_label: str, show_cmd: str, home_url: str) -> Callable[[Path, str, str | None], None]:
    """write_atomic once the site has answered. On failure, exit 5 saying where the answer is, so nobody resends."""

    def write(path: Path, text: str, chat_id: str | None) -> None:
        try:
            write_atomic(path, text)
        except OSError as exc:
            where = f"Read it with: {show_cmd} {chat_id}" if chat_id else f"Read it at {home_url}."
            die(f"{site_label} answered, but writing {path} failed: {exc}", EXIT_API,
                hint=f"Don't resend the prompt. {where}")

    return write


def last_chat_file(site: Site) -> Path:
    """sites/<site>/<identity-dir>/last_chat: the chat of the identity's last finished ask, for `-p -c`."""
    return config.private_dir(config.site_dir(site.name) / identity().dir_name) / "last_chat"


def read_last_chat(site: Site, clean: Callable[[str], str]) -> str:
    try:
        return clean(last_chat_file(site).read_text())
    except (OSError, ValueError):
        die("No chat to continue: no ask has finished yet.", EXIT_USAGE, hint="Drop -c, or pass --chat ID.")


def image_out_arg(is_dir_target: Callable[[str], bool]) -> Callable:
    """--image-out: a directory is created now (so a trailing "/" still counts); a file needs an existing
    directory."""

    def callback(ctx: click.Context, param: click.Parameter, value: str | None) -> Path | None:
        if value is None:
            return None
        path = Path(value).expanduser()
        if is_dir_target(value):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise click.BadParameter(f"can't create directory {path}: {exc}") from None
        elif not path.parent.is_dir():
            raise click.BadParameter(f"no such directory: {path.parent}")
        return path

    return callback


prompt_file_option = click.option(
    "--prompt-file", type=click.Path(exists=True, allow_dash=True, dir_okay=False, path_type=Path),
    help="Read the prompt from this file (`-`: stdin).")
output_file_option = click.option(
    "--output-file", type=click.Path(allow_dash=True, dir_okay=False, path_type=Path),
    help="Write the answer to this file (atomically) instead of stdout (`-`: stdout).")
output_format_option = click.option(
    "--output-format", type=click.Choice(["text", "md", "json"]), default="text", show_default=True,
    help="json prints the `ask` JSON object instead of the answer (md is the same as text: the answer is markdown).")


def add_common_commands(group: click.Group, site: Site, *, notes: list[str], writes: list[str]) -> None:
    """login (delegates to `identity login`), serve (the identity's daemon), and agent-help."""

    @group.command("login")
    @click.option("--timeout", default=600, show_default=True, type=click.IntRange(min=10), help="Seconds to wait.")
    def login_cmd(timeout: int) -> None:
        """Open a VISIBLE Chrome so a person can sign in (same as `identity login <this site's identity>`)."""
        from webcli.apps.common import login_and_report

        login_and_report(identity(), timeout)

    @group.command("serve")
    @click.option("--status", "show_status", is_flag=True,
                  help="Print whether the identity's daemon runs, its Chrome, and launchd.")
    @click.option("--stop", is_flag=True, help="Stop the daemon after its current command (commands restart it).")
    @click.option("--install-launchd", is_flag=True,
                  help="Install and load the launchd agent com.truefrontier.webcli.<identity> (starts at login).")
    @click.option("--uninstall-launchd", is_flag=True, help="Unload and remove the launchd agent.")
    def serve_cmd(show_status: bool, stop: bool, install_launchd: bool, uninstall_launchd: bool) -> None:
        """Run this site's identity daemon: one hidden (headless) Chrome every command on the identity shares.
        Commands start it on demand; logs go to ~/.webcli/logs (redacted)."""
        from webcli.apps.common import run_serve

        run_serve(identity(), show_status=show_status, stop=stop, install_launchd=install_launchd,
                  uninstall_launchd=uninstall_launchd)

    group.add_command(output.agent_help_command(group, version=__version__, env=env_help, notes=notes,
                                                writes=writes))


def call(site: Site, op: str, *args, headed: bool = False, **kwargs) -> Any:
    """<site>.fetch.<op> in the identity daemon's hidden Chrome (started on demand), or with --headed in a visible
    Chrome of this command's own, while the daemon's Chrome steps aside. Returns the coroutine's result; errors
    propagate for the site's own mapping."""
    ident = identity()
    if headed:
        import importlib

        fn = getattr(importlib.import_module(site.fetch), op)
        return browser.run(daemon.paused(ident, lambda: fn(*args, headed=True, **kwargs)))
    return browser.run(daemon.call(ident, site.name, op, *args, headed=False, **kwargs))
