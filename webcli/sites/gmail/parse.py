"""Gmail filter rows from the settings table, and the create plan. No browser code."""

from __future__ import annotations

import re
from typing import Any

from webcli.sites.gmail.endpoints import ACTION_LABELS, CRITERIA_LABELS

_MATCHES = re.compile(r"Matches:\s*(.*?)\s*Do this:\s*(.*)$", re.I | re.S)


def criteria_fields(text: str) -> dict[str, Any]:
    """from / to / subject / query / hasAttachment, read off a row's criteria text ("from:(a@example.com)")."""
    if not text:
        return {}
    lower = text.lower()
    for key in ("from", "to", "subject"):
        if lower.startswith(key + ":"):
            return {key: text.split(":", 1)[1].strip()}
    if "has:attachment" in lower:
        rest = re.sub(r"has:attachment", "", text, flags=re.I).strip()
        return {"hasAttachment": True, **({"query": rest} if rest else {})}
    return {"query": text}


def normalize(raw: dict) -> dict:
    """One scraped row ({id, criteria, actionText}) as {id, matches, do_this, from, to, subject, query,
    hasAttachment, criteria}."""
    criteria = (raw.get("criteria") or "").strip()
    action_text = (raw.get("actionText") or "").strip()
    found = _MATCHES.search(action_text)
    matches, do_this = (found.group(1).strip(), found.group(2).strip()) if found else (criteria, action_text)
    parsed = criteria_fields(criteria)
    return {"id": raw.get("id") or "", "matches": matches or criteria, "do_this": do_this,
            "from": parsed.get("from") or "", "to": parsed.get("to") or "", "subject": parsed.get("subject") or "",
            "query": parsed.get("query") or "", "hasAttachment": parsed.get("hasAttachment"), "criteria": criteria}


def find(rows: list[dict], filter_id: str) -> dict | None:
    """The row whose id is exactly `filter_id` (no prefix or substring match: a delete must hit one filter)."""
    return next((r for r in rows if r["id"] and r["id"] == filter_id.strip()), None)


def create_plan(criteria: dict[str, Any], actions: list[str]) -> dict:
    """{criteria, actions}: what `filters create` fills in and ticks. ValueError without a criterion or an action."""
    criteria = {k: v for k, v in criteria.items()
                if (k in CRITERIA_LABELS and isinstance(v, str) and v.strip()) or (k == "hasAttachment" and v is True)}
    actions = [a for a in ACTION_LABELS if a in actions]
    if not criteria:
        raise ValueError("Create needs at least one criterion: --from, --to, --subject, --query, --negated-query, or "
                         "--has-attachment.")
    if not actions:
        raise ValueError("Create needs at least one action: --skip-inbox, --mark-as-read, --star, --delete-it, or "
                         "--never-spam.")
    return {"criteria": criteria, "actions": actions}


def needles(plan: dict) -> tuple[str, ...]:
    """The text the create request must carry: each typed criterion (the guard binds the write to it)."""
    return tuple(v for k, v in plan["criteria"].items() if k in CRITERIA_LABELS)
