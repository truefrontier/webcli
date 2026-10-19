"""Read-only access to Walkie.app's local SQLite history (walkie-cli parity): meetings, their transcript segments,
and dictations. No network.

The database: --db PATH, else $WALKIE_DB, else ~/Library/Application Support/com.pais.walkie/accounts/<account>/
history.db (--account picks one; otherwise the only one, or the most recently modified). It is opened with
mode=ro, so nothing here can write to it.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from webcli.core.errors import ConfigError, NotFound

DEFAULT_ROOT = Path.home() / "Library" / "Application Support" / "com.pais.walkie"
DB_ENV = "WALKIE_DB"
DB_HINT = f"Pass --db PATH to a Walkie history.db, or set ${DB_ENV}."


class SchemaChanged(RuntimeError):
    """Walkie's tables lack a column this reads: Walkie changed its schema (exit 5)."""


def locate(db: Path | None = None, account: str | None = None) -> Path:
    """The history.db to read (see the module doc). A named file that isn't there is a ConfigError (exit 7); no
    Walkie data where Walkie keeps it, or no such account, is NotFound (exit 3)."""
    env = os.environ.get(DB_ENV, "").strip()
    if db or env:
        path, source = (db, "--db") if db else (Path(env), f"${DB_ENV}")
        path = path.expanduser()
        if not path.is_file():
            raise ConfigError(f"{source} names no database file: {path}", hint=DB_HINT)
        return path
    root = DEFAULT_ROOT
    accounts = root / "accounts"
    dirs = sorted(d for d in accounts.iterdir() if d.is_dir()) if accounts.is_dir() else []
    if not dirs:
        raise NotFound(f"No Walkie.app data on this Mac (no accounts in {root}).",
                       hint=f"Install Walkie.app and record something first. {DB_HINT}")
    if account:
        match = [d for d in dirs if d.name == account]
        if not match:
            raise NotFound(f"No Walkie account {account!r}.", hint=f"Accounts: {', '.join(d.name for d in dirs)}.")
        target = match[0]
    else:
        target = max(dirs, key=lambda d: d.stat().st_mtime)
    path = target / "history.db"
    if not path.is_file():
        raise NotFound(f"Walkie account {target.name} has no history.db yet.", hint=DB_HINT)
    return path


def account_id(path: Path) -> str:
    """The <account> in .../accounts/<account>/history.db, else "unknown"."""
    parts = path.parts
    return parts[parts.index("accounts") + 1] if "accounts" in parts[:-1] else "unknown"


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    """A read-only connection (mode=ro). A file that isn't a readable SQLite database is a ConfigError (exit 7)."""
    hint = f"Is it Walkie's history.db? {DB_HINT}"
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ConfigError(f"Can't open {path}: {exc}", hint=hint) from None
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    except sqlite3.OperationalError as exc:
        if str(exc).startswith("no such column"):
            raise SchemaChanged(f"Walkie's database changed shape ({exc}).") from None
        raise ConfigError(f"Can't read {path}: {exc}", hint=hint) from None
    except sqlite3.DatabaseError as exc:
        raise ConfigError(f"Can't read {path}: {exc}", hint=hint) from None
    finally:
        conn.close()


def _when(ms_epoch: int) -> str:
    return datetime.fromtimestamp(ms_epoch / 1000).isoformat()


def _duration(ms: int) -> str:
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def _json(text: str | None) -> Any:
    try:
        return json.loads(text) if text else []
    except ValueError:
        return []


def status(conn: sqlite3.Connection, path: Path) -> dict:
    def count(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    return {"db_path": str(path), "account_id": account_id(path),
            "meeting_count": count("SELECT COUNT(*) FROM meetings"),
            "segment_count": count("SELECT COUNT(*) FROM meeting_segments"),
            "dictation_count": count("SELECT COUNT(*) FROM transcription_history WHERE kind = 'dictation'")}


def meetings(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    """Newest first."""
    rows = conn.execute("""
        SELECT m.id, m.title, m.source, m.started_at, m.duration_ms, m.status, m.folder, COUNT(s.id) AS segments
        FROM meetings m LEFT JOIN meeting_segments s ON m.id = s.meeting_id
        GROUP BY m.id ORDER BY m.started_at DESC LIMIT ?""", (limit or -1,))
    return [{"id": r["id"], "title": r["title"], "source": r["source"], "started": _when(r["started_at"]),
             "duration": _duration(r["duration_ms"]), "status": r["status"], "folder": r["folder"] or "",
             "segment_count": r["segments"]} for r in rows]


def meeting(conn: sqlite3.Connection, id_or_title: str) -> dict | None:
    """By exact id, else the newest meeting whose title contains it (case-insensitive)."""
    row = (conn.execute("SELECT * FROM meetings WHERE id = ?", (id_or_title,)).fetchone()
           or conn.execute("SELECT * FROM meetings WHERE title LIKE ? ORDER BY started_at DESC LIMIT 1",
                           (f"%{id_or_title}%",)).fetchone())
    if row is None:
        return None
    segments = conn.execute("SELECT COUNT(*) FROM meeting_segments WHERE meeting_id = ?", (row["id"],)).fetchone()[0]
    return {"id": row["id"], "title": row["title"], "source": row["source"], "template_id": row["template_id"],
            "started": _when(row["started_at"]), "started_at": row["started_at"],
            "duration": _duration(row["duration_ms"]), "duration_ms": row["duration_ms"], "status": row["status"],
            "audio_path": row["audio_path"] or "", "folder": row["folder"] or "", "user_notes": row["user_notes"] or "",
            "sections": _json(row["sections"]), "action_items": _json(row["action_items"]),
            "participants": _json(row["participants"]), "attendees": _json(row["attendees"]),
            "segment_count": segments, "has_system_audio": bool(row["has_system_audio"])}


def transcript(conn: sqlite3.Connection, meeting_id: str, limit: int | None = None) -> list[dict]:
    """The meeting's segments in time order."""
    rows = conn.execute("SELECT speaker, start_ms, text FROM meeting_segments WHERE meeting_id = ? "
                        "ORDER BY start_ms ASC LIMIT ?", (meeting_id, limit or -1))
    return [{"time": _duration(r["start_ms"]), "speaker": r["speaker"], "text": r["text"]} for r in rows]


def dictations(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    """Newest first. transcription_history.timestamp is in seconds."""
    rows = conn.execute("SELECT id, title, timestamp, transcription_text, app_name, app_bundle_id "
                        "FROM transcription_history WHERE kind = 'dictation' ORDER BY timestamp DESC LIMIT ?",
                        (limit or -1,))
    return [{"id": r["id"], "title": r["title"], "timestamp": _when(r["timestamp"] * 1000),
             "app": r["app_name"] or r["app_bundle_id"] or "", "text": r["transcription_text"]} for r in rows]
