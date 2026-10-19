"""The local cache: one SQLite file of people, their About data, and their enrichment (fbc's store).

Lives in ~/.webcli/sites/facebook/ (fbc kept it in ~/Projects/TRUE-fb-contacts/), never in the repo. The directory
is 0700 and every file written there is 0600: it is personal data about the account's friends. Nothing in it comes
from cookies or tokens; URLs are canonical before they get here.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from webcli.core import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    key TEXT PRIMARY KEY,           -- username when known, else numeric id
    name TEXT,
    profile_url TEXT,
    username TEXT,
    numeric_id TEXT,
    subtitle TEXT,                  -- the work/city line under the name on the Friends tab
    is_friend INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT,
    about TEXT,                     -- JSON from parse.about
    about_fetched_at TEXT,
    enrich TEXT,                    -- JSON from enrich.build
    enriched_at TEXT
);
CREATE INDEX IF NOT EXISTS people_username ON people(username);
CREATE INDEX IF NOT EXISTS people_numeric_id ON people(numeric_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def data_dir() -> Path:
    return config.site_dir("facebook")


def exports_dir() -> Path:
    return config.private_dir(data_dir() / "exports")


def private_file(path: Path) -> Path:
    """Create (or tighten) a file to 0600 before anything is written to it."""
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    return path


def connect() -> sqlite3.Connection:
    db = private_file(data_dir() / "friends.sqlite")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for col in ("about", "enrich"):
        out[col] = json.loads(out[col]) if out[col] else None
    out["is_friend"] = bool(out["is_friend"])
    return out


def _find(conn: sqlite3.Connection, key: str | None, username: str | None, numeric_id: str | None) -> sqlite3.Row | None:
    """The cached row for a person by any id we have: key, username, or numeric id."""
    for col, value in (("key", key), ("username", username), ("numeric_id", numeric_id),
                       ("username", key), ("numeric_id", key)):
        if value:
            row = conn.execute(f"SELECT * FROM people WHERE {col} = ? COLLATE NOCASE", (value,)).fetchone()
            if row:
                return row
    return None


def get(key: str, username: str | None = None, numeric_id: str | None = None) -> dict | None:
    with closing(connect()) as conn:
        return _row(_find(conn, key, username, numeric_id))


def _upsert(conn: sqlite3.Connection, key: str, username: str | None, numeric_id: str | None,
            **cols) -> tuple[bool, str]:
    """Insert or update one person: (is_new, the row's key). Known values are never overwritten with None."""
    row = _find(conn, key, username, numeric_id)
    cols = {"username": username, "numeric_id": numeric_id, **cols}
    if row is None:
        cols = {"key": key, **{k: v for k, v in cols.items() if v is not None}}
        conn.execute(f"INSERT INTO people ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     list(cols.values()))
        return True, key
    sets = {k: v for k, v in cols.items() if v is not None}
    if username and row["key"] != username and row["key"] == row["numeric_id"]:
        sets["key"] = username  # learned the username for a row keyed by numeric id
    if sets:
        conn.execute(f"UPDATE people SET {', '.join(f'{k} = ?' for k in sets)} WHERE key = ?",
                     [*sets.values(), row["key"]])
    return False, sets.get("key", row["key"])


def save_friends(rows: list[dict], synced_at: str | None = None) -> dict:
    """Upsert Friends-tab rows. {"new": n, "updated": n}. The subtitle is replaced, even with None."""
    synced_at = synced_at or now()
    new = 0
    with closing(connect()) as conn, conn:
        for r in rows:
            is_new, key = _upsert(conn, r["key"], r.get("username"), r.get("numericId"), name=r.get("name"),
                                  profile_url=r.get("profileUrl"), is_friend=1, synced_at=synced_at)
            conn.execute("UPDATE people SET subtitle = ? WHERE key = ?", (r.get("subtitle"), key))
            new += is_new
    return {"new": new, "updated": len(rows) - new}


def save_about(about: dict, fetched_at: str | None = None) -> dict:
    fetched_at = fetched_at or now()
    record = {**about, "fetchedAt": fetched_at}
    with closing(connect()) as conn, conn:
        _upsert(conn, about["key"], about.get("username"), about.get("numericId"), name=about.get("name"),
                profile_url=about.get("profileUrl"), about=json.dumps(record), about_fetched_at=fetched_at)
    return record


def save_enrich(key: str, enrich: dict) -> dict:
    with closing(connect()) as conn, conn:
        row = _find(conn, key, None, None)
        if row is None:
            raise LookupError(f"{key} is not cached.")
        conn.execute("UPDATE people SET enrich = ?, enriched_at = ? WHERE key = ?",
                     (json.dumps(enrich), enrich["enrichedAt"], row["key"]))
    return enrich


def people() -> list[dict]:
    with closing(connect()) as conn:
        return [_row(r) for r in conn.execute("SELECT * FROM people ORDER BY name COLLATE NOCASE, key")]


# --- Search and flat rows (pure: they work on people() output) -----------------------------------

def _fold(text: str | None) -> str:
    import unicodedata

    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()


def _value(field: dict | None) -> str | None:
    return field.get("value") if isinstance(field, dict) else None


def current_job(about: dict | None) -> dict | None:
    return next((w for w in (about or {}).get("work") or [] if w.get("current") and w.get("employer")), None)


def flat(person: dict) -> dict:
    """One row per person for search output and CSV export. Enrichment wins over raw About data."""
    about = person.get("about") or {}
    enrich = person.get("enrich") or {}
    links = enrich.get("publicLinks") or {}
    job = current_job(about) or {}
    return {
        "key": person["key"],
        "name": person.get("name"),
        "subtitle": person.get("subtitle"),
        "city": _value(enrich.get("location")) or about.get("currentCity"),
        "hometown": about.get("hometown"),
        "employer": _value(enrich.get("company")) or job.get("employer"),
        "role": _value(enrich.get("role")) or job.get("role"),
        "education": "; ".join(e["school"] for e in about.get("education") or []) or None,
        "linkedin": _value(links.get("linkedin")),
        "website": _value(links.get("website")) or next(iter(about.get("websites") or []), None),
        "companyPage": _value(links.get("companyPage")),
        "profileUrl": person.get("profile_url"),
        "username": person.get("username"),
        "numericId": person.get("numeric_id"),
        "isFriend": person.get("is_friend"),
        "syncedAt": person.get("synced_at"),
        "aboutFetchedAt": person.get("about_fetched_at"),
        "enrichedAt": person.get("enriched_at"),
    }


def matches(person: dict, *, name: str | None = None, city: str | None = None, employer: str | None = None,
            keyword: str | None = None) -> bool:
    """Case- and accent-insensitive substring filters, all of which must hold."""
    about = person.get("about") or {}
    enrich = person.get("enrich") or {}
    row = flat(person)
    places = [about.get("currentCity"), about.get("hometown"), person.get("subtitle"), row["city"]]
    jobs = [w.get("employer") for w in about.get("work") or []] + [person.get("subtitle"), row["employer"]]
    blob = " ".join(map(str, [person.get("name"), person.get("subtitle"),
                              json.dumps(about, ensure_ascii=False), json.dumps(enrich, ensure_ascii=False)]))
    tests = ((name, [person.get("name")]), (city, places), (employer, jobs), (keyword, [blob]))
    return all(_fold(want) in " | ".join(_fold(h) for h in haystacks if h) for want, haystacks in tests if want)
