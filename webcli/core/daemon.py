"""The identity daemon: one long-lived headless Chrome per identity, shared by every command on that identity
(generalized from cgpt's `serve`).

Commands talk to it over a unix socket, run/<identity>.sock (run/ 0700, socket 0600): one JSON line each way per
connection. Request {site, op, args, kwargs, lockWait, version}; reply {ok: true, result} or {ok: false, error: {type,
message, ...}}. The daemon runs the same <site>.fetch.<op>() a command would, one at a time (the "turn"), each in a
fresh tab of its Chrome, always headless. It serves only sites of its identity's provider, and only their listed
ops. A client that disconnects (SIGTERM, tmuxlet stop) cancels its command.

"pause" (`identity login`, --headed, WEBCLI_DAEMON=0) is a lease: the daemon closes its Chrome, releasing the
profile lock, and holds the turn until that connection closes; its Chrome comes back on the next command. The
daemon holds the profile lock whenever its Chrome is open, so a command running Chrome in-process waits for it
instead of opening a second one.

Started on demand (call() spawns `llmcli serve --identity ID`, or kickstarts the launchd agent
com.truefrontier.webcli.<identity-dir> when installed). Everything the daemon prints goes through redaction into
logs/serve-<identity-dir>.log (rotated); its raw stdout/stderr (and launchd's) go to /dev/null.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import fcntl
import importlib
import json
import os
import plistlib
import signal
import subprocess
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from webcli import __version__
from webcli.core import browser, config, output
from webcli.core.errors import (
    AuthRequired,
    Challenged,
    ConfigError,
    NotFound,
    ProfileBusy,
    RateLimited,
    UsageError,
)
from webcli.core.identities import Identity
from webcli.core.redact import redact_text

DAEMON_ENV = "WEBCLI_DAEMON"
RECYCLE_ENV = "WEBCLI_SERVE_RECYCLE_HOURS"
REQUEST_LIMIT = 64 * 2**20  # a request is a prompt and file paths
REPLY_LIMIT = 2**30  # a reply may carry images, base64
MAX_WAITING = 32  # commands queued for the turn; more are refused as busy
START_SECONDS = 20  # how long a command waits for a daemon it started to answer
HOUSEKEEPING_SECONDS = 300
SUN_PATH_MAX = 104  # macOS sockaddr_un.sun_path, NUL included: a longer $WEBCLI_HOME can't host a daemon
SENT_HINT = "The prompt may have been sent: check the chat list before sending it again."
RESTARTING = "The webcli daemon is stopping or restarting; nothing was run. Retry in a moment."

# The lock wait of the request being served (each request carries its command's --lock-wait).
request_wait: contextvars.ContextVar[float] = contextvars.ContextVar("request_wait", default=30.0)


class Restarting(RuntimeError):
    """The daemon is stopping (`serve --stop`, or a newer webcli asked it to restart). Nothing was run."""

    def __init__(self, message: str = RESTARTING) -> None:
        super().__init__(message)


def enabled() -> bool:
    return os.environ.get(DAEMON_ENV, "").strip().lower() not in ("0", "false", "no", "off")


def recycle_seconds() -> float:
    try:
        hours = float(os.environ.get(RECYCLE_ENV, "") or 6)
    except ValueError:
        hours = 6
    return max(hours, 0.1) * 3600


def socket_path(ident: Identity) -> Path:
    return ident.run_file(".sock")


def plist_path(ident: Identity) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{ident.label}.plist"


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split(".") if part.isdigit())


def log(message: str) -> None:
    """A daemon log line on stderr, which serve() routes through redaction (and a timestamp) into the rotated log."""
    print(redact_text(message), file=sys.stderr, flush=True)


def site_op(ident: Identity, site_name: str, op: str) -> Callable[..., Awaitable[Any]]:
    """<site>.fetch.<op>, if this identity's daemon may run it."""
    from webcli import sites

    site = sites.get(site_name)
    if site.provider != ident.provider:
        raise ConfigError(f"{site.name} doesn't sign in with {ident.provider}; this daemon serves {ident.id}.")
    if op not in site.ops:
        raise ConfigError(f"{site.name} has no daemon op {op!r}.")
    return getattr(importlib.import_module(site.fetch), op)


class Shared:
    """The daemon's Chrome: launched at start and on demand, relaunched after a crash or every recycle_seconds(),
    closed for a pause. `turn` serializes everything that touches it."""

    def __init__(self, ident: Identity) -> None:
        self.identity = ident
        self.turn = asyncio.Lock()
        self.stack: AsyncExitStack | None = None
        self.context = None
        self.started = 0.0
        self.since = time.time()
        self.calls = 0
        self.waiting = 0
        self.paused = False
        self.draining = False
        self.tasks: set[asyncio.Task] = set()

    async def acquire(self, wait: float) -> None:
        """Take the turn, polling (like the profile lock) for up to `wait` seconds."""
        deadline = time.monotonic() + wait
        self.waiting += 1
        try:
            while self.turn.locked():
                if self.draining:
                    raise Restarting()
                if time.monotonic() >= deadline:
                    what = "an `identity login` / --headed run" if self.paused else "another command"
                    raise ProfileBusy(f"The {self.identity.id} daemon is busy with {what} (waited {wait:g}s). "
                                      f"Retry, or raise --lock-wait / {browser.LOCK_WAIT_ENV}.")
                await asyncio.sleep(0.25)
            if self.draining:
                raise Restarting()
            await self.turn.acquire()  # free: returns without suspending
        finally:
            self.waiting -= 1

    def alive(self) -> bool:
        return self.context is not None and time.monotonic() - self.started < recycle_seconds()

    async def ensure(self, wait: float) -> None:
        """Hold the turn. Launch Chrome (headless, always) unless it is running and younger than recycle_seconds()."""
        if self.alive():
            return
        await self.close()
        stack = AsyncExitStack()
        try:
            context = await stack.enter_async_context(browser.chrome(self.identity, mode="headless", wait=wait))
        except BaseException:
            await stack.aclose()
            raise
        self.stack, self.context, self.started = stack, context, time.monotonic()
        context.on("close", lambda _context: self._lost(context))
        log("chrome: launched (headless)")

    def _lost(self, context) -> None:
        if self.context is context:  # Chrome went away without close(): relaunch on the next command
            self.context = None
            log("chrome: closed unexpectedly; relaunching on the next command")

    async def close(self) -> None:
        stack, self.stack, self.context = self.stack, None, None
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()
            log("chrome: closed")

    @asynccontextmanager
    async def page(self, policy, **page_options) -> AsyncIterator[Any]:
        """browser.site_page's page: the turn, then a fresh tab (closed afterwards)."""
        await self.acquire(request_wait.get())
        try:
            await self.ensure(request_wait.get())
            async with browser.page_at(self.context, policy, mode="headless", new=True, **page_options) as page:
                yield page
        finally:
            self.calls += 1
            self.turn.release()

    async def pause(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Close Chrome and keep the turn until the client's connection closes."""
        await self.acquire(request_wait.get())
        try:
            await self.close()
            self.paused = True
            log("paused for login / --headed")
            await reply(writer, {"ok": True, "result": {"paused": True}})
            await reader.read(1)  # EOF: the client is done with the profile
        finally:
            self.paused = False
            self.turn.release()
            log("resumed")

    async def housekeeping(self) -> None:
        """Every HOUSEKEEPING_SECONDS: an idle Chrome past recycle_seconds() is relaunched."""
        while True:
            await asyncio.sleep(HOUSEKEEPING_SECONDS)
            if self.context is None or self.alive() or self.turn.locked() or self.draining:
                continue
            await self.turn.acquire()
            try:
                log("chrome: recycling")
                await self.ensure(browser.lock_wait)
            except Exception as exc:
                log(f"chrome: recycle failed: {exc}")
            finally:
                self.turn.release()

    async def warm(self) -> None:
        if not self.identity.profile_exists():
            return
        try:
            await self.acquire(browser.lock_wait)
        except Exception:
            return
        try:
            await self.ensure(browser.lock_wait)
        except Exception as exc:
            log(f"chrome: launch failed: {exc}")
        finally:
            self.turn.release()

    def info(self) -> dict:
        return {"running": True, "identity": self.identity.id, "pid": os.getpid(), "version": __version__,
                "socket": str(socket_path(self.identity)), "uptimeSeconds": round(time.time() - self.since),
                "chrome": self.context is not None, "chromeMode": "headless",
                "chromeAgeSeconds": round(time.monotonic() - self.started) if self.context is not None else None,
                "recycleHours": round(recycle_seconds() / 3600, 2), "calls": self.calls, "busy": self.turn.locked(),
                "waiting": self.waiting, "paused": self.paused, "stopping": self.draining}


async def reply(writer: asyncio.StreamWriter, payload: dict) -> None:
    writer.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
    await writer.drain()


def encode_result(result: Any) -> Any:
    """imageData [{data: bytes, ...}] travels as base64."""
    if isinstance(result, dict) and result.get("imageData"):
        result = {**result, "imageData": [{**image, "data": base64.b64encode(image["data"]).decode()}
                                          for image in result["imageData"]]}
    return result


def decode_result(result: Any) -> Any:
    if isinstance(result, dict) and result.get("imageData"):
        result["imageData"] = [{**image, "data": base64.b64decode(image["data"])} for image in result["imageData"]]
    return result


def error_payload(exc: BaseException) -> dict:
    """An exception as {type, message, ...}: only types the client re-raises, everything else as RuntimeError."""
    message = redact_text(str(exc)) or type(exc).__name__
    if isinstance(exc, Challenged):
        return {"type": "Challenged", "mode": exc.mode, "host": exc.host, "message": message}
    if isinstance(exc, RateLimited):
        return {"type": "RateLimited", "message": redact_text(exc.message), "sent": exc.sent,
                "resets_at": exc.resets_at}
    if isinstance(exc, (NotFound, ConfigError, UsageError)):
        return {"type": type(exc).__name__, "message": message, "hint": exc.hint}
    for kind in (AuthRequired, ProfileBusy, Restarting, ValueError):
        if isinstance(exc, kind):
            return {"type": kind.__name__, "message": message}
    return {"type": "RuntimeError", "message": message}


def rebuild(error: dict) -> BaseException:
    kind, message = error.get("type"), str(error.get("message") or "The webcli daemon failed.")
    if kind == "Challenged":
        return Challenged(str(error.get("mode") or "headless"), str(error.get("host") or "the site"))
    if kind == "RateLimited":
        return RateLimited(message, sent=bool(error.get("sent")), resets_at=error.get("resets_at"))
    if kind == "NotFound":
        return NotFound(message, hint=error.get("hint"))
    if kind == "ConfigError":
        return ConfigError(message, hint=error.get("hint"))
    if kind == "UsageError":
        return UsageError(message, hint=error.get("hint"))
    known = {"AuthRequired": AuthRequired, "ProfileBusy": ProfileBusy, "Restarting": Restarting,
             "ValueError": ValueError}
    return known.get(kind, RuntimeError)(message)


async def handle(shared: Shared, stop: Callable[[], None], reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter) -> None:
    started, op, outcome = time.monotonic(), "?", "ok"
    try:
        try:
            request = json.loads(await reader.readline())
            op = request["op"]
            request_wait.set(browser.parse_wait(request.get("lockWait", browser.lock_wait)))
        except (ValueError, KeyError, TypeError, asyncio.LimitOverrunError):
            outcome = "bad request"
            await reply(writer, {"ok": False, "error": {"type": "RuntimeError", "message": "bad request"}})
            return
        if op == "ping":
            await reply(writer, {"ok": True, "result": shared.info()})
            return
        if op == "stop":
            await reply(writer, {"ok": True, "result": {**shared.info(), "stopping": True}})
            stop()
            return
        client = str(request.get("version") or "")
        if shared.draining or _version(client) > _version(__version__):
            if not shared.draining:
                log(f"webcli {client} is newer than this daemon ({__version__}): restarting")
                stop()
            outcome = "restarting"
            await reply(writer, {"ok": False, "error": {"type": "Restarting", "message": RESTARTING}})
            return
        if op == "pause":
            await shared.pause(reader, writer)
            return
        args, kwargs = request.get("args", []), request.get("kwargs", {})
        try:
            if not isinstance(args, list) or not isinstance(kwargs, dict):
                raise ConfigError("bad request")
            fn = site_op(shared.identity, str(request.get("site")), op)
        except ConfigError as exc:
            outcome = "bad request"
            await reply(writer, {"ok": False, "error": error_payload(exc)})
            return
        op = f"{request.get('site')}.{op}"
        if shared.waiting >= MAX_WAITING:
            outcome = "busy"
            await reply(writer, {"ok": False, "error": {
                "type": "ProfileBusy", "message": f"The webcli daemon has {shared.waiting} commands queued. Retry."}})
            return
        kwargs["headed"] = False  # the daemon's Chrome is headless, whatever a client asks
        task = asyncio.ensure_future(fn(*args, **kwargs))
        shared.tasks.add(task)
        gone = asyncio.ensure_future(reader.read(1))  # the client sends nothing more: this ends when it leaves
        try:
            await asyncio.wait({task, gone}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            shared.tasks.discard(task)
        if not task.done():
            outcome = "cancelled: client left"
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            return
        gone.cancel()
        if task.cancelled():  # the daemon is stopping on a signal
            outcome = "cancelled: daemon stopping"
            message = "The webcli daemon stopped in the middle of the command." + (
                f" {SENT_HINT}" if op.endswith(".ask") else "")
            await reply(writer, {"ok": False, "error": {"type": "RuntimeError", "message": message}})
        elif task.exception() is not None:
            outcome = type(task.exception()).__name__
            await reply(writer, {"ok": False, "error": error_payload(task.exception())})
        else:
            await reply(writer, {"ok": True, "result": encode_result(task.result())})
    except (ConnectionError, BrokenPipeError):
        outcome = "client left"
    except Exception as exc:
        outcome = f"error: {type(exc).__name__}"
        with contextlib.suppress(Exception):
            await reply(writer, {"ok": False, "error": error_payload(exc)})
    finally:
        with contextlib.suppress(Exception):
            writer.close()
        if op != "ping":
            log(f"{op} {outcome} {time.monotonic() - started:.1f}s")


async def listen(shared: Shared, path: Path, stop: Callable[[], None]) -> asyncio.AbstractServer:
    """The unix socket, created 0600 (umask) in its 0700 directory."""
    path.unlink(missing_ok=True)
    old = os.umask(0o177)
    try:
        server = await asyncio.start_unix_server(lambda r, w: handle(shared, stop, r, w), path=str(path),
                                                 limit=REQUEST_LIMIT)
    finally:
        os.umask(old)
    path.chmod(0o600)
    return server


async def _serve(ident: Identity) -> None:
    shared = Shared(ident)
    browser.shared = shared
    browser.identity = ident
    stopping = asyncio.Event()
    hard = []

    def stop() -> None:
        shared.draining = True
        stopping.set()

    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        loop.add_signal_handler(signum, lambda s=signum: (hard.append(s), stop()))
    sock = socket_path(ident)
    server = await listen(shared, sock, stop)
    log(f"webcli serve {__version__} for {ident.id} listening on {sock} (pid {os.getpid()})")
    background = [asyncio.ensure_future(shared.warm()), asyncio.ensure_future(shared.housekeeping())]
    try:
        await stopping.wait()
        server.close()
        if hard:  # a signal (launchctl bootout, kill): don't wait for the running command
            for task in list(shared.tasks):
                task.cancel()
        # --stop or a restart: let the running command finish (it is bounded by its own --timeout)
        while shared.turn.locked() and not hard:
            await asyncio.sleep(0.25)
    finally:
        for task in background:
            task.cancel()
        await shared.close()
        sock.unlink(missing_ok=True)
        log("stopped")


def _redirect_output(ident: Identity) -> None:
    """Everything this process prints (log lines, tracebacks, library warnings) goes through redaction into
    logs/serve-<identity-dir>.log, and to the original stderr when that is a terminal (`serve` in the foreground)."""
    file_log = output.logger(f"serve-{ident.dir_name}")
    original = sys.__stderr__
    echo = bool(original and original.isatty())

    def write_line(line: str) -> None:
        file_log.info(line)
        if echo:
            original.write(f"{output.stamp()} {line}\n")
            original.flush()

    sys.stdout = sys.stderr = output.RedactingStream(write_line)


def serve(ident: Identity) -> int:
    """Run the daemon in the foreground until stopped. Exit 0 also when another daemon already runs."""
    os.umask(0o077)
    config.private_dir(ident.path)
    config.run_dir()
    _redirect_output(ident)
    with open(ident.run_file(".serve.lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log(f"another webcli serve for {ident.id} is running")
            return 0
        # serve.lock is held from before the stale socket is removed until after it is unlinked again.
        asyncio.run(_serve(ident))
    return 0


# Client side.

async def _open(ident: Identity) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
    try:
        return await asyncio.open_unix_connection(str(socket_path(ident)), limit=REPLY_LIMIT)
    except OSError:
        return None


def launchd_loaded(ident: Identity) -> bool:
    try:
        return subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{ident.label}"],
                              capture_output=True).returncode == 0
    except OSError:
        return False


def serve_command(ident: Identity) -> list[str]:
    """How a daemon is started: this interpreter running llmcli's `serve`."""
    return [sys.executable, "-m", "webcli.apps.llmcli", "serve", "--identity", ident.id]


def start(ident: Identity) -> None:
    """Start a daemon in the background: kickstart the launchd agent when it is loaded, else spawn `serve`
    detached (a second one exits at once on serve.lock). Its raw output goes nowhere: it logs through redaction."""
    if len(os.fsencode(socket_path(ident))) >= SUN_PATH_MAX:
        raise OSError(f"{socket_path(ident)} is too long for a unix socket; commands run in-process")
    if plist_path(ident).exists() and launchd_loaded(ident):
        if subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{ident.label}"],
                          capture_output=True).returncode == 0:
            return
    subprocess.Popen(serve_command(ident), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True, cwd=str(config.ensure_home()))


async def connect(ident: Identity, *, start_daemon: bool) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
    conn = await _open(ident)
    if conn is not None or not start_daemon:
        return conn
    try:
        start(ident)
    except OSError:
        return None
    deadline = time.monotonic() + START_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(0.2)
        if (conn := await _open(ident)) is not None:
            return conn
    return None


async def _request(conn, payload: dict) -> dict:
    reader, writer = conn
    try:
        writer.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
        await writer.drain()
        line = await reader.readline()
    finally:
        writer.close()
    if not line:
        raise ConnectionError("The webcli daemon closed the connection.")
    return json.loads(line)


async def wait_gone(ident: Identity, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        conn = await _open(ident)
        if conn is None:
            return True
        conn[1].close()
        await asyncio.sleep(0.25)
    return False


async def call(ident: Identity, site: str, op: str, *args, start_daemon: bool = True, **kwargs) -> Any:
    """<site>.fetch.<op>(*args, **kwargs) in the identity's daemon (started on demand unless start_daemon=False),
    else in this process. Never sends an ask twice: a dropped connection is an error, and only a "restarting"
    answer (nothing was run) is retried."""
    def local() -> Awaitable[Any]:
        return site_op(ident, site, op)(*args, **kwargs)

    if not enabled():
        return await paused(ident, local)
    payload = {"site": site, "op": op, "args": list(args), "kwargs": kwargs, "lockWait": browser.lock_wait,
               "version": __version__}
    for attempt in range(2):
        conn = await connect(ident, start_daemon=start_daemon)
        if conn is None:  # no daemon could start: this command runs its own (hidden) Chrome
            return await local()
        try:
            answer = await _request(conn, payload)
        except (ConnectionError, ValueError):
            raise RuntimeError("The webcli daemon stopped in the middle of the command."
                               + (f" {SENT_HINT}" if op == "ask" else " Retry.")) from None
        if answer.get("ok"):
            return decode_result(answer["result"])
        error = rebuild(answer.get("error") or {})
        if not isinstance(error, Restarting) or attempt:
            raise error
        await wait_gone(ident, max(browser.lock_wait, 5))
    raise AssertionError("unreachable")


async def paused(ident: Identity, run: Callable[[], Awaitable[Any]]) -> Any:
    """run() with a running daemon's Chrome closed (`identity login`, --headed, WEBCLI_DAEMON=0): the pause lease
    ends with this connection."""
    conn = await connect(ident, start_daemon=False)
    if conn is None:
        return await run()
    reader, writer = conn
    try:
        writer.write(json.dumps({"op": "pause", "lockWait": browser.lock_wait, "version": __version__}).encode()
                     + b"\n")
        await writer.drain()
        line = await reader.readline()
        answer = json.loads(line) if line else {"ok": False, "error": {"message": "The webcli daemon closed."}}
        if not answer.get("ok"):
            error = rebuild(answer.get("error") or {})
            if not isinstance(error, Restarting):
                raise error
        return await run()
    finally:
        writer.close()


async def daemon_info(ident: Identity) -> dict:
    conn = await _open(ident)
    if conn is None:
        return {"running": False, "identity": ident.id, "socket": str(socket_path(ident))}
    try:
        return (await _request(conn, {"op": "ping"})).get("result") or {"running": False, "identity": ident.id}
    except (ConnectionError, ValueError):
        return {"running": False, "identity": ident.id, "socket": str(socket_path(ident))}


async def stop_daemon(ident: Identity, seconds: float = 60) -> dict:
    conn = await _open(ident)
    if conn is None:
        return {"running": False, "stopped": True, "identity": ident.id}
    try:
        info = (await _request(conn, {"op": "stop"})).get("result") or {}
    except (ConnectionError, ValueError):
        info = {}
    stopped = await wait_gone(ident, seconds)
    return {"running": not stopped, "stopped": stopped, "identity": ident.id, "pid": info.get("pid"),
            "wasBusy": info.get("busy")}


def status(ident: Identity) -> dict:
    """`serve --status`: the daemon, plus its launchd agent."""
    info = browser.run(daemon_info(ident))
    info["launchd"] = {"label": ident.label, "installed": plist_path(ident).exists(), "loaded": launchd_loaded(ident)}
    return info


# launchd.

def program(ident: Identity) -> list[str]:
    """How launchd runs the daemon: the llmcli on PATH (pipx's stable ~/.local/bin/llmcli), else this interpreter."""
    import shutil

    found = shutil.which("llmcli")
    return [found, "serve", "--identity", ident.id] if found else serve_command(ident)


def plist(ident: Identity, prog: list[str]) -> dict:
    env = {"PATH": f"{Path.home() / '.local/bin'}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}
    if os.environ.get(config.HOME_ENV):
        env[config.HOME_ENV] = str(config.home())
    return {
        "Label": ident.label,
        "ProgramArguments": prog,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},  # restart after a crash; `serve --stop` (exit 0) stays stopped
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "WorkingDirectory": str(config.home()),
        # The daemon logs through redaction itself; raw fd-level output (Chrome's, Playwright's driver) goes nowhere.
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
        "EnvironmentVariables": env,
    }


def install_launchd(ident: Identity) -> dict:
    config.private_dir(ident.path)
    path = plist_path(ident)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(plist(ident, program(ident))))
    browser.run(stop_daemon(ident))  # a daemon started on demand hands over to launchd's
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{ident.label}"], capture_output=True)
    done = subprocess.run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {done.stderr.strip() or done.returncode}")
    info = browser.run(_wait_up(ident))
    return {"installed": True, "plist": str(path), "loaded": launchd_loaded(ident), "daemon": info}


async def _wait_up(ident: Identity) -> dict:
    deadline = time.monotonic() + START_SECONDS
    while time.monotonic() < deadline:
        info = await daemon_info(ident)
        if info.get("running"):
            return info
        await asyncio.sleep(0.25)
    return {"running": False, "identity": ident.id}


def uninstall_launchd(ident: Identity) -> dict:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{ident.label}"], capture_output=True)
    path = plist_path(ident)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return {"installed": False, "removed": existed, "plist": str(path)}
