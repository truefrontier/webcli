"""chromecli tab recordings, host side (0.12.1).

The CLI picks the file (`record start --out`, default ~/.webcli/chromecli/recordings/"tab<ID> <date> at <time>.webm") and sends
it to chromecli-host with record.start; the host keeps the path, gives the request a recId, and forwards only the
recId to the extension (the extension never names a file). The extension's offscreen MediaRecorder then sends
`rec_chunk` events (base64, at most 512 KiB raw each, numbered from 0), which the host appends to that file as they
come; `rec_stopped` closes it. A chunk for an unknown recId, out of order, or too big is dropped and marks the
recording broken (never written past a hole). MP4 conversion (ffmpeg) is the CLI's job, after the file is closed.
"""

from __future__ import annotations

import base64
import json
import binascii
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from webcli.core import config
from webcli.core.redact import redact_text

CHUNK_RAW_MAX = 1 * 2**20  # the extension sends 512 KiB; anything over 1 MiB is refused
FILE_MAX = 16 * 2**30
KEEP = 50


def log_name(name: str) -> str:
    """A recording's file name for the log, quoted (the log redacts token-looking runs: 20+ letters and digits
    without a space; chromecli's default names have spaces, so they stay readable)."""
    return json.dumps(redact_text(str(name))[:120], ensure_ascii=False)


def default_dir() -> Path:
    return config.home() / "chromecli" / "recordings"


def default_path(tab: int | str, now: float | None = None) -> Path:
    # Like macOS's own "Screen Recording 2026-10-22 at 17.25.31.mov" (spaces keep the name readable in the log).
    return default_dir() / f"tab{tab} {time.strftime('%Y-%m-%d at %H.%M.%S', time.localtime(now))}.webm"


class Recordings:
    """The host's recordings this session: recId -> state, file handle, bytes written. Thread-safe."""

    def __init__(self, log: Callable[[str], None] = lambda line: None) -> None:
        self.lock = threading.Lock()
        self.recs: dict[str, dict[str, Any]] = {}
        self.log = log

    def register(self, path: Path, requested: str | None, tab: Any, fps: Any = None, audio: Any = None,
                 max_sec: Any = None) -> str:
        rec_id = uuid.uuid4().hex
        with self.lock:
            self.recs[rec_id] = {"recId": rec_id, "path": Path(path), "requested": requested, "tab": tab, "origin": None,
                                 "state": "requested", "fh": None, "bytes": 0, "nextSeq": 0, "error": None,
                                 "createdAt": time.time(), "startedAt": None, "stoppedAt": None, "durationMs": None,
                                 "reason": None, "mime": None, "fps": fps, "audio": audio, "maxSec": max_sec,
                                 "width": None, "height": None}
            self._trim()
        return rec_id

    def _trim(self) -> None:
        done = [r for r in self.recs.values() if r["state"] in ("stopped", "failed", "dropped", "cancelled", "expired")]
        for r in done[: max(0, len(done) - KEEP)]:
            self.recs.pop(r["recId"], None)

    def drop(self, rec_id: str) -> None:
        """record.start failed before any chunk: forget it (no file was created)."""
        with self.lock:
            r = self.recs.get(rec_id)
            if r and r["fh"] is None and not r["bytes"]:
                self.recs.pop(rec_id, None)

    def update(self, rec_id: str, **fields: Any) -> None:
        with self.lock:
            r = self.recs.get(rec_id)
            if r:
                for k, v in fields.items():
                    if v is not None:
                        r[k] = v

    def started(self, msg: dict) -> dict | None:
        with self.lock:
            r = self.recs.get(str(msg.get("recId")))
            if not r or r["state"] not in ("requested", "waiting"):
                return None
            r.update(state="recording", startedAt=time.time(), origin=msg.get("origin"), mime=msg.get("mime"),
                     width=msg.get("width"), height=msg.get("height"), fps=msg.get("fps", r["fps"]),
                     audio=msg.get("audio", r["audio"]), tab=msg.get("tab", r["tab"]))
            return dict(r)

    def waiting(self, msg: dict) -> None:
        with self.lock:
            r = self.recs.get(str(msg.get("recId")))
            if r and r["state"] == "requested":
                r.update(state="waiting", origin=msg.get("origin"))

    def chunk(self, msg: dict) -> str | None:
        """Append one chunk. None when written, else why it was dropped."""
        rec_id, seq, data = str(msg.get("recId")), msg.get("seq"), msg.get("data")
        with self.lock:
            r = self.recs.get(rec_id)
            if r is None:
                return "unknown recId"
            if r["state"] != "recording" or r["error"]:
                return f"recording is {r['state']}{' (broken)' if r['error'] else ''}"
            if not isinstance(seq, int) or seq != r["nextSeq"] or not isinstance(data, str):
                r["error"] = f"chunk {seq!r} out of order (expected {r['nextSeq']}); file ends at the last good chunk"
                return r["error"]
            try:
                raw = base64.b64decode(data, validate=True)
            except (binascii.Error, ValueError):
                r["error"] = f"chunk {seq} isn't base64"
                return r["error"]
            if len(raw) > CHUNK_RAW_MAX or r["bytes"] + len(raw) > FILE_MAX:
                r["error"] = f"chunk {seq} too big ({len(raw)} bytes)"
                return r["error"]
            try:
                if r["fh"] is None:
                    r["path"].parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    fd = os.open(r["path"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    r["fh"] = os.fdopen(fd, "wb")
                r["fh"].write(raw)
                r["fh"].flush()
            except OSError as exc:
                r["error"] = f"write failed: {exc.strerror or exc}"
                return r["error"]
            r["bytes"] += len(raw)
            r["nextSeq"] += 1
        return None

    def stopped(self, msg: dict) -> dict | None:
        with self.lock:
            r = self.recs.get(str(msg.get("recId")))
            if not r or r["state"] in ("stopped", "failed", "cancelled", "expired"):
                return None
            if r["fh"] is not None:
                try:
                    r["fh"].close()
                except OSError:
                    pass
                r["fh"] = None
            state = str(msg.get("state") or "stopped")
            r.update(state=state if state in ("stopped", "failed", "cancelled", "expired") else "stopped",
                     stoppedAt=time.time(), reason=msg.get("reason"), mime=msg.get("mime") or r["mime"],
                     durationMs=msg.get("durationMs"))
            if msg.get("error") and not r["error"]:
                r["error"] = str(msg["error"])[:300]
            if isinstance(msg.get("bytes"), int) and msg["bytes"] != r["bytes"] and not r["error"] and r["state"] == "stopped":
                r["error"] = f"the extension sent {msg['bytes']} bytes, {r['bytes']} were written"
            return dict(r)

    def close_all(self, why: str) -> None:
        """Chrome went away: close every open file (they stay playable up to the last chunk)."""
        with self.lock:
            for r in self.recs.values():
                if r["fh"] is not None:
                    try:
                        r["fh"].close()
                    except OSError:
                        pass
                    r["fh"] = None
                if r["state"] in ("requested", "waiting", "recording"):
                    r.update(state="stopped" if r["bytes"] else "dropped", stoppedAt=time.time(), reason=why)

    def view(self, rec_id: str) -> dict | None:
        with self.lock:
            r = self.recs.get(rec_id)
            return public(r) if r else None

    def all(self) -> list[dict]:
        with self.lock:
            return [public(r) for r in self.recs.values()]


def public(r: dict) -> dict:
    return {"recId": r["recId"], "tab": r["tab"], "origin": r["origin"], "state": r["state"], "path": str(r["path"]),
            "file": r["path"].name, "requested": r["requested"], "bytesWritten": r["bytes"], "chunks": r["nextSeq"],
            "mime": r["mime"], "fps": r["fps"], "audio": r["audio"], "maxSec": r["maxSec"],
            "width": r["width"], "height": r["height"],
            "startedAt": _iso(r["startedAt"]), "stoppedAt": _iso(r["stoppedAt"]),
            "durationMs": r["durationMs"] if r["durationMs"] is not None else (
                int((time.time() - r["startedAt"]) * 1000) if r["startedAt"] and r["state"] == "recording" else None),
            "reason": r["reason"], "error": r["error"]}


def _iso(t: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t)) if t else None
