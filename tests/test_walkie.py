"""webcli walkie offline: every verb against a temp history.db with Walkie's schema (from walkie-cli's fixture), the
database discovery, the exit codes for a missing or unreadable database, and read-only. No network."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
from datetime import datetime

import pytest
from click.testing import CliRunner

from webcli.apps import webcli as webcli_app
from webcli.core.errors import ConfigError
from webcli.sites.walkie import db as walkie_db

SCHEMA = """
CREATE TABLE meetings (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'unknown',
    template_id TEXT NOT NULL DEFAULT 'general', started_at INTEGER NOT NULL, duration_ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'recording', audio_path TEXT, sections TEXT NOT NULL DEFAULT '[]',
    action_items TEXT NOT NULL DEFAULT '[]', user_notes TEXT NOT NULL DEFAULT '',
    has_system_audio INTEGER NOT NULL DEFAULT 0, folder TEXT, participants TEXT NOT NULL DEFAULT '[]',
    attendees TEXT NOT NULL DEFAULT '[]');
CREATE TABLE meeting_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, meeting_id TEXT NOT NULL, speaker TEXT NOT NULL, start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL, text TEXT NOT NULL);
CREATE TABLE transcription_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, file_name TEXT NOT NULL, timestamp INTEGER NOT NULL, title TEXT NOT NULL,
    transcription_text TEXT NOT NULL, app_bundle_id TEXT, app_name TEXT, kind TEXT NOT NULL DEFAULT 'dictation');
"""


def make_db(path) -> object:
    """Two meetings (Team Standup, 10 min, 4 segments; Design Review a day later, 15 min, 2), two dictations, and
    one non-dictation transcription."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO meetings (id, title, source, started_at, duration_ms, status, sections, action_items, "
                 "participants, attendees) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 ("mtg-001", "Team Standup", "zoom", 1700000000000, 600000, "ready",
                  json.dumps([{"title": "Updates", "content": "Team updates"}]),
                  json.dumps([{"text": "Review PR #123", "assignee": "Alice"}]), json.dumps(["you", "them:1"]),
                  json.dumps(["Alice", "Bob"])))
    conn.execute("INSERT INTO meetings (id, title, source, started_at, duration_ms, status, folder) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                 ("mtg-002", "Design Review", "google-meet", 1700100000000, 900000, "ready", "Design"))
    conn.executemany("INSERT INTO meeting_segments (meeting_id, speaker, start_ms, end_ms, text) "
                     "VALUES (?, ?, ?, ?, ?)",
                     [("mtg-001", "them:1", 5500, 12000, "Morning! Let's start with updates."),
                      ("mtg-001", "you", 0, 5000, "Good morning everyone!"),
                      ("mtg-001", "you", 65000, 70000, "I finished the authentication module."),
                      ("mtg-001", "them:1", 3700000, 3710000, "Great work!"),
                      ("mtg-002", "you", 0, 8000, "Let's review the new dashboard design."),
                      ("mtg-002", "them:1", 8500, 15000, "The color scheme looks good.")])
    conn.executemany("INSERT INTO transcription_history (file_name, timestamp, title, transcription_text, kind, "
                     "app_name, app_bundle_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     [("w-1", 1700000000, "Todo for today", "Finish the CLI", "dictation", "VS Code", None),
                      ("w-2", 1700010000, "Quick note", "Update docs", "dictation", None, "com.tinyspeck.slack"),
                      ("w-3", 1700020000, "Agent", "Ask the agent", "agent", "Slack", None)])
    conn.commit()
    conn.close()
    return path


@pytest.fixture(autouse=True)
def no_walkie_here(monkeypatch, tmp_path):
    """Walkie's real data is never read: its root is an empty temp dir, and $WALKIE_DB is unset."""
    monkeypatch.setattr(walkie_db, "DEFAULT_ROOT", tmp_path / "com.pais.walkie")
    monkeypatch.delenv(walkie_db.DB_ENV, raising=False)


@pytest.fixture
def db(tmp_path):
    return make_db(tmp_path / "history.db")


def run(*args):
    return CliRunner().invoke(webcli_app.build(), ["walkie", *args])


def data(*args):
    result = run(*args)
    assert result.exit_code == 0, result.stderr
    return json.loads(result.stdout)


def local_time(seconds: int) -> str:
    return datetime.fromtimestamp(seconds).isoformat()


def error(result, code: int) -> dict:
    assert result.exit_code == code, result.stdout + result.stderr
    payload = json.loads(result.stderr)
    assert payload["code"] == code and payload["hint"]
    return payload


def test_help_and_agent_help_list_every_verb():
    result = run("--help")
    assert result.exit_code == 0
    for verb in ("meetings", "dictations", "status", "agent-help"):
        assert verb in result.stdout
    ref = json.loads(run("agent-help", "--format", "json").stdout)
    commands = {c["name"]: c for c in ref["commands"]}
    assert set(commands) == {"status", "meetings list", "meetings show", "meetings transcript", "dictations list",
                             "agent-help"}
    assert (commands["meetings show"]["aliases"], commands["meetings transcript"]["aliases"]) == (
        ["get"], ["history", "read"])
    assert {"--db", "--account", "--format"} <= {f for o in commands["meetings list"]["options"] for f in o["flags"]}


def test_status(db):
    assert data("status", "--db", str(db)) == {"db_path": str(db), "account_id": "unknown", "meeting_count": 2,
                                               "segment_count": 6, "dictation_count": 2}


def test_meetings_list_newest_first(db):
    assert data("meetings", "list", "--db", str(db)) == [
        {"id": "mtg-002", "title": "Design Review", "source": "google-meet", "started": local_time(1700100000),
         "duration": "15.0m", "status": "ready", "folder": "Design", "segment_count": 2},
        {"id": "mtg-001", "title": "Team Standup", "source": "zoom", "started": local_time(1700000000),
         "duration": "10.0m", "status": "ready", "folder": "", "segment_count": 4}]
    assert [m["id"] for m in data("meetings", "list", "--db", str(db), "--limit", "1")] == ["mtg-002"]
    assert data("meetings", "list", "--db", str(db), "--compact")[0] == {
        "id": "mtg-002", "title": "Design Review", "started": local_time(1700100000)}


def test_meetings_show_by_id_or_title(db):
    meeting = data("meetings", "show", "mtg-001", "--db", str(db))
    assert (meeting["title"], meeting["duration_ms"], meeting["segment_count"]) == ("Team Standup", 600000, 4)
    assert meeting["action_items"] == [{"text": "Review PR #123", "assignee": "Alice"}]
    assert meeting["attendees"] == ["Alice", "Bob"] and meeting["has_system_audio"] is False
    assert data("meetings", "get", "standup", "--db", str(db))["id"] == "mtg-001"  # alias, title substring
    assert data("meetings", "show", "review", "--db", str(db))["sections"] == []
    error(run("meetings", "show", "nope", "--db", str(db)), 3)


def test_meetings_transcript_in_time_order(db):
    assert data("meetings", "transcript", "standup", "--db", str(db)) == [
        {"time": "0s", "speaker": "you", "text": "Good morning everyone!"},
        {"time": "6s", "speaker": "them:1", "text": "Morning! Let's start with updates."},
        {"time": "1.1m", "speaker": "you", "text": "I finished the authentication module."},
        {"time": "1.0h", "speaker": "them:1", "text": "Great work!"}]
    assert len(data("meetings", "read", "mtg-001", "--db", str(db), "--limit", "2")) == 2
    assert data("meetings", "history", "mtg-002", "--db", str(db))[0]["text"].startswith("Let's review")
    error(run("meetings", "transcript", "nope", "--db", str(db)), 3)


def test_dictations_only_dictations_newest_first(db):
    assert data("dictations", "list", "--db", str(db)) == [
        {"id": 2, "title": "Quick note", "timestamp": local_time(1700010000), "app": "com.tinyspeck.slack",
         "text": "Update docs"},
        {"id": 1, "title": "Todo for today", "timestamp": local_time(1700000000), "app": "VS Code",
         "text": "Finish the CLI"}]


def test_csv_and_md(db):
    assert run("dictations", "list", "--db", str(db), "--csv", "--select", "id,app").stdout == (
        "id,app\n2,com.tinyspeck.slack\n1,VS Code\n")
    assert run("status", "--db", str(db), "--format", "md").stdout.splitlines()[:3] == [
        "| field | value |", "|---|---|", f"| db_path | {db} |"]


def test_discovery_picks_the_newest_account_or_the_named_one(tmp_path):
    accounts = walkie_db.DEFAULT_ROOT / "accounts"
    make_db(accounts / "old" / "history.db")
    make_db(accounts / "new" / "history.db")
    os.utime(accounts / "old", (1, 1))
    assert data("status")["account_id"] == "new"
    assert data("status", "--account", "old")["account_id"] == "old"
    assert "new, old" in error(run("status", "--account", "nobody"), 3)["hint"]


def test_db_env_is_used_when_there_is_no_db_flag(db, monkeypatch, tmp_path):
    monkeypatch.setenv(walkie_db.DB_ENV, str(db))
    assert data("status")["db_path"] == str(db)
    monkeypatch.setenv(walkie_db.DB_ENV, str(tmp_path / "gone.db"))
    assert "$WALKIE_DB" in error(run("status"), 7)["error"]


def test_no_walkie_data_exits_3_with_a_hint():
    for args in (("status",), ("meetings", "list"), ("dictations", "list")):
        assert "--db" in error(run(*args), 3)["hint"]
    (walkie_db.DEFAULT_ROOT / "accounts" / "empty").mkdir(parents=True)
    assert "no history.db" in error(run("status"), 3)["error"]


def test_a_missing_or_unreadable_db_exits_7(tmp_path):
    assert "--db" in error(run("status", "--db", str(tmp_path / "missing.db")), 7)["error"]
    assert not (tmp_path / "missing.db").exists()
    junk = tmp_path / "junk.db"
    junk.write_text("not sqlite")
    assert "not a database" in error(run("meetings", "list", "--db", str(junk)), 7)["error"]
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    error(run("dictations", "list", "--db", str(empty)), 7)  # no such table
    error(run("status", "--db", str(tmp_path)), 7)  # a directory


def test_a_changed_schema_exits_5(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE meetings (id TEXT, title TEXT, started_at INTEGER);"
                       "CREATE TABLE meeting_segments (id INTEGER, meeting_id TEXT);")
    conn.close()
    assert run("meetings", "list", "--db", str(path)).exit_code == 5


def test_read_only_and_no_network(db, monkeypatch):
    before = db.read_bytes()

    def no_network(*_args, **_kwargs):
        raise AssertionError("walkie opened a network socket")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    for args in (("status",), ("meetings", "list"), ("meetings", "show", "mtg-001"),
                 ("meetings", "transcript", "mtg-001"), ("dictations", "list")):
        assert run(*args, "--db", str(db)).exit_code == 0
    assert db.read_bytes() == before
    with pytest.raises(ConfigError, match="readonly"):
        with walkie_db.connect(db) as conn:
            conn.execute("DELETE FROM meetings")
    assert db.read_bytes() == before


def test_a_path_with_uri_characters_opens(tmp_path):
    path = make_db(tmp_path / "odd ?#% name" / "history.db")
    assert data("status", "--db", str(path))["meeting_count"] == 2
