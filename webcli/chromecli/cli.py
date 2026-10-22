"""`chromecli`: read the Chrome tabs Kevin shares, through the chromecli extension (P1: read-only).

Commands: status, list, show, read, screenshot, history, install-host, uninstall-host, doctor, agent-help.
"""

from __future__ import annotations

import base64
import os
from datetime import datetime
from pathlib import Path

import click

from webcli import __version__
from webcli.chromecli import (
    CONFIG_SECTION,
    EXTENSION_ID,
    HOST_NAME,
    TIMEOUT_ENV,
    bridge,
    doctor,
    install,
    protocol,
)
from webcli.core import config, output
from webcli.core.output import (
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_USAGE,
    agent_output_options,
    confirm_write,
    die,
    emit_json,
    emit_rows,
    kv_table,
    resolve_format,
    write_options,
)

NOTES = [
    "chromecli reads Kevin's real, signed-in Chrome (Default profile) through the chromecli MV3 extension and its "
    f"native messaging host ({HOST_NAME}, the chromecli-host script). No CDP, no TCP port, no launchd: Chrome starts "
    "the host when the extension connects, and the host serves ~/.webcli/run/chromecli.sock (0600).",
    "Only tabs Kevin shares in the extension popup are visible: Share-read (badge R) or Share-act (badge A; acting "
    "comes in a later version). Sharing is per (tab, origin): navigating a shared tab to another origin revokes it. "
    "Shares are cleared when Chrome restarts.",
    "Pause agents (popup, or Cmd+Shift+Period) makes every command but `status` exit 4 (`paused`).",
    "Denied origins (built-in: every non-http(s) scheme and the Chrome Web Store; config: banks, sign-in pages, "
    "password managers, checkout/cart paths) can't be shared, and `history` leaves them out.",
    f"Config: [{CONFIG_SECTION}] in ~/.webcli/config.toml: implicit_active (\"off\"|\"read\"), history (bool), "
    "confirm_timeout, read_allow, act_allow, deny. Changes that widen access wait for Kevin to Accept in the popup "
    "(ops on affected origins exit 4 `policy_pending`); tightening applies at once. Invalid config: exit 7.",
    "TAB is Chrome's integer tab id (from `list`); `--active` means the active tab of the last-focused window.",
    "`screenshot` needs the tab to be the active tab of a focused window (else exit 4 `not_visible`); chromecli never "
    "focuses a tab itself.",
    "Errors are JSON on stderr when piped: {\"error\": <code>, \"code\": <exit>, \"message\", \"hint\"}. Codes: "
    + "; ".join(f"{code} {ex}" for code, ex in protocol.ERROR_EXIT.items()) + ".",
    "Every op is logged, redacted, to ~/.webcli/logs/chromecli.log (history queries only as <N chars sha256:8>).",
    f"Extension ID {EXTENSION_ID}; the extension lives in the webcli repo's extensions/chromecli/ (Load unpacked).",
]
WRITES = [
    "chromecli never changes a tab, a page, or Chrome's data (P1 is read-only).",
    "`install-host --confirm` writes ~/Library/Application Support/Google/Chrome/NativeMessagingHosts/"
    f"{HOST_NAME}.json; `uninstall-host --confirm` removes it. Without --confirm both print the plan and write "
    "nothing.",
    "`screenshot -o PATH` writes that image file.",
]
TAB_FIELDS = ["id", "title", "url", "origin", "share", "active", "windowId", "botOpened"]
HISTORY_FIELDS = ["title", "url", "lastVisit", "visitCount"]


def _env() -> dict[str, str]:
    return {
        output.FORMAT_ENV: "json | table | text | md. Default output when --format is not given.",
        TIMEOUT_ENV: "Seconds to wait for the extension (default 30).",
        config.HOME_ENV: "The data home (default ~/.webcli): config.toml [chromecli], run/chromecli.sock, "
                         "logs/chromecli.log.",
    }


def _target(tab: int | None, active: bool) -> dict:
    if (tab is None) == (not active):
        die("Give a TAB id or --active (one of them).", EXIT_USAGE, error="bad_target")
    return {"active": True} if active else {"tab": tab}


def tab_options(f):
    f = click.option("--active", is_flag=True, help="The active tab of the last-focused window.")(f)
    return click.argument("tab", type=int, required=False)(f)


@click.group(cls=output.AgentGroup, name="chromecli")
@click.version_option(__version__, prog_name="chromecli")
def group() -> None:
    """Read the Chrome tabs you share with agents (chromecli extension + native host). Data home: ~/.webcli/"""
    output.env_format()


@group.command("status")
@agent_output_options(rows=False)
def status_cmd(fmt, quiet) -> None:
    """Bridge health: socket, host, extension version + ID, paused, shares, policy. Exit 5 when not connected."""
    try:
        reply = bridge.call("status", timeout=min(bridge.timeout_seconds(), 10))
    except bridge.BridgeDown as exc:
        emit_json({"connected": False, "socket": str(bridge.socket_path()), "socketPresent": bridge.socket_path().exists(),
                   "expectedExtensionId": EXTENSION_ID, "hostManifest": install.check()})
        bridge.die_down(str(exc))
    if not reply.get("ok"):
        bridge.die_reply(reply)
    res = dict(reply.get("result") or {})
    data = {"connected": True, "extensionIdMatches": res.get("extensionId") == EXTENSION_ID,
            "expectedExtensionId": EXTENSION_ID, **res}
    if resolve_format(fmt) == "json":
        emit_json(data)
    else:
        flat = {k: (v if not isinstance(v, (dict, list)) else output.truncate(str(v), 200)) for k, v in data.items()}
        kv_table("chromecli status", flat, resolve_format(fmt))
    raise SystemExit(EXIT_OK)


@group.command("list")
@click.option("--active", is_flag=True, help="Only the active tab of the last-focused window (if shared).")
@agent_output_options()
def list_cmd(active, fmt, compact, select, as_csv, quiet) -> None:
    """Shared tabs: id, title, url, origin, share (read|act), active, windowId, botOpened."""
    rows = bridge.request("tabs.list", {"active": active}) or []
    emit_rows(rows, title="Shared tabs", fields=TAB_FIELDS, compact_fields=("id", "title", "share"),
              columns=["id", "share", "title", "url"], noun="shared tabs", fmt=fmt, compact=compact, select=select,
              quiet=quiet, as_csv=as_csv)
    raise SystemExit(EXIT_OK)


@group.command("show")
@tab_options
@agent_output_options(rows=False)
def show_cmd(tab, active, fmt, quiet) -> None:
    """One shared tab's metadata (`show --active` is chtabs' `active`)."""
    res = bridge.request("tabs.show", _target(tab, active))
    if resolve_format(fmt) == "json":
        emit_json(res)
    else:
        kv_table(f"Tab {res.get('id')}", res, resolve_format(fmt))
    raise SystemExit(EXIT_OK)


@group.command("read")
@tab_options
@click.option("--as", "as_", type=click.Choice(["text", "md"]), default="text", show_default=True,
              help="Page text (innerText) or markdown built from the DOM.")
@click.option("--max-chars", type=click.IntRange(1, 5_000_000), default=200_000, show_default=True,
              help="Cut the content at this many characters (truncated: true).")
@click.option("--elements", is_flag=True, help="Add interactive elements (refs e1..eN; values are never read).")
@agent_output_options(formats=("text", "json"), rows=False)
def read_cmd(tab, active, as_, max_chars, elements, fmt, quiet) -> None:
    """A shared tab's content as text or markdown. JSON: {tab, url, title, origin, as, content, chars, truncated,
    elements?}; on a terminal (text) just the content."""
    args = {**_target(tab, active), "as": as_, "maxChars": max_chars, "elements": elements}
    res = bridge.request("tabs.read", args)
    if resolve_format(fmt, human="text") == "json":
        emit_json(res)
    else:
        click.echo(res.get("content") or "")
        for el in res.get("elements") or []:
            click.echo(f"[{el.get('ref')}] {el.get('tag')}{'/' + el['role'] if el.get('role') else ''} "
                       f"{el.get('name') or ''}".rstrip())
    raise SystemExit(EXIT_OK)


@group.command("screenshot")
@tab_options
@click.option("-o", "--output", "out", type=click.Path(path_type=Path, dir_okay=False), required=True,
              help="Image file to write.")
@click.option("--jpeg", is_flag=True, help="JPEG instead of PNG.")
@click.option("--quality", type=click.IntRange(1, 100), default=80, show_default=True, help="JPEG quality.")
@agent_output_options(rows=False)
def screenshot_cmd(tab, active, out, jpeg, quality, fmt, quiet) -> None:
    """The visible part of a shared tab. The tab must be the active tab of a focused window (else exit 4
    not_visible); nothing is focused for you."""
    args = {**_target(tab, active), "format": "jpeg" if jpeg else "png", "quality": quality}
    res = dict(bridge.request("tabs.screenshot", args, timeout=max(bridge.timeout_seconds(), 30)) or {})
    data = res.pop("data", None)
    if not isinstance(data, str):
        die("The extension sent no image.", protocol.exit_code("script_error"), error="script_error")
    try:
        raw = base64.b64decode(data, validate=True)
    except ValueError:
        die("The extension sent an invalid image.", protocol.exit_code("script_error"), error="script_error")
    path = out.expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    except OSError as exc:
        die(f"Can't write {path}: {exc}", EXIT_USAGE)
    emit_json({**res, "path": str(path.resolve()), "bytes": len(raw)})
    raise SystemExit(EXIT_OK)


def _since_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        die(f"--since {value!r} is not a date (YYYY-MM-DD or ISO 8601).", EXIT_USAGE, error="bad_request")
    return int(when.timestamp() * 1000)


@group.command("history")
@click.option("--query", "query", default="", help="Text to match in titles and URLs.")
@click.option("--limit", type=click.IntRange(1, 1000), default=50, show_default=True)
@click.option("--since", default=None, help="Only visits since this date (YYYY-MM-DD or ISO 8601, local time).")
@agent_output_options()
def history_cmd(query, limit, since, fmt, compact, select, as_csv, quiet) -> None:
    """Chrome history (newest first), denied origins left out. Off when history = false in [chromecli]."""
    rows = bridge.request("history.search", {"query": query, "limit": limit, "since": _since_ms(since)}) or []
    emit_rows(rows, title="History", fields=HISTORY_FIELDS, compact_fields=("title", "url"),
              columns=["lastVisit", "title", "url"], noun="history items", fmt=fmt, compact=compact, select=select,
              quiet=quiet, as_csv=as_csv)
    raise SystemExit(EXIT_OK)


@group.command("install-host")
@click.option("--extension-id", default=EXTENSION_ID, show_default=True, help="The extension ID to allow.")
@write_options
def install_host_cmd(extension_id, dry_run, confirm) -> None:
    """Write Chrome's native-host manifest for chromecli-host (dry run unless --confirm)."""
    if not install.valid_id(extension_id):
        die(f"{extension_id!r} is not a Chrome extension ID (32 letters a-p).", EXIT_USAGE, error="bad_request")
    host = install.host_path()
    if host is None:
        die("chromecli-host isn't installed next to this chromecli or on PATH.", EXIT_CONFIG,
            "Reinstall webcli (pipx install --force <repo>) so the chromecli-host script exists.",
            error="host_not_installed")
    manifest = install.render(extension_id, host)
    current = install.installed()
    plan = {"manifestPath": str(install.manifest_path()), "manifest": manifest, "replaces": current,
            "unchanged": current == manifest}
    if extension_id != EXTENSION_ID:
        plan["warning"] = f"{extension_id} is not the committed chromecli ID {EXTENSION_ID}."
    if not confirm_write(confirm, plan, dry_run=dry_run):
        raise SystemExit(EXIT_OK)
    path = install.write(manifest)
    emit_json({"written": True, "manifestPath": str(path), "manifest": manifest,
               "next": "Reload the chromecli extension in chrome://extensions, then run `chromecli status`."})
    raise SystemExit(EXIT_OK)


@group.command("uninstall-host")
@write_options
def uninstall_host_cmd(dry_run, confirm) -> None:
    """Remove Chrome's native-host manifest for chromecli (dry run unless --confirm)."""
    plan = {"manifestPath": str(install.manifest_path()), "present": install.installed() is not None}
    if not confirm_write(confirm, plan, dry_run=dry_run):
        raise SystemExit(EXIT_OK)
    emit_json({"removed": install.remove(), "manifestPath": str(install.manifest_path())})
    raise SystemExit(EXIT_OK)


@group.command("doctor")
def doctor_cmd() -> None:
    """chromecli health (JSON): host manifest present + ID match, chromecli-host path, socket, extension connected."""
    emit_json({"version": __version__, **doctor.report()})
    raise SystemExit(EXIT_OK)


group.add_command(output.agent_help_command(group, version=__version__, env=_env, notes=NOTES, writes=WRITES))


def main() -> None:
    from webcli.chromecli import FORMAT_ENV

    output.APP, output.FORMAT_ENV = "chromecli", FORMAT_ENV
    os.umask(0o077)
    group.main(prog_name="chromecli")
