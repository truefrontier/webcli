"""chromecli-host: the native messaging host Chrome spawns for the chromecli extension.

stdin/stdout: Chrome's native framing (protocol.read_frame / encode_frame). It refuses to run unless argv[1] is the
extension's origin, takes flock(run/chromecli.lock) (a second host answers {"event":"error","code":"host_busy"} and
exits), serves run/chromecli.sock (0600 in the 0700 run/ dir; one JSON line each way per connection), forwards each
CLI request to the extension with an id and a timeout, pushes the [chromecli] policy on start and whenever
config.toml changes (2s mtime poll), writes one redacted audit line per op to logs/chromecli.log, and exits on stdin
EOF (Chrome closed the port), unlinking the socket. It never prints anything but frames on stdout.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, BinaryIO

from webcli import __version__
from webcli.chromecli import LOCK_NAME, LOG_NAME, SOCKET_NAME, install, policy, protocol, recording
from webcli.core import config
from webcli.core.errors import ConfigError
from webcli.core.redact import redact_text, redact_url

DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 900.0
POLL_SECONDS = 2.0
SUN_PATH_MAX = 104


FRAME_ACT_OPS = {"page.click", "page.fill", "page.type", "page.select", "page.press", "page.probe"}  # 0.11.3


def audit(line: str) -> None:
    """One redacted line in logs/chromecli.log (0600, rotated). Never raises."""
    try:
        from webcli.core.output import logger

        logger(LOG_NAME).info(redact_text(line))
    except Exception:
        pass


def digest(text: str) -> str:
    """How a value the bot typed (a query, later typed text) appears in the log: its size and a short hash."""
    return f"<{len(text)} chars sha256:{hashlib.sha256(text.encode()).hexdigest()[:8]}>"


def socket_path() -> Path:
    return config.run_dir() / SOCKET_NAME


def listen(path: Path) -> socket.socket:
    """The unix socket, created 0600 (umask 0177) in its 0700 directory (core/daemon.listen's pattern)."""
    if len(str(path).encode()) >= SUN_PATH_MAX:
        raise OSError(f"Socket path too long for macOS ({len(str(path))} bytes): {path}")
    path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        server.bind(str(path))
    finally:
        os.umask(old)
    path.chmod(0o600)
    server.listen(16)
    return server


def take_lock() -> Any | None:
    """flock(run/chromecli.lock), non-blocking: the open file (keep it), or None when another host holds it."""
    fd = os.open(config.run_dir() / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(fd, "r+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def _target_summary(target: Any) -> str:
    """How a page-action target is logged: selector / ref / role as given (redacted), text as a digest."""
    if not isinstance(target, dict):
        return ""
    if target.get("ref"):
        return f"target=ref:{redact_text(str(target['ref']))[:40]}"
    if target.get("selector"):
        return f"target=selector:{redact_text(str(target['selector']))[:120]}"
    if target.get("text") is not None:
        role = f" role={redact_text(str(target['role']))[:30]}" if target.get("role") else ""
        return f"target=text:{digest(str(target['text']))}{role}"
    if target.get("find") is not None:
        return f"target=find:{digest(str(target['find']))}"
    return ""


def _brief(el: dict) -> str:
    """tag#id[role] of an element description, for the audit line (no names or text)."""
    tag = re.sub(r"[^a-z0-9-]", "", str(el.get("tag") or "?").lower())[:16] or "?"
    ident = f"#{redact_text(str(el['id']))[:40]}" if el.get("id") else ""
    role = f"[{redact_text(str(el['role']))[:20]}]" if el.get("role") else ""
    return (tag + ident + role).replace(" ", "_")


def _grant_summary(args: dict) -> str:
    """One grant's fields for the audit line (values only as digests)."""
    parts = [f"grant_op={redact_text(str(args.get('op')))[:10]} origin={redact_url(str(args.get('origin')))}"]
    t = _target_summary(args.get("target"))
    if t:
        parts.append(t)
    if args.get("value") is not None:
        parts.append(f"value={digest(str(args['value']))}")
    if args.get("urlPrefix"):
        parts.append(f"url_prefix={redact_url(str(args['urlPrefix']))}")
    if args.get("frameOrigin"):  # 0.11.3
        parts.append(f"frame_origin={redact_url(str(args['frameOrigin']))}")
    if args.get("maxAmount") is not None:
        parts.append(f"max_amount={redact_text(str(args['maxAmount']))[:16]} {redact_text(str(args.get('currency') or 'USD'))[:3]}")
    # 0.11.5: no ttlSec -> the quote's time window (else 30m), set in the extension
    parts.append(f"uses={args.get('uses', 1)} ttl_s={args.get('ttlSec', 'default')}")
    return " ".join(parts)


def _clause_log(text) -> str:
    return json.dumps(redact_text(str(text))[:120], ensure_ascii=False)


def _summary(op: str, args: dict) -> str:
    """The op's arguments for the audit line. Typed / filled / selected values and eval code appear only as
    <N chars sha256:8>; URLs go through redact_url."""
    parts = []
    if "tab" in args:
        parts.append(f"tab={args.get('tab')}")
    if args.get("active"):
        parts.append("tab=active")
    if op == "tabs.read":
        parts.append(f"as={args.get('as', 'text')} elements={bool(args.get('elements'))}")
    if op == "history.search" and args.get("query"):
        parts.append(f"query={digest(str(args['query']))}")
    if op in ("tab.open", "tab.navigate") and args.get("url"):
        parts.append(f"to={redact_url(str(args['url']))}")
    if op.startswith("page."):
        t = _target_summary(args.get("target"))
        if t:
            parts.append(t)
        if args.get("value") is not None:
            parts.append(f"value={digest(str(args['value']))}")
        if args.get("code") is not None:
            parts.append(f"code={digest(str(args['code']))}")
        if args.get("key"):
            parts.append(f"key={redact_text(str(args['key']))[:20]}")
        if args.get("intent") is not None:
            parts.append(f"intent={digest(str(args['intent']))}")
        if args.get("jev"):
            parts.append(f"jev={redact_text(str(args['jev']))[:4]}")
        if op == "page.wait":
            for k in ("selector", "text"):
                if args.get(k) is not None:
                    parts.append(f"{k}={digest(str(args[k]))}")
            if args.get("urlPrefix"):
                parts.append(f"urlPrefix={redact_url(str(args['urlPrefix']))}")
    if op == "page.scroll":  # 0.10.2
        if args.get("measure"):
            parts.append("measure=1")
        to = args.get("to")
        if isinstance(to, dict):
            parts.append(f"to={redact_text(str(to.get('top')))[:12]}px")
        elif to is not None:
            parts.append(f"to={redact_text(str(to))[:8]}")
        if args.get("by") is not None:
            parts.append(f"by={redact_text(str(args['by']))[:12]}{' up' if args.get('dir') == -1 else ''}")
        for k in ("into", "container"):
            if args.get(k):
                parts.append(f"{k}={redact_text(str(args[k]))[:120]}")
        if isinstance(args.get("frameId"), int) and args["frameId"] > 0:  # 0.11.1
            parts.append(f"frame={args['frameId']}")
        if args.get("path") is not None:
            parts.append("path=1")
    if op == "tabs.screenshot" and args.get("part"):  # one viewport of `screenshot --full-page`
        parts.append(f"full_page_part={redact_text(str(args['part']))[:4]}")
    if op == "tabs.screenshot" and args.get("covered"):  # 0.12.1
        parts.append("covered=1")
    if op == "record.start":  # 0.12.1: the file name only (paths look like tokens to the redactor)
        parts.append(f"file={recording.log_name(Path(str(args.get('out') or '')).name)} fps={args.get('fps', 30)} audio={args.get('audio') is True} "
                     f"max_s={args.get('maxSec', 600)}")
    if op == "record.stop" and args.get("all"):
        parts.append("all=1")
    if op == "tabs.list" and args.get("all"):
        parts.append("all=1")
    if op == "page.probe" and args.get("op"):
        parts.append(f"probe_op={redact_text(str(args['op']))[:10]}")
    if op == "grant.add":
        parts.append(_grant_summary(args))
        parts.append(f"user_approved={args.get('userApproved') is True}")
        # Kevin's words, through the same redaction as everything else.
        parts.append(f"quote={json.dumps(redact_text(str(args.get('quote') or ''))[:500], ensure_ascii=False)}")
    if op == "grant.add_batch":  # 0.11.5: one quote, several grants
        grants = args.get("grants") if isinstance(args.get("grants"), list) else []
        parts.append(f"grants={len(grants)} partial={args.get('partial') is True} user_approved={args.get('userApproved') is True}")
        for i, g in enumerate(grants[:20], 1):
            if isinstance(g, dict):
                parts.append(f"[{i}] {_grant_summary(g)}")
        parts.append(f"quote={json.dumps(redact_text(str(args.get('quote') or ''))[:500], ensure_ascii=False)}")
    if op == "grant.revoke":
        parts.append("all=1" if args.get("all") else f"id={redact_text(str(args.get('id')))[:20]}")
    expect = args.get("expect")
    if isinstance(expect, dict):
        if expect.get("origin"):
            parts.append(f"expect_origin={redact_url(str(expect['origin']))}")
        if expect.get("urlPrefix"):
            parts.append(f"expect_url={redact_url(str(expect['urlPrefix']))}")
    return " ".join(parts)


class Host:
    def __init__(self, stdin: BinaryIO, stdout: BinaryIO) -> None:
        self.stdin, self.stdout = stdin, stdout
        self.write_lock = threading.Lock()
        self.pending: dict[str, dict] = {}
        self.pending_lock = threading.Lock()
        self.stopped = threading.Event()
        self.hello: dict | None = None
        self.last_event: dict | None = None
        self.policy_ack: dict | None = None
        self.started = time.time()
        self.policy: dict | None = None
        self.config_error: str | None = None
        self.config_warnings: list[str] = []
        self.config_stamp: tuple | None = None
        self.server: socket.socket | None = None
        self.sock_path: Path | None = None
        self.recordings = recording.Recordings()
        self.rec_drops: dict[str, int] = {}

    # extension side
    def send(self, message: dict) -> None:
        frame = protocol.encode_frame(message, protocol.HOST_TO_EXT_MAX)
        with self.write_lock:
            self.stdout.write(frame)
            self.stdout.flush()

    def read_loop(self) -> None:
        try:
            while not self.stopped.is_set():
                try:
                    msg = protocol.read_frame(self.stdin, protocol.EXT_TO_HOST_MAX)
                except protocol.ProtocolError as exc:
                    audit(f"event=bad_frame code={exc.code} {exc.message}")
                    break
                if msg is None:
                    break
                if isinstance(msg, dict) and "event" in msg:
                    self.on_event(msg)
                elif isinstance(msg, dict) and msg.get("id"):
                    with self.pending_lock:
                        slot = self.pending.get(str(msg["id"]))
                    if slot is not None:
                        slot["reply"] = msg
                        slot["done"].set()
        finally:
            self.stop("stdin closed")

    def on_event(self, msg: dict) -> None:
        kind = msg.get("event")
        if kind == "hello":
            self.hello = {k: msg.get(k) for k in ("extVersion", "extensionId", "paused", "shares", "jevConfigured",
                                                  "policyHash", "policyPending")}
            shares = msg.get("shares") if isinstance(msg.get("shares"), list) else []
            audit(f"event=hello ext={msg.get('extVersion')} paused={msg.get('paused')} shares={len(shares)} "
                  f"policy {msg.get('policyHash')}")
            if self.policy and msg.get("policyHash") != self.policy["hash"]:
                self.push_policy()
        elif kind == "policy_ack":
            self.policy_ack = {"policyHash": msg.get("policyHash"), "accepted": msg.get("accepted"),
                               "pending": msg.get("pending")}
            audit(f"event=policy_ack policy {msg.get('policyHash')} accepted={msg.get('accepted')}")
        elif kind in ("share_changed", "paused_changed"):
            self.last_event = {"event": kind, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            if kind == "paused_changed" and self.hello is not None:
                self.hello["paused"] = msg.get("paused")
            origin = redact_url(str(msg.get("origin"))) if msg.get("origin") else ""
            # 0.16.3: a share that followed its tab to another host of the same site says from where and which site.
            carried = ""
            if msg.get("from"):
                carried += f" from={redact_url(str(msg.get('from')))}"
            if isinstance(msg.get("site"), str) and re.fullmatch(r"[a-z0-9.-]{1,253}", msg["site"]):
                carried += f" site={msg['site']}"
            reason = msg.get("reason")
            reason = reason.replace(" ", "_") if isinstance(reason, str) and reason == "same site" else reason
            audit(f"event={kind} tab={msg.get('tab')} origin={origin} level={msg.get('level')} "
                  f"paused={msg.get('paused')} reason={reason}{carried}")
        elif kind == "grants_cleared":
            audit(f"event={kind} count={msg.get('count')} reason={redact_text(str(msg.get('reason')))[:60]}")
        elif kind in ("grant_added", "grant_refused"):
            # Jev's scope verdict, with Kevin's words (redacted like everything else).
            narrowed = ",".join(str(x) for x in (msg.get("narrowed") or []))[:60]
            audit(f"event={kind} grant={redact_text(str(msg.get('grant')))[:20]} op={redact_text(str(msg.get('op')))[:10]} "
                  f"origin={redact_url(str(msg.get('origin')))} verdict={redact_text(str(msg.get('verdict')))[:12]} "
                  f"confidence={msg.get('confidence')} narrowed={narrowed or '-'} "
                  + (f"reason={json.dumps(redact_text(str(msg.get('reason')))[:300], ensure_ascii=False)} " if msg.get("reason") else "")
                  + (f"clause={_clause_log(msg['clause'])} " if msg.get("clause") else "")  # 0.11.5
                  + f"quote={json.dumps(redact_text(str(msg.get('quote') or ''))[:500], ensure_ascii=False)}")
        elif kind == "rec_chunk":  # 0.12.1: appended to the recording's file as it comes
            why = self.recordings.chunk(msg)
            if why:
                rid = str(msg.get("recId"))[:32]
                self.rec_drops[rid] = self.rec_drops.get(rid, 0) + 1
                if self.rec_drops[rid] == 1:
                    audit(f"event=rec_chunk_dropped rec={redact_text(rid)} seq={msg.get('seq')} why={redact_text(why)[:160]}")
        elif kind == "rec_waiting":
            self.recordings.waiting(msg)
            audit(f"event=record_waiting tab={msg.get('tab')} origin={redact_url(str(msg.get('origin')))} "
                  f"rec={redact_text(str(msg.get('recId'))[:8])} why=needs Kevin's Record in the popup")
        elif kind == "rec_started":
            r = self.recordings.started(msg)
            if r:
                audit(f"event=record_start tab={r['tab']} origin={redact_url(str(r['origin']))} file={recording.log_name(r['path'].name)} "
                      f"rec={r['recId'][:8]} mime={redact_text(str(r['mime']))[:40]} size={r['width']}x{r['height']} fps={r['fps']} "
                      f"audio={r['audio'] is True} max_s={r['maxSec']} via={redact_text(str(msg.get('via')))[:10]}")
        elif kind == "rec_stopped":
            r = self.recordings.stopped(msg)
            if r:
                dur = (r["durationMs"] or 0) / 1000
                audit(f"event=record_stop tab={r['tab']} origin={redact_url(str(r['origin'] or msg.get('origin')))} "
                      f"file={recording.log_name(r['path'].name)} rec={r['recId'][:8]} state={r['state']} duration_s={dur:.1f} "
                      f"bytes={r['bytes']} chunks={r['nextSeq']} reason={redact_text(str(r['reason']))[:80]}"
                      + (f" error={json.dumps(redact_text(str(r['error']))[:200])}" if r["error"] else ""))
        else:
            audit(f"event={redact_text(str(kind))}")

    # config / policy
    def _stamp(self) -> tuple | None:
        try:
            st = (config.home() / config.CONFIG_NAME).stat()
            return (st.st_mtime_ns, st.st_size, st.st_ino)
        except FileNotFoundError:
            return None

    def reload_policy(self) -> bool:
        """Re-read [chromecli]; True when the policy changed."""
        self.config_stamp = self._stamp()
        try:
            new, warnings = policy.load_checked()
        except ConfigError as exc:
            if self.config_error != str(exc):
                audit(f"event=config_error {exc}")
            self.config_error = str(exc)
            return False
        self.config_error = None
        if warnings != self.config_warnings:
            for w in warnings:  # unknown [chromecli] keys: ignored, never fatal, never loosening
                audit(f"event=config_warning {w}")
        self.config_warnings = warnings
        if self.policy and new["hash"] == self.policy["hash"]:
            return False
        self.policy = new
        return True

    def push_policy(self) -> None:
        if self.policy:
            try:
                self.send({"event": "policy", "policy": self.policy})
                audit(f"event=policy_push policy {self.policy['hash']}")
            except (OSError, protocol.ProtocolError) as exc:
                audit(f"event=policy_push_failed {exc}")

    def watch_config(self) -> None:
        while not self.stopped.wait(POLL_SECONDS):
            if self._stamp() != self.config_stamp and self.reload_policy():
                self.push_policy()

    # CLI side
    def host_info(self) -> dict:
        return {"pid": os.getpid(), "version": __version__, "socket": str(self.sock_path),
                "since": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.started)),
                "helloReceived": self.hello is not None,
                "policyHash": (self.policy or {}).get("hash"), "configError": self.config_error,
                "configWarnings": list(self.config_warnings), "policyAck": self.policy_ack}

    def forward(self, req: dict) -> dict:
        req_id = uuid.uuid4().hex[:16]
        try:
            timeout = float(req["client"].get("timeoutMs") or DEFAULT_TIMEOUT * 1000) / 1000
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        timeout = min(max(timeout, 1.0), MAX_TIMEOUT)
        slot = {"done": threading.Event(), "reply": None}
        # The extension drops a queued in-browser confirm once nobody waits for the answer (deadline, epoch ms).
        deadline_ms = int((time.time() + timeout) * 1000)
        with self.pending_lock:
            self.pending[req_id] = slot
        try:
            try:
                self.send({"v": protocol.VERSION, "id": req_id, "op": req["op"], "args": req["args"],
                           "confirm": req["confirm"], "deadlineMs": deadline_ms})
            except protocol.ProtocolError as exc:
                return protocol.reply_error(req_id, exc.code, exc.message)
            except OSError as exc:
                return protocol.reply_error(req_id, "bridge_down", f"Can't write to Chrome: {exc}")
            deadline = time.monotonic() + timeout
            while not slot["done"].wait(0.2):
                if self.stopped.is_set():
                    return protocol.reply_error(req_id, "bridge_down", "Chrome closed the connection.")
                if time.monotonic() >= deadline:
                    return protocol.reply_error(req_id, "timeout", f"The extension didn't answer {req['op']} "
                                                                   f"within {timeout:g}s.")
            reply = slot["reply"]
        finally:
            with self.pending_lock:
                self.pending.pop(req_id, None)
        if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
            return protocol.reply_error(req_id, "bad_request", "The extension sent a malformed reply.")
        return reply

    def handle(self, line: bytes) -> dict:
        start = time.monotonic()
        op, args = "?", {}
        try:
            req = protocol.validate_request(json.loads(line.decode("utf-8")))
            op, args = req["op"], req["args"]
        except protocol.ProtocolError as exc:
            reply = protocol.reply_error(None, exc.code, exc.message)
        except (UnicodeDecodeError, json.JSONDecodeError):
            reply = protocol.reply_error(None, "bad_request", "A request is one line of JSON.")
        else:
            if self.config_error and op != "status":
                # The host's version (and the extension's) let the CLI say "reload the extension" when they differ.
                reply = protocol.reply_error(None, "config_error", self.config_error, hostVersion=__version__,
                                             extVersion=(self.hello or {}).get("extVersion"))
            elif op in protocol.HOST_OPS:
                reply = {"v": protocol.VERSION, "id": None, "ok": True, "result": {"recordings": self.recordings.all()}}
            elif op == "record.start":
                reply = self.record_start(req)
            else:
                reply = self.forward(req)
                if op in ("record.stop", "record.status") and reply.get("ok") and isinstance(reply.get("result"), dict):
                    self.enrich(reply["result"])
            if op == "status" and reply.get("ok") and isinstance(reply.get("result"), dict):
                reply["result"]["host"] = self.host_info()
        self.audit_op(op, args, reply, (time.monotonic() - start) * 1000)
        return reply

    # 0.12.1: recordings. The CLI names the file; the extension only ever sees the host's recId.
    def record_start(self, req: dict) -> dict:
        args = dict(req["args"])
        out = args.pop("out", None)
        if not isinstance(out, str) or not os.path.isabs(out) or not out.endswith(".webm") or os.path.isdir(out):
            return protocol.reply_error(None, "bad_request", "record.start needs out: an absolute path ending in .webm.")
        requested = args.pop("requestedOut", None)
        rec_id = self.recordings.register(Path(out), requested if isinstance(requested, str) else None, args.get("tab"),
                                          fps=args.get("fps"), audio=args.get("audio") is True, max_sec=args.get("maxSec"))
        args["recId"] = rec_id
        reply = self.forward({**req, "args": args})
        if not reply.get("ok"):
            self.recordings.drop(rec_id)
            return reply
        if isinstance(reply.get("result"), dict):
            reply["result"]["recId"] = rec_id
            self.enrich(reply["result"])
        return reply

    def enrich(self, result: dict) -> None:
        """Add the host's side (path, bytes written, write errors) to each recording row of a reply."""
        def add(row: Any) -> None:
            if isinstance(row, dict) and isinstance(row.get("recId"), str):
                mine = self.recordings.view(row["recId"])
                if mine:
                    row.update({k: mine[k] for k in ("path", "file", "requested", "bytesWritten")})
                    if mine.get("error"):
                        row["writeError"] = mine["error"]
        add(result)
        for key in ("recordings", "stopped", "finished"):
            for row in result.get(key) or []:
                add(row)

    def audit_op(self, op: str, args: dict, reply: dict, ms: float) -> None:
        result = reply.get("result") if isinstance(reply.get("result"), dict) else {}
        err = reply.get("error") if isinstance(reply.get("error"), dict) else {}
        url = result.get("url") or err.get("url")
        origin = result.get("origin") or err.get("origin")
        code = None if reply.get("ok") else str(err.get("code") or "error")
        where = " ".join(x for x in (f"origin={redact_url(str(origin))}" if origin else "",
                                     f"url={redact_url(str(url))}" if url else "") if x)
        info = result if reply.get("ok") else err
        gate = ""
        if isinstance(info.get("reasons"), list) and info.get("reasons"):
            reasons = ",".join(redact_text(str(r))[:40] for r in info["reasons"][:10])
            gate = f" gated={reasons} confirmed={bool(info.get('confirmedInBrowser'))}"
        if code in ("declined", "confirm_timeout"):
            gate += f" confirm={code}"
            # 0.16.2: how the in-browser confirm was answered (via=click / escape / window_closed / ...), ms after the
            # window appeared, and the keys / clicks its first second ignored. Short keys: the log redacts 20+ char tokens.
            if isinstance(info.get("answeredBy"), str) and info["answeredBy"].isidentifier():
                gate += f" via={info['answeredBy'][:16]}"
            for key, name in (("answeredAfterMs", "answer_ms"), ("ignoredKeys", "ignored_keys"),
                              ("ignoredClicks", "ignored_clicks")):
                if isinstance(info.get(key), int) and not isinstance(info.get(key), bool):
                    gate += f" {name}={info[key]}"
        # 0.11.3: an action inside an iframe (frame_act_allow) names the frame and its origin.
        if isinstance(info.get("frameId"), int) and info["frameId"] > 0 and op in FRAME_ACT_OPS:
            gate += f" frame={info['frameId']}"
            if info.get("frameOrigin"):
                gate += f" frame_origin={redact_url(str(info['frameOrigin']))}"
        if isinstance(info.get("denyExcept"), str):  # 0.11.3: a deny_except entry is what let this URL through
            gate += f" deny_except={redact_url(info['denyExcept'])[:200]}"
        if info.get("tabKind") in ("act", "bot", "read"):
            gate += f" tab_kind={info['tabKind']}"
        ji = info.get("jevInfo") if isinstance(info.get("jevInfo"), dict) else None
        if ji and isinstance(ji.get("choice"), str):
            gate += f" jev_info={redact_text(ji['choice'])[:20]}:{'gate' if ji.get('gated') else 'info'}"
        # 0.10.0: a pre-approval answered the gate instead of Kevin's click.
        if isinstance(info.get("confirmedBy"), str) and info["confirmedBy"].startswith("grant:"):
            g = info.get("grant") if isinstance(info.get("grant"), dict) else {}
            gate += f" grant_used={redact_text(info['confirmedBy'][6:])[:24]}"
            if g.get("quote"):
                gate += f" grant_quote={json.dumps(redact_text(str(g['quote']))[:500], ensure_ascii=False)}"
            if g.get("amount") is not None:
                gate += f" grant_amount={redact_text(str(g['amount']))[:16]}"
        if op == "grant.add" and reply.get("ok") and isinstance(result.get("grant"), dict):
            gate += f" grant={redact_text(str(result['grant'].get('id')))[:20]}"
        if op == "grant.add" and isinstance(info.get("clause"), dict) and info["clause"].get("text"):  # 0.11.5
            gate += f" clause={_clause_log(info['clause']['text'])}"
        if op == "grant.add_batch" and isinstance(info.get("results"), list):
            # 0.11.5: per grant: verdict:confidence, the matched clause, the stored grant id (or -)
            if reply.get("ok"):
                gate += f" added={info.get('added')} refused={info.get('refused')}"
            for r in info["results"][:20]:
                if not isinstance(r, dict):
                    continue
                g = r.get("grant") if isinstance(r.get("grant"), dict) else {}
                cl = r.get("clause") if isinstance(r.get("clause"), dict) else {}
                i = r.get("index")
                gate += (f" g{i}={redact_text(str(r.get('verdict')))[:12]}:{r.get('confidence')}"
                         f" grant{i}={redact_text(str(g.get('id') or '-'))[:20]}"
                         f" clause{i}={_clause_log(cl['text']) if cl.get('text') else '-'}")
        jc = info.get("jevCheck") if op == "grant.add" and isinstance(info.get("jevCheck"), dict) else None
        if jc:  # 0.10.1: Jev's scope verdict on the grant (the quote itself is in the summary)
            narrowed = ",".join(str(n.get("field")) for n in (jc.get("narrowed") or []) if isinstance(n, dict))[:60]
            gate += (f" jev_verdict={redact_text(str(jc.get('verdict')))[:12]} jev_confidence={jc.get('confidence')}"
                     f" narrowed={narrowed or '-'}")
            if jc.get("reason"):
                gate += f" jev_reason={json.dumps(redact_text(str(jc['reason']))[:300], ensure_ascii=False)}"
        res = info.get("resolved") if isinstance(info.get("resolved"), dict) else None
        if res and isinstance(res.get("from"), dict):  # 0.17.1: a non-HTML click target and what it resolved to
            el = info.get("element") if isinstance(info.get("element"), dict) else {}
            gate += f" resolved={_brief(el)}<-{_brief(res['from'])} via={redact_text(str(res.get('via')))[:20]}"
        opened = info.get("openedTabs") if isinstance(info.get("openedTabs"), list) else []
        if opened:
            gate += " opened=" + ",".join(f"{o.get('id')}:{o.get('share') or 'none'}" for o in opened[:10] if isinstance(o, dict))
        jev = info.get("jev") if isinstance(info.get("jev"), dict) else None
        if jev:
            gate += f" jev_used={bool(jev.get('used'))}" + (" jev_error=1" if jev.get("error") else "")
        audit(f"op={op} {_summary(op, args)} {where}{gate} outcome={'ok' if code is None else code} "
              f"exit={0 if code is None else protocol.exit_code(code)} ms={ms:.0f}")

    def serve_client(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(10)
            buf = b""
            try:
                while b"\n" not in buf and len(buf) <= protocol.CLI_REQUEST_MAX:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
            except OSError:
                return
            if len(buf) > protocol.CLI_REQUEST_MAX:
                reply = protocol.reply_error(None, "too_large", "Request over 1 MiB.")
            else:
                reply = self.handle(buf.split(b"\n", 1)[0])
            try:
                conn.settimeout(None)
                conn.sendall(json.dumps(reply, separators=(",", ":")).encode() + b"\n")
            except OSError:
                pass

    def accept_loop(self) -> None:
        while not self.stopped.is_set():
            try:
                conn, _ = self.server.accept()
            except OSError:
                break
            threading.Thread(target=self.serve_client, args=(conn,), daemon=True).start()

    def stop(self, why: str) -> None:
        if self.stopped.is_set():
            return
        self.stopped.set()
        self.recordings.close_all(f"host stopped ({why})")
        audit(f"event=host_stop reason={why}")
        if self.server is not None:
            try:
                self.server.close()
            except OSError:
                pass
        if self.sock_path is not None:
            try:
                self.sock_path.unlink()
            except FileNotFoundError:
                pass

    def run(self) -> int:
        self.sock_path = socket_path()
        self.server = listen(self.sock_path)
        audit(f"event=host_start pid={os.getpid()} version={__version__}")
        self.reload_policy()
        self.push_policy()
        threads = [threading.Thread(target=self.accept_loop, daemon=True),
                   threading.Thread(target=self.watch_config, daemon=True)]
        for t in threads:
            t.start()
        self.read_loop()
        return 0


def _refuse(stdout: BinaryIO, code: str, message: str) -> None:
    try:
        stdout.write(protocol.encode_frame({"event": "error", "code": code, "message": message}))
        stdout.flush()
    except OSError:
        pass


def main(argv: list[str] | None = None, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    origin = argv[1] if len(argv) > 1 else ""
    if origin not in install.allowed_origins():
        audit(f"event=refused reason=bad_origin origin={redact_text(origin)[:120]}")
        _refuse(stdout, "bad_origin", "chromecli-host only talks to the chromecli extension.")
        return 1
    lock = take_lock()
    if lock is None:
        audit("event=refused reason=host_busy")
        _refuse(stdout, "host_busy", "Another chromecli-host is already running.")
        return 1
    try:
        return Host(stdin, stdout).run()
    except OSError as exc:
        audit(f"event=host_failed {exc}")
        _refuse(stdout, "bridge_down", f"chromecli-host couldn't start: {exc}")
        return 1
    finally:
        lock.close()


def entry() -> None:
    """The chromecli-host console script (Chrome runs it; never run it by hand)."""
    os.umask(0o077)
    raise SystemExit(main())


if __name__ == "__main__":
    entry()
