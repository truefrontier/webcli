"""Keep's changes JSON: its nodes as note rows, and whether a changes request body is a read-only sync."""

from __future__ import annotations

import json
from typing import Any

NOTE_TYPES = {"NOTE", "LIST"}


def note(node: Any) -> dict | None:
    """A top-level note or list (notes#node, NOTE/LIST, parent root) as a row; None for anything else (list items,
    blobs, labels)."""
    if not isinstance(node, dict) or node.get("kind") != "notes#node" or node.get("type") not in NOTE_TYPES:
        return None
    if node.get("parentId") not in (None, "", "root"):
        return None
    stamps = node.get("timestamps") if isinstance(node.get("timestamps"), dict) else {}
    return {
        "id": node.get("id") or "",
        "server_id": node.get("serverId") or "",
        "type": node["type"],
        "title": (node.get("title") or "").strip(),
        "text": (node.get("indexableText") or "").strip(),
        "pinned": bool(node.get("isPinned")),
        "archived": bool(node.get("isArchived")),
        "color": node.get("color") or "",
        "updated": stamps.get("userEdited") or stamps.get("updated") or "",
        "created": stamps.get("created") or "",
        "labels": [x.get("labelId") if isinstance(x, dict) else str(x) for x in node.get("labelIds") or []],
    }


def read_only_sync(body: str | None) -> bool:
    """True only for a changes body that uploads nothing: a JSON object whose nodes (edits) and userInfo (label
    changes) are empty or absent. Anything else, unreadable included, is False."""
    try:
        data = json.loads(body or "")
    except ValueError:
        return False
    return isinstance(data, dict) and not data.get("nodes") and not data.get("userInfo")


def display_title(row: dict, width: int = 60) -> str:
    """The title, else the text's first line (cut at `width`), else (untitled)."""
    if row.get("title"):
        return row["title"]
    lines = (row.get("text") or "").strip().splitlines()
    first = lines[0] if lines else "(untitled)"
    return first if len(first) <= width else first[: width - 1] + "…"


def matches(row: dict, query: str) -> bool:
    return query.lower() in " ".join([row.get("title") or "", row.get("text") or "", row.get("id") or ""]).lower()


def find(rows: list[dict], note_id: str) -> dict | None:
    """The note whose id or server id is `note_id`, else the first whose id contains it."""
    exact = next((r for r in rows if note_id in (r["id"], r["server_id"])), None)
    return exact or next((r for r in rows if note_id in r["id"]), None)


def text(row: dict) -> str:
    """Title, a blank line, then the body (keep get)."""
    parts = [row["title"], ""] if row.get("title") else []
    return "\n".join([*parts, row.get("text") or ""]).rstrip() + "\n"
