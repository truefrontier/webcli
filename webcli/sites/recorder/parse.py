"""Google Recorder's gRPC-web JSON (google-recorder-cli's index paths). Pure: no browser code."""

from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from typing import Any


def recordings(data: Any, tz: tzinfo | None = None) -> list[dict]:
    """GetRecordingList [[recording, ...], cursor] -> rows. Each recording: [id, title, [created_sec, ns],
    [duration_sec, ns], lat, lng, location, null, audio_info, tags, transcript_segments, ..., audio_id (13), ...].
    A recording that doesn't parse is skipped."""
    try:
        items = data[0]
    except (IndexError, KeyError, TypeError):
        return []
    rows = []
    for item in items if isinstance(items, list) else []:
        try:
            created = datetime.fromtimestamp(int(item[2][0]), tz=timezone.utc).astimezone(tz)
            rows.append({
                "id": str(item[0]),
                "title": str(item[1]),
                "created_at": created.isoformat(timespec="seconds"),
                "duration_seconds": int(item[3][0]),
                "has_transcript": bool(item[10]),
                "audio_id": str(item[13]) if item[13] else "",
            })
        except (IndexError, TypeError, ValueError):
            continue
    return rows


def transcript(data: Any) -> str:
    """GetTranscription [[unit, ...]] -> the words joined by spaces. Each unit: [[word, ...], 0, language]; each
    word: [text, display_text, start_ms, end_ms, null, null, [flag, speaker]]. A unit or word that doesn't parse
    is skipped."""
    try:
        units = data[0]
    except (IndexError, KeyError, TypeError):
        return ""
    words = []
    for unit in units if isinstance(units, list) else []:
        for word in unit[0] if isinstance(unit, list) and unit and isinstance(unit[0], list) else []:
            if isinstance(word, list) and word and isinstance(word[0], str):
                words.append(word[0])
    return " ".join(words).strip()


def find(rows: list[dict], recording_id: str) -> dict | None:
    return next((row for row in rows if row["id"] == recording_id), None)


def matches(row: dict, query: str) -> bool:
    return query.lower() in row["title"].lower()
