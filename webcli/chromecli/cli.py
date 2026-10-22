"""`chromecli`: read and act in the Chrome tabs Kevin shares, through the chromecli extension.

Read: status, list, show, read, screenshot, history, wait, find (Jev or heuristics). Tab actions (P2): open, focus, close, navigate. Page actions
(P3): click, type, fill, select, press, eval (disabled). Management: install-host, uninstall-host, doctor, agent-help.
Every action previews unless --confirm; risky ones also wait for Kevin's Allow in Chrome.
"""

from __future__ import annotations

import base64
import json
import os
import sys
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
    policy,
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
    "Only tabs Kevin shares in the extension popup are visible: Share-read (badge R) or Share-act (badge A: read and "
    "act). Sharing is per (tab, origin): navigating a shared tab to another origin revokes it. Shares are cleared when "
    "Chrome restarts.",
    "Actions preview by default: without --confirm they print {\"dryRun\": true, ...} and send nothing to Chrome (exit "
    "0); --dry-run --confirm stays a dry run. Tab actions: `open URL` (no share needed), `focus|close|navigate TAB` "
    "(Share-act or a tab the bot opened). Bot-opened tabs are auto-shared for act if the origin is in act_allow, for "
    "read if in read_allow, else left unshared (metadata only in `list`).",
    "Page actions (click/type/fill/select/press/eval) need --tab, Share-act, and --expect-origin or --expect-url "
    "(missing: exit 2 missing_expect; tab elsewhere: exit 5 url_mismatch). Targets: exactly one of --selector, --ref "
    "(from `read TAB --elements` or `find`; refs die on navigation), --text [--role], or --find INTENT (resolved like "
    "`find`; no clear winner: exit 3 ambiguous_target with candidates). "
    "They run in the extension's isolated world with synthetic events (isTrusted=false; some sites ignore them).",
    "In-browser confirm (Kevin clicks Allow once / Deny in a chromecli window; 60s = confirm_timeout): always for "
    "eval; clicks on submit-like elements (form submit buttons, input[type=submit|image], names like buy, order, pay, "
    "send, post, reply, submit, confirm, delete, remove, subscribe, accept, approve, sign out, merge, deploy ...); "
    "Enter in a form field; type/fill into password, cc-*, current-password/new-password/one-time-code, or "
    "card/cvv/cvc/iban/routing/ssn fields; open, or cross-origin navigate, outside read_allow and act_allow (a "
    "same-origin navigate is confirmed only in bot-opened tabs). Deny: exit 4 declined; no "
    "answer: exit 4 confirm_timeout; Pause while waiting: exit 4 paused. Never retry a declined action on your own.",
    "Post-checks: fill/type/select read the value back; --verify-url-prefix / --verify-text wait up to 10s. A check that "
    "fails after the action went out: exit 8 sent_unverified (don't repeat; read the page). Result JSON: {tab, origin, "
    "op, element, gated, reasons, confirmedInBrowser, dispatched, verified}.",
    "eval is disabled (exit 7 eval_disabled): Chrome's MV3 isolated-world CSP forbids running code strings.",
    "Jev (optional, BYOK; Kevin saves a TypeSafe key in the extension's options): `find --tab TAB INTENT` and --find rank "
    "elements with Jev (source jev), else heuristics (source heuristic); click/press targets are classified "
    "(safe/submit/purchase/destructive/send_publish/credential/uncertain). In tabs Kevin Share-act'd himself "
    "(result tabKind act) Jev adds a jev:<choice> gate unless it is >= the act-tab safe floor (default 0.7) sure the "
    "target is safe; in bot-opened tabs (tabKind bot, incl. auto-shared via act_allow) the bar is the confidence floor "
    "(0.9), which find/--find also use. Result jevInfo shows Jev's answer even when it isn't a gate. Jev only adds "
    "gates, never removes one. --jev auto (default) uses Jev when a key is set; if the call fails: act tabs use "
    "heuristics only, bot tabs gate more strictly (every button and anything in a form asks Kevin); result jev.used "
    "false. --jev on: exit 7 jev_not_configured without a key, exit 5 jev_error on failure. "
    "--jev off: heuristics only. status.jevConfigured says whether a key is set; the key never leaves the extension.",
    "Put sensitive values in a file: --value-file FILE (or '-' for stdin), not --value on the command line.",
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
    "Every op is logged, redacted, to ~/.webcli/logs/chromecli.log (history queries, typed/filled/selected values, "
    "--text targets, and eval code only as <N chars sha256:8>).",
    f"Extension ID {EXTENSION_ID}; the extension lives in the webcli repo's extensions/chromecli/ (Load unpacked).",
]
WRITES = [
    "Without --confirm (or with --dry-run) no action is sent to Chrome; the preview is {\"dryRun\": true, ...}.",
    "`open URL --confirm` opens a tab (background unless --active; --window new); `focus TAB --confirm` brings a tab "
    "and its window to the front; `close TAB --confirm` closes an act-shared or bot-opened tab; `navigate TAB URL "
    "--confirm` loads a URL in an act-shared or bot-opened tab.",
    "`click`, `type`, `fill`, `select`, `press` with --confirm change the page in an act-shared tab (they can submit "
    "forms, send, buy, or delete: the risky ones wait for Kevin's Allow in Chrome). `eval` is disabled.",
    "`wait` only reads.",
    "`install-host --confirm` writes ~/Library/Application Support/Google/Chrome/NativeMessagingHosts/"
    f"{HOST_NAME}.json; `uninstall-host --confirm` removes it. Without --confirm both print the plan and write "
    "nothing.",
    "`screenshot -o PATH` writes that image file.",
]
TAB_FIELDS = ["id", "title", "url", "origin", "share", "active", "windowId", "botOpened"]
HISTORY_FIELDS = ["title", "url", "lastVisit", "visitCount"]
FIND_FIELDS = ["ref", "score", "source", "tag", "role", "name"]


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
    """Read and act in the Chrome tabs you share with agents (chromecli extension + native host). Actions preview
    unless --confirm. Data home: ~/.webcli/"""
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


# ---- tab actions (P2) and page actions (P3). Preview unless --confirm; --dry-run wins over --confirm.

def _local_policy() -> dict | None:
    try:
        return policy.load()
    except Exception:
        return None


def _write_timeout(jev: str = "off") -> float:
    """Long enough for an in-browser confirm (confirm_timeout) plus the post-checks (<= 10s each), plus two Jev calls
    (<= 25s each) unless --jev off."""
    pol = _local_policy() or {}
    return max(bridge.timeout_seconds(), float(pol.get("confirm_timeout") or 60) + 30 + (0 if jev == "off" else 55))


def _preview(plan: dict) -> None:
    emit_json({"dryRun": True, **plan, "hint": "Nothing was sent to Chrome. Re-run with --confirm to do it."})
    raise SystemExit(EXIT_OK)


def _execute(op: str, args: dict, plan: dict, dry_run: bool, confirm: bool, timeout: float | None = None) -> None:
    if dry_run or not confirm:
        _preview(plan)
    try:
        reply = bridge.call(op, args, timeout=timeout or _write_timeout(), confirm=True)
    except bridge.BridgeDown as exc:
        bridge.die_down(str(exc))
    if not reply.get("ok"):
        bridge.die_reply(reply)
    emit_json(reply.get("result"))
    raise SystemExit(EXIT_OK)


def _allowlisted(url: str) -> str | None:
    pol = _local_policy()
    if not pol:
        return None
    if any(policy.matches(p, url) for p in pol["act_allow"]):
        return "act"
    if any(policy.matches(p, url) for p in pol["read_allow"]):
        return "read"
    return None


def _destination_plan(url: str) -> dict:
    pol = _local_policy()
    why = policy.denied(url, pol) if pol else None
    level = _allowlisted(url)
    out = {"url": url, "origin": policy.origin_of(url), "autoShare": level,
           "browserConfirm": level is None and not why,
           "note": ("Denied origin: the extension will refuse it (exit 4 origin_blocked)." if why else
                    "Not in read_allow/act_allow: Kevin confirms it in the browser; the tab stays unshared." if level is None
                    else f"In {level}_allow: no browser confirm; the tab is auto-shared for {level}.")}
    if why:
        out["denied"] = why
    return out


def _check_url(url: str) -> None:
    if not policy.origin_of(url):
        die(f"{url!r} is not an http(s) URL.", EXIT_USAGE, error="bad_request")


def _expect(expect_origin: str | None, expect_url: str | None, required: bool) -> dict:
    out = {}
    if expect_origin:
        if not policy.origin_of(expect_origin):
            die(f"--expect-origin {expect_origin!r} is not an http(s) origin.", EXIT_USAGE, error="bad_request")
        out["origin"] = expect_origin
    if expect_url:
        out["urlPrefix"] = expect_url
    if required and not out:
        die("Page actions need --expect-origin ORIGIN or --expect-url PREFIX (where you think the tab is).", EXIT_USAGE,
            protocol.ERROR_HINTS["missing_expect"], error="missing_expect")
    return out


def _page_target(selector, ref, text, role, find, optional: bool = False) -> dict | None:
    given = [k for k, v in (("selector", selector), ("ref", ref), ("text", text), ("find", find)) if v not in (None, "")]
    if role and text in (None, ""):
        die("--role goes with --text.", EXIT_USAGE, error="bad_target")
    if not given and optional:
        return None
    if len(given) != 1:
        die("Give exactly one target: --selector, --ref, --text (+ --role), or --find.", EXIT_USAGE, error="bad_target")
    if given[0] == "text":
        return {"text": text, **({"role": role} if role else {})}
    return {given[0]: {"selector": selector, "ref": ref, "find": find}[given[0]]}


def _verify(prefix, text) -> dict:
    return {k: v for k, v in (("urlPrefix", prefix), ("text", text)) if v}


def _value(value: str | None, value_file: Path | None, required: bool = True) -> str | None:
    if value is not None and value_file is not None:
        die("Give --value or --value-file, not both.", EXIT_USAGE, error="bad_request")
    if value_file is not None:
        try:
            raw = sys.stdin.read() if str(value_file) == "-" else value_file.expanduser().read_text()
        except OSError as exc:
            die(f"Can't read {value_file}: {exc}", EXIT_USAGE, error="bad_request")
        return raw[:-1] if raw.endswith("\n") else raw  # one trailing newline (from echo / editors) is dropped
    if value is None and required:
        die("Give the value: --value V or --value-file FILE ('-' = stdin; use a file for anything sensitive).", EXIT_USAGE,
            error="bad_request")
    return value


def _digest(value: str | None) -> str | None:
    from webcli.chromecli.host import digest

    return None if value is None else digest(value)


def page_options(f):
    for opt in reversed([
        click.option("--tab", type=int, required=True, help="Chrome tab id (from `list`)."),
        click.option("--selector", default=None, help="Target: a CSS selector matching exactly one visible element."),
        click.option("--ref", default=None, help="Target: a ref (e1..eN) from `read TAB --elements` (dies on navigation)."),
        click.option("--text", "target_text", default=None, help="Target: the element's accessible name (exact, else contains)."),
        click.option("--role", default=None, help="With --text: only elements with this role (button, link, textbox, ...)."),
        click.option("--find", default=None, help="Target by description ('the email field'): heuristics, or Jev when a key "
                     "is set. No clear winner: exit 3 ambiguous_target with candidates."),
        click.option("--jev", type=click.Choice(["auto", "on", "off"]), default="auto", show_default=True,
                     help="Jev for --find and click/press classification: auto = if a key is set; on = required "
                          "(exit 7 no key, exit 5 on failure); off. Jev only adds confirm prompts."),
        click.option("--expect-origin", default=None, help="Required (or --expect-url): the origin the tab must be on."),
        click.option("--expect-url", default=None, help="Required (or --expect-origin): a prefix the tab's URL must have."),
        click.option("--verify-url-prefix", default=None, help="After: wait <=10s for the tab URL to start with this (else exit 8)."),
        click.option("--verify-text", default=None, help="After: wait <=10s for this text on the page (else exit 8)."),
    ]):
        f = opt(f)
    return f


def _page_plan(op, tab, target, expect, verify, **extra) -> dict:
    return {"op": op, "tab": tab, "target": target, "expect": expect, **({"verify": verify} if verify else {}),
            **{k: v for k, v in extra.items() if v is not None},
            "browserConfirm": "decided in Chrome from the element (submit-like clicks, credential/payment fields, "
                              "Enter in a form field)"}


def _page(op, tab, selector, ref, target_text, role, find, jev, expect_origin, expect_url, verify_url_prefix,
          verify_text, dry_run, confirm, *, optional_target=False, value=None, key=None, plan_extra=None) -> None:
    target = _page_target(selector, ref, target_text, role, find, optional=optional_target)
    expect = _expect(expect_origin, expect_url, required=True)
    verify = _verify(verify_url_prefix, verify_text)
    args = {"tab": tab, "target": target, "expect": expect, "verify": verify, "jev": jev}
    if value is not None:
        args["value"] = value
    if key is not None:
        args["key"] = key
    plan = _page_plan(op, tab, target, expect, verify, jev=jev, **(plan_extra or {}))
    _execute(f"page.{op}", args, plan, dry_run, confirm, timeout=_write_timeout(jev))


@group.command("open")
@click.argument("url")
@click.option("--active", is_flag=True, help="Open in the foreground (default: a background tab).")
@click.option("--window", "window", type=click.Choice(["new"]), default=None, help="Open in a new window.")
@write_options
def open_cmd(url, active, window, dry_run, confirm) -> None:
    """Open URL in a new tab (WRITES; preview unless --confirm). Bot-opened tabs are auto-shared for act if the origin
    is in act_allow, for read if in read_allow, else left unshared. Outside both lists Kevin confirms in Chrome."""
    _check_url(url)
    args = {"url": url, "active": active, "window": window}
    _execute("tab.open", args, {"op": "open", **_destination_plan(url), "active": active, "window": window or "current"},
             dry_run, confirm)


@group.command("focus")
@click.argument("tab", type=int)
@write_options
def focus_cmd(tab, dry_run, confirm) -> None:
    """Bring TAB and its window to the front (WRITES; preview unless --confirm). Needs Share-act or a bot-opened tab.
    Makes `screenshot` possible."""
    _execute("tab.focus", {"tab": tab}, {"op": "focus", "tab": tab, "needs": "Share-act or bot-opened"}, dry_run, confirm)


@group.command("close")
@click.argument("tab", type=int)
@write_options
def close_cmd(tab, dry_run, confirm) -> None:
    """Close TAB (WRITES; preview unless --confirm). Only act-shared or bot-opened tabs; anything else exits 4."""
    _execute("tab.close", {"tab": tab}, {"op": "close", "tab": tab, "needs": "Share-act or bot-opened"}, dry_run, confirm)


@group.command("navigate")
@click.argument("tab", type=int)
@click.argument("url")
@click.option("--expect-origin", default=None, help="Refuse (exit 5 url_mismatch) unless the tab is on this origin.")
@click.option("--expect-url", default=None, help="Refuse (exit 5 url_mismatch) unless the tab's URL starts with this.")
@write_options
def navigate_cmd(tab, url, expect_origin, expect_url, dry_run, confirm) -> None:
    """Load URL in TAB (WRITES; preview unless --confirm). Needs Share-act or a bot-opened tab. Going to another origin
    ends the old share; the tab becomes bot-opened and follows the auto-share rules. Outside read_allow/act_allow Kevin
    confirms in Chrome; denied origins exit 4."""
    _check_url(url)
    expect = _expect(expect_origin, expect_url, required=False)
    args = {"tab": tab, "url": url, **({"expect": expect} if expect else {})}
    nplan = _destination_plan(url)
    if nplan["browserConfirm"]:
        nplan["note"] += " Exception: a same-origin move in a tab Kevin Share-act'd himself isn't confirmed."
    _execute("tab.navigate", args, {"op": "navigate", "tab": tab, **nplan, "expect": expect or None},
             dry_run, confirm)


@group.command("click")
@page_options
@write_options
def click_cmd(tab, selector, ref, target_text, role, find, jev, expect_origin, expect_url, verify_url_prefix, verify_text,
              dry_run, confirm) -> None:
    """Click an element in an act-shared tab (WRITES; preview unless --confirm). Submit-like elements (form submit
    buttons; labels such as Buy, Order, Pay, Send, Post, Delete, Confirm, Sign out, ...) wait for Kevin in Chrome."""
    _page("click", tab, selector, ref, target_text, role, find, jev, expect_origin, expect_url, verify_url_prefix,
          verify_text, dry_run, confirm)


@group.command("fill")
@page_options
@click.option("--value", default=None, help="The new value (replaces the field's content).")
@click.option("--value-file", "--text-file", "value_file", type=click.Path(path_type=Path, dir_okay=False,
              allow_dash=True), default=None, help="Read the value from a file ('-' = stdin). Use this for anything sensitive.")
@write_options
def fill_cmd(tab, selector, ref, target_text, role, find, jev, expect_origin, expect_url, verify_url_prefix, verify_text,
             value, value_file, dry_run, confirm) -> None:
    """Set a field's value (WRITES; preview unless --confirm), then read it back (mismatch: exit 8). Password, payment
    (cc-*), one-time-code, and card/CVV/IBAN/routing/SSN fields wait for Kevin in Chrome, value masked. The value is
    logged only as <N chars sha256:8>."""
    v = _value(value, value_file)
    _page("fill", tab, selector, ref, target_text, role, find, jev, expect_origin, expect_url, verify_url_prefix,
          verify_text, dry_run, confirm, value=v, plan_extra={"value": _digest(v)})


@group.command("type")
@page_options
@click.option("--value", default=None, help="Text to type (appended, key by key).")
@click.option("--value-file", "--text-file", "value_file", type=click.Path(path_type=Path, dir_okay=False,
              allow_dash=True), default=None, help="Read the text from a file ('-' = stdin). Use this for anything sensitive.")
@write_options
def type_cmd(tab, selector, ref, target_text, role, find, jev, expect_origin, expect_url, verify_url_prefix, verify_text,
             value, value_file, dry_run, confirm) -> None:
    """Type text into a field key by key (WRITES; preview unless --confirm), then read it back (exit 8 if it differs).
    Same gates as fill. Logged only as <N chars sha256:8>."""
    v = _value(value, value_file)
    _page("type", tab, selector, ref, target_text, role, find, jev, expect_origin, expect_url, verify_url_prefix,
          verify_text, dry_run, confirm, value=v, plan_extra={"value": _digest(v)})


@group.command("select")
@page_options
@click.option("--value", required=True, help="The option's value, or its visible label.")
@write_options
def select_cmd(tab, selector, ref, target_text, role, find, jev, expect_origin, expect_url, verify_url_prefix, verify_text,
               value, dry_run, confirm) -> None:
    """Choose an option in a <select> (WRITES; preview unless --confirm), then read it back (exit 8 if it differs)."""
    _page("select", tab, selector, ref, target_text, role, find, jev, expect_origin, expect_url, verify_url_prefix,
          verify_text, dry_run, confirm, value=value, plan_extra={"value": _digest(value)})


@group.command("press")
@click.argument("key")
@page_options
@write_options
def press_cmd(key, tab, selector, ref, target_text, role, find, jev, expect_origin, expect_url, verify_url_prefix,
              verify_text, dry_run, confirm) -> None:
    """Press KEY (Enter, Escape, Tab, a letter, ...) on the target, or the focused element when no target is given
    (WRITES; preview unless --confirm). Enter in a form field submits the form, so it waits for Kevin in Chrome."""
    _page("press", tab, selector, ref, target_text, role, find, jev, expect_origin, expect_url, verify_url_prefix,
          verify_text, dry_run, confirm, optional_target=True, key=key, plan_extra={"key": key})


@group.command("wait")
@click.option("--tab", type=int, required=True, help="Chrome tab id (Share-read is enough).")
@click.option("--selector", default=None, help="Wait for a visible element matching this CSS selector.")
@click.option("--text", "text", default=None, help="Wait for this text on the page.")
@click.option("--url-prefix", default=None, help="Wait for the tab's URL to start with this.")
@click.option("--timeout", type=click.FloatRange(0.1, 120), default=15, show_default=True, help="Seconds.")
@agent_output_options(rows=False)
def wait_cmd(tab, selector, text, url_prefix, timeout, fmt, quiet) -> None:
    """Wait until a selector / text / URL prefix shows up in a shared tab (read-only). Exit 5 timeout if it doesn't."""
    given = [x for x in (selector, text, url_prefix) if x not in (None, "")]
    if len(given) != 1:
        die("Give exactly one of --selector, --text, --url-prefix.", EXIT_USAGE, error="bad_target")
    args = {"tab": tab, "timeoutMs": int(timeout * 1000)}
    if selector:
        args["selector"] = selector
    elif text:
        args["text"] = text
    else:
        args["urlPrefix"] = url_prefix
    emit_json(bridge.request("page.wait", args, timeout=timeout + max(bridge.timeout_seconds(), 10)))
    raise SystemExit(EXIT_OK)


@group.command("find")
@click.option("--tab", type=int, required=True, help="Chrome tab id (Share-read is enough).")
@click.argument("intent")
@click.option("--jev", type=click.Choice(["auto", "on", "off"]), default="auto", show_default=True,
              help="auto = Jev if a key is set in the extension's options; on = required (exit 7 no key, exit 5 on "
                   "failure); off = heuristics only.")
@click.option("--limit", type=click.IntRange(1, 50), default=5, show_default=True)
@agent_output_options()
def find_cmd(tab, intent, jev, limit, fmt, compact, select, as_csv, quiet) -> None:
    """Find the element INTENT describes ('the email field') in a shared tab (read-only). Rows: ref, score, source
    (jev|heuristic), tag, role, name; use the ref with --ref. Jev never sees field values."""
    res = bridge.request("page.find", {"tab": tab, "intent": intent, "jev": jev, "limit": limit},
                         timeout=max(bridge.timeout_seconds(), 45))
    info = (res or {}).get("jev") or {}
    if info.get("error"):
        click.echo(json.dumps({"warning": "jev_unavailable", "message": info["error"],
                               "hint": "Results are heuristic (--jev auto fallback)."}), err=True)
    emit_rows((res or {}).get("results") or [], title="Candidates", fields=FIND_FIELDS,
              compact_fields=("ref", "score", "name"), columns=["ref", "score", "source", "role", "name"],
              noun="candidates", fmt=fmt, compact=compact, select=select, quiet=quiet, as_csv=as_csv)
    raise SystemExit(EXIT_OK)


@group.command("eval")
@click.option("--tab", type=int, required=True, help="Chrome tab id.")
@click.option("--code", default=None, help="JavaScript to run.")
@click.option("--file", "file", type=click.Path(path_type=Path, dir_okay=False, exists=True), default=None,
              help="Read the JavaScript from a file.")
@click.option("--expect-origin", default=None, help="Required (or --expect-url).")
@click.option("--expect-url", default=None, help="Required (or --expect-origin).")
@write_options
def eval_cmd(tab, code, file, expect_origin, expect_url, dry_run, confirm) -> None:
    """Run JavaScript in a tab: always browser-confirmed. DISABLED in this version (exit 7 eval_disabled): Chrome's MV3
    isolated-world CSP forbids running code strings. The code is logged only as <N chars sha256:8>."""
    if (code is None) == (file is None):
        die("Give --code or --file (one of them).", EXIT_USAGE, error="bad_request")
    src = code if code is not None else file.read_text()
    expect = _expect(expect_origin, expect_url, required=True)
    plan = {"op": "eval", "tab": tab, "expect": expect, "code": _digest(src), "browserConfirm": "always",
            "note": "eval is disabled in this version: the extension answers exit 7 eval_disabled."}
    _execute("page.eval", {"tab": tab, "code": src, "expect": expect}, plan, dry_run, confirm)


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
