"""Pure helpers for webcli linkedin (limsg's parsers, kept as they were): messaging conversations and messages
(Voyager GraphQL or REST), Recent activity cards, /voyager/api/me, the post's inputs, and the post's write answers.
No browser, no network."""

from __future__ import annotations

import re
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlsplit

from webcli.core.errors import NotFound
from webcli.sites.linkedin.endpoints import FEED_UPDATE, MAX_IMAGE_BYTES, MAX_TEXT, MESSAGING_GQL

# --- Messaging ----------------------------------------------------------------------------------------------------

def _textish(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, dict):
        for k in ("text", "value", "localized", "name"):
            if k in val:
                return _textish(val.get(k))
        return ""
    return str(val).strip()


def _name_from_member(member: dict | None) -> str:
    if not isinstance(member, dict):
        return ""
    # Newer messenger shape: firstName/lastName as {text: ...}
    fn = _textish(member.get("firstName"))
    ln = _textish(member.get("lastName"))
    if fn or ln:
        return f"{fn} {ln}".strip()
    return _textish(member.get("name") or member.get("publicIdentifier") or "")


def _preview_from_conv(conv: dict) -> str:
    for key in ("lastMessage", "messages", "preview"):
        lm = conv.get(key)
        if isinstance(lm, dict):
            for tkey in ("text", "body", "snippet"):
                s = _textish(lm.get(tkey))
                if s:
                    return s[:160]
            ab = lm.get("attributedBody") or lm.get("body")
            s = _textish(ab)
            if s:
                return s[:160]
        if isinstance(lm, str):
            return lm[:160]
    return ""


def parse_conversations_payload(data: Any) -> list[dict]:
    """Normalize GraphQL or REST conversation list → agent rows."""
    rows: list[dict] = []
    if not isinstance(data, dict):
        return rows

    included = data.get("included")
    # GraphQL sometimes nests included under data
    if included is None and isinstance(data.get("data"), dict):
        included = data["data"].get("included")
    if not isinstance(included, list):
        included = []

    by_urn: dict[str, dict] = {}
    for obj in included:
        if not isinstance(obj, dict):
            continue
        urn = obj.get("entityUrn") or obj.get("backendUrn") or ""
        if urn:
            by_urn[urn] = obj

    participant_names: dict[str, str] = {}
    for obj in included:
        if not isinstance(obj, dict):
            continue
        t = obj.get("$type") or ""
        if "MessagingParticipant" in t or "Participant" in t:
            member = None
            pt = obj.get("participantType") or {}
            if isinstance(pt, dict):
                member = pt.get("member") or pt.get("organization")
            if member is None:
                member = obj.get("member") or obj
            name = _name_from_member(member if isinstance(member, dict) else None)
            urn = obj.get("entityUrn") or ""
            if name and urn:
                participant_names[urn] = name

    conversations: list[dict] = []
    for obj in included:
        if not isinstance(obj, dict):
            continue
        t = obj.get("$type") or ""
        if "Conversation" in t and "Participant" not in t:
            conversations.append(obj)

    # REST list: data.elements
    elements = data.get("elements")
    if isinstance(elements, list) and not conversations:
        for el in elements:
            if isinstance(el, dict):
                conversations.append(el)

    for conv in conversations:
        entity = conv.get("entityUrn") or conv.get("backendUrn") or conv.get("conversationId") or ""
        thread = ""
        m = re.search(r"(2-[A-Za-z0-9_=-]+)", str(entity))  # = too: thread ids are base64 (normalize_conversation_urn)
        if m:
            thread = m.group(1)
        elif entity:
            thread = str(entity)

        refs = (
            conv.get("*conversationParticipants")
            or conv.get("conversationParticipants")
            or conv.get("participants")
            or []
        )
        names: list[str] = []
        if isinstance(refs, list):
            for r in refs:
                if isinstance(r, str):
                    names.append(participant_names.get(r) or by_urn.get(r, {}).get("name") or "")
                elif isinstance(r, dict):
                    n = _name_from_member(r.get("member") if isinstance(r.get("member"), dict) else r)
                    if not n:
                        mini = r.get("miniProfile") or {}
                        n = _name_from_member(mini if isinstance(mini, dict) else None)
                    names.append(n)
        names = [n for n in names if n]
        peer = ", ".join(names) if names else (conv.get("title") or "(unknown)")

        unread = conv.get("unreadCount")
        if unread is None:
            unread = conv.get("unread")
        last_at = (
            conv.get("lastActivityAt")
            or conv.get("lastActivityTime")
            or conv.get("deliveredAt")
            or ""
        )
        preview = _preview_from_conv(conv)

        rows.append(
            {
                "id": thread or str(entity),
                "entityUrn": str(entity) if entity else "",
                "peer": peer,
                "preview": preview,
                "time": last_at,
                "unread": int(unread) if isinstance(unread, (int, float)) else (1 if unread else 0),
            }
        )

    # Sort by time desc when numeric
    def sort_key(r: dict) -> float:
        t = r.get("time")
        try:
            return float(t)
        except (TypeError, ValueError):
            return 0.0

    rows.sort(key=sort_key, reverse=True)
    return rows


def _ms_to_iso(value: Any) -> str:
    """Convert LinkedIn ms epoch (int/str) to ISO-8601 UTC; pass through strings that look ISO."""
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return ""
        if "T" in s or s.endswith("Z"):
            return s
        try:
            value = float(s)
        except ValueError:
            return s
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return str(value)
    # Heuristic: seconds vs milliseconds
    if ms < 1e12:
        ms *= 1000.0
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_conversation_urn(conversation_id_or_urn: str) -> tuple[str, str]:
    """Return (thread_id, conversation_urn_for_api).

    Accepts peer-thread id (2-…), entityUrn, or msg_conversation URN.
    """
    raw = (conversation_id_or_urn or "").strip()
    if not raw:
        raise ValueError("empty conversation id")
    m = re.search(r"(2-[A-Za-z0-9_=-]+)", raw)
    thread = m.group(1) if m else raw
    if raw.startswith("urn:li:"):
        # Keep full URN (including parenthetical msg_conversation forms)
        if "msg_conversation" in raw or "fs_conversation" in raw or "fsd_conversation" in raw:
            return thread, raw
        # Unknown urn type that still embeds a thread id → wrap as msg_conversation
        if m:
            return thread, f"urn:li:msg_conversation:{thread}"
        return thread, raw
    return thread, f"urn:li:msg_conversation:{thread}"


def _text_from_body(obj: Any) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if not isinstance(obj, dict):
        return ""
    for key in ("text", "body", "snippet"):
        v = obj.get(key)
        if isinstance(v, str):
            return v
        if isinstance(v, dict) and isinstance(v.get("text"), str):
            return v["text"]
    ab = obj.get("attributedBody") or obj.get("body")
    if isinstance(ab, dict) and isinstance(ab.get("text"), str):
        return ab["text"]
    if isinstance(ab, str):
        return ab
    return ""


def _sender_name_and_urn(obj: dict, by_urn: dict[str, dict]) -> tuple[str, str]:
    """Extract display name + sender profile/participant URN from a Message or Event."""
    sender_urn = ""
    name = ""

    def _name_from_participant(ref: dict | None) -> str:
        if not isinstance(ref, dict):
            return ""
        pt = ref.get("participantType") or {}
        if isinstance(pt, dict):
            for key in ("member", "organization", "agent"):
                cand = pt.get(key)
                if isinstance(cand, dict):
                    n = _name_from_member(cand)
                    if n:
                        return n
        n = _name_from_member(ref.get("member") or ref.get("miniProfile") or ref)
        return n

    # GraphQL messenger.Message uses *sender / *actor (plain sender is often absent)
    sender = obj.get("sender") or obj.get("*sender") or obj.get("actor") or obj.get("*actor")
    if isinstance(sender, str):
        sender_urn = sender
        ref = by_urn.get(sender) or {}
        name = _name_from_participant(ref)
    elif isinstance(sender, dict):
        sender_urn = str(sender.get("entityUrn") or sender.get("backendUrn") or "")
        name = _name_from_participant(sender)
        if not name and sender_urn:
            name = _name_from_participant(by_urn.get(sender_urn))

    # REST Event: *from / from
    if not name:
        from_ref = obj.get("*from") or obj.get("from")
        if isinstance(from_ref, str):
            sender_urn = sender_urn or from_ref
            ref = by_urn.get(from_ref) or {}
            name = _name_from_participant(ref) if ref else ""
            if not name and isinstance(ref, dict):
                mini = ref.get("miniProfile") or ref.get("member") or ref
                if isinstance(mini, str):
                    mini = by_urn.get(mini) or {}
                name = _name_from_member(mini if isinstance(mini, dict) else None)
                mp = ref.get("*miniProfile") or ref.get("miniProfile")
                if not name and isinstance(mp, str):
                    name = _name_from_member(by_urn.get(mp))
                if not name and isinstance(mp, dict):
                    name = _name_from_member(mp)
        elif isinstance(from_ref, dict):
            sender_urn = sender_urn or str(from_ref.get("entityUrn") or "")
            mini = from_ref.get("miniProfile") or from_ref
            name = _name_from_member(mini if isinstance(mini, dict) else None)

    return (name or "").strip(), sender_urn


def _label_from(name: str, sender_urn: str, self_urn: str | None) -> str:
    if self_urn and sender_urn and self_urn in sender_urn:
        return "me"
    if self_urn and sender_urn:
        # Compare profile id suffixes
        a = re.search(r"(ACoAA[A-Za-z0-9_-]+|fsd_profile:[A-Za-z0-9_-]+)", self_urn)
        b = re.search(r"(ACoAA[A-Za-z0-9_-]+|fsd_profile:[A-Za-z0-9_-]+)", sender_urn)
        if a and b and a.group(1).split(":")[-1] == b.group(1).split(":")[-1]:
            return "me"
    return name or "peer"


def parse_messages_payload(data: Any, *, self_urn: str | None = None) -> list[dict]:
    """Normalize GraphQL messengerMessages or REST conversation events → agent rows.

    Each row: id, time (ISO), from, text, senderUrn (optional).
    """
    rows: list[dict] = []
    if not isinstance(data, dict):
        return rows

    included_raw = data.get("included")
    if included_raw is None and isinstance(data.get("data"), dict):
        included_raw = data["data"].get("included")

    # included may be list (common) or dict keyed by index (rare REST variants)
    included_list: list[dict] = []
    by_urn: dict[str, dict] = {}
    if isinstance(included_raw, list):
        included_list = [o for o in included_raw if isinstance(o, dict)]
    elif isinstance(included_raw, dict):
        for v in included_raw.values():
            if isinstance(v, dict):
                included_list.append(v)
                # also allow lookup by string keys that look like URNs
        for k, v in included_raw.items():
            if isinstance(v, dict) and isinstance(k, str) and k.startswith("urn:"):
                by_urn[k] = v

    for obj in included_list:
        urn = obj.get("entityUrn") or obj.get("backendUrn") or ""
        if urn:
            by_urn[str(urn)] = obj

    messages: list[dict] = []
    for obj in included_list:
        t = str(obj.get("$type") or "")
        # GraphQL: com.linkedin.messenger.Message
        if ("messenger.Message" in t) or (t.endswith(".Message") and "Conversation" not in t):
            messages.append(obj)
            continue
        # REST: com.linkedin.voyager.messaging.Event
        if "messaging.Event" in t or t.endswith(".Event"):
            messages.append(obj)
            continue
        # Fallback: has body/deliveredAt typical of Message
        if obj.get("deliveredAt") is not None and (obj.get("body") is not None or obj.get("sender") is not None):
            messages.append(obj)
            continue
        if obj.get("eventContent") is not None and obj.get("createdAt") is not None:
            messages.append(obj)

    # REST sometimes puts events under data.elements
    elements = data.get("elements")
    if isinstance(elements, list) and not messages:
        for el in elements:
            if isinstance(el, dict):
                messages.append(el)

    # GraphQL collection under data.data.messengerMessagesByConversation etc.
    if not messages and isinstance(data.get("data"), dict):
        d0 = data["data"]
        # nested data.data.*.elements
        stack = [d0]
        while stack and not messages:
            cur = stack.pop()
            if not isinstance(cur, dict):
                continue
            for key, val in cur.items():
                if key in {"elements", "*elements"} and isinstance(val, list):
                    for el in val:
                        if isinstance(el, str):
                            ref = by_urn.get(el)
                            if ref:
                                messages.append(ref)
                        elif isinstance(el, dict):
                            messages.append(el)
                elif isinstance(val, dict) and (
                    "Message" in key or "message" in key or "messenger" in key.lower()
                ):
                    stack.append(val)

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        mid = str(msg.get("entityUrn") or msg.get("backendUrn") or msg.get("id") or "")
        # Skip non-message scaffolding
        t = str(msg.get("$type") or "")
        if "Conversation" in t and "Message" not in t:
            continue

        text = _text_from_body(msg.get("body"))
        if not text:
            text = _text_from_body(msg.get("eventContent"))
        if not text:
            text = _text_from_body(msg.get("attributedBody"))
        if not text and isinstance(msg.get("renderContent"), list):
            # attachment-only — leave empty text with marker
            text = ""

        when = msg.get("deliveredAt") or msg.get("createdAt") or msg.get("lastActivityAt") or ""
        name, sender_urn = _sender_name_and_urn(msg, by_urn)
        from_label = _label_from(name, sender_urn, self_urn)

        # Skip empty system stubs with no id and no text
        if not mid and not text:
            continue

        rows.append(
            {
                "id": mid,
                "time": _ms_to_iso(when),
                "from": from_label,
                "text": text,
                "senderUrn": sender_urn,
            }
        )

    def sort_key(r: dict) -> float:
        t = r.get("time") or ""
        # ISO sorts lexicographically for Z times; also try raw ms in id-less cases
        if isinstance(t, str) and t.endswith("Z") and "T" in t:
            try:
                return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return 0.0
        try:
            return float(t)
        except (TypeError, ValueError):
            return 0.0

    # Newest last in chat UIs; Career wants "last N" — return chronological ascending
    # then caller can slice [-limit:]. We sort ascending here.
    rows.sort(key=sort_key)
    # Dedupe by id
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        k = r.get("id") or f"{r.get('time')}|{r.get('from')}|{r.get('text')}"
        if k in seen:
            continue
        seen.add(str(k))
        out.append(r)
    return out


def conversations(data: Any) -> list[dict]:
    """Conversation rows {id, entityUrn, peer, preview, time (ISO 8601), unread}, newest first."""
    return [{**row, "time": _ms_to_iso(row["time"])} for row in parse_conversations_payload(data)]


def resolve_thread(rows: list[dict], target: str) -> dict | None:
    """The thread `target` names: an exact thread id or entityUrn (ignoring case), else the one thread whose peer
    contains it. None when nothing matches; NotFound (exit 3) when several peers do."""
    q = target.strip().lower()
    for row in rows:
        if row.get("id", "").lower() == q or row.get("entityUrn", "").lower() == q:
            return row
    found = [row for row in rows if q in (row.get("peer") or "").lower()]
    if len(found) > 1:
        raise NotFound(f"{len(found)} threads match {target!r}: use a thread id.",
                       hint="See `webcli linkedin messages list --compact`.")
    return found[0] if found else None


def query_id(url: str) -> str | None:
    """The messaging GraphQL queryId ("messengerMessages.<hash>") a request URL carries, else None."""
    parts = urlsplit(url)
    if f"{parts.scheme}://{parts.netloc}{parts.path}" != MESSAGING_GQL:
        return None
    found = parse_qs(parts.query).get("queryId", [])
    return found[0] if len(found) == 1 and re.fullmatch(r"[A-Za-z]+\.[0-9a-f]{16,64}", found[0]) else None


def load_message(text: str | None) -> str:
    """A DM's text: trimmed, and not empty."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Message text is empty.")
    return text



# --- Recent activity (READ-ONLY: the rendered cards of /in/<vanity>/recent-activity/...) ---------------------------

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


_REL_TIME = re.compile(r"^(\d+)\s*([smhdw])$", re.I)


_ABS_TIME = re.compile(
    r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})(?:,\s*(\d{4}))?$",
    re.I,
)


_TIME_LINE = re.compile(
    r"^(\d+\s*[smhdw]|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}(?:,\s*\d{4})?)\s*•?$",
    re.I,
)


_URN = re.compile(r"urn:li:[A-Za-z0-9_:-]+")


_NOISE = re.compile(
    r"^(feed post|follow|following|load more comments|… more|\.\.\. more|"
    r"•\s*(you|1st|2nd|3rd|following)|you|"
    r"\d+|[\d,]+\s+impressions?|book an appointment|"
    r"this post type can't be boosted\.?)$",
    re.I,
)


_END = re.compile(r"^(… more|\.\.\. more|load more comments|\d+\s+impressions?)$", re.I)


def parse_since(value: str | None, *, flag: str = "--since") -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid {flag}. Use ISO-8601, for example 2026-10-01 or 2026-10-01T00:00:00Z.") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def linkedin_time_to_iso(token: str, *, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    text = (token or "").replace("•", "").strip()
    rel = _REL_TIME.fullmatch(text)
    if rel:
        n = int(rel.group(1))
        unit = rel.group(2).lower()
        delta = {
            "s": timedelta(seconds=n),
            "m": timedelta(minutes=n),
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
            "w": timedelta(weeks=n),
        }[unit]
        return _iso(now - delta)
    abs_m = _ABS_TIME.fullmatch(text)
    if abs_m:
        year = int(abs_m.group(3) or now.year)
        month = _MONTHS[abs_m.group(1).lower()[:3]]
        day = int(abs_m.group(2))
        try:
            dt = datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            return text
        if abs_m.group(3) is None and dt > now + timedelta(days=1):
            dt = dt.replace(year=year - 1)
        return _iso(dt)
    return text


def _is_time_line(line: str) -> bool:
    return bool(_TIME_LINE.fullmatch(line.strip()))


def _clean_time_token(line: str) -> str:
    return line.replace("•", "").strip()


def _is_noise(line: str) -> bool:
    return bool(_NOISE.fullmatch(line.strip()))


def _is_end(line: str) -> bool:
    text = line.strip()
    if _END.fullmatch(text):
        return True
    if re.fullmatch(r"\d+", text):
        return True
    return False


def _you_index(lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        low = line.lower().strip()
        if low in {"• you", "you"} or low.endswith("• you"):
            return i
    return None


def _text_after_own_time(lines: list[str]) -> tuple[str, str]:
    """Return (time token, text) from the viewer's block on a card."""
    you = _you_index(lines)
    if you is None:
        return "", ""
    time_token = ""
    seen_time = False
    chunks: list[str] = []
    for line in lines[you + 1 :]:
        if not seen_time:
            if _is_time_line(line):
                time_token = _clean_time_token(line)
                seen_time = True
            continue
        if _is_end(line) or _is_noise(line):
            break
        chunks.append(line)
    return time_token, "\n".join(chunks).strip()


def _first_time(lines: list[str]) -> str:
    for line in lines:
        if _is_time_line(line):
            return _clean_time_token(line)
    return ""


def _content_after_first_time(lines: list[str]) -> str:
    seen = False
    chunks: list[str] = []
    for line in lines:
        if not seen:
            if _is_time_line(line):
                seen = True
            continue
        if _is_end(line) or _is_noise(line):
            if chunks:
                break
            continue
        chunks.append(line)
        if len(chunks) >= 8:
            break
    return "\n".join(chunks).strip()


def _reaction_label(lines: list[str]) -> str:
    blob = " ".join(lines[:8]).lower()
    for phrase, label in (
        ("found this funny", "funny"),
        ("finds this funny", "funny"),
        ("found this insightful", "insightful"),
        ("finds this insightful", "insightful"),
        ("celebrates", "celebrate"),
        ("celebrate", "celebrate"),
        ("supports", "support"),
        ("supported", "support"),
        ("loved", "love"),
        ("loves", "love"),
        ("likes", "like"),
        ("liked", "like"),
    ):
        if phrase in blob:
            return label
    return ""


def _target(links: list[dict[str, str]]) -> tuple[str, str]:
    pairs = _link_pairs(links)
    preferred = ("/feed/update/", "/pulse/", "/posts/")
    for href, text in pairs:
        if any(part in href for part in preferred):
            return href, text
    social: list[tuple[str, str]] = []
    for href, text in pairs:
        if "/recent-activity/" in href or "/safety/" in href:
            continue
        if "/in/" in href or "/company/" in href:
            social.append((href, text))
    # The viewer's profile is usually the first link. The next person or company is the target.
    if len(social) >= 2:
        href, text = social[1]
        if not text:
            text = next((label for link, label in social if link == href and label), "")
        return href, text
    if social:
        return social[0]
    return "", ""


def _link_pairs(links: list[dict[str, str]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for link in links:
        href = (link.get("href") or "").strip()
        if not href or href.startswith("javascript:"):
            continue
        if href.startswith("/"):
            href = urljoin("https://www.linkedin.com", href)
        text = re.sub(r"\s+", " ", (link.get("text") or "").strip())
        out.append((href, text))
    return out


def _stable_id(raw_id: str, href: str) -> str:
    match = _URN.search(href or "")
    if match:
        return match.group(0)
    token = raw_id or ""
    token = re.sub(r"^expanded", "", token)
    token = re.sub(r"FeedType_.*$", "", token)
    return token or href


def normalize_card(kind: str, card: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    lines = [str(x).strip() for x in (card.get("lines") or []) if str(x).strip()]
    links = card.get("links") or []
    target_url, target_title = _target(links)
    own_time, own_text = _text_after_own_time(lines)
    post_url = ""
    for href, _text in _link_pairs(links):
        if "/feed/update/" in href or "/pulse/" in href or "/posts/" in href:
            post_url = href
            break
    row_id = _stable_id(str(card.get("id") or ""), post_url or target_url)
    if kind == "comments":
        when = own_time or _first_time(lines)
        text = own_text or _content_after_first_time(lines)
        return {
            "id": row_id,
            "time": linkedin_time_to_iso(when, now=now) if when else "",
            "type": "comment",
            "text": text,
            "targetUrl": target_url,
            "targetTitle": target_title,
        }
    if kind == "reactions":
        when = _first_time(lines)
        content_title = _content_after_first_time(lines).split("\n", 1)[0]
        if content_title:
            target_title = content_title
        return {
            "id": row_id,
            "time": linkedin_time_to_iso(when, now=now) if when else "",
            "type": "reaction",
            "reaction": _reaction_label(lines),
            "targetUrl": target_url,
            "targetTitle": target_title,
        }
    when = own_time or _first_time(lines)
    text = own_text or _content_after_first_time(lines)
    return {
        "id": row_id,
        "time": linkedin_time_to_iso(when, now=now) if when else "",
        "type": "post",
        "text": text,
        "url": post_url or target_url,
    }


def apply_since(rows: list[dict[str, Any]], since: datetime | None) -> list[dict[str, Any]]:
    if since is None:
        return rows
    kept: list[dict[str, Any]] = []
    for row in rows:
        stamp = str(row.get("time") or "")
        if stamp.endswith("Z"):
            stamp = stamp[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt >= since:
            kept.append(row)
    return kept


# --- Posts and /me --------------------------------------------------------------------------------------------------

_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


_POST_URN = re.compile(r"urn:li:(?:share|ugcPost|activity):\d+")


def utf16_len(text: str) -> int:
    """Length as LinkedIn's composer counts it (UTF-16 code units, so an emoji is 2)."""
    return len(text.encode("utf-16-le")) // 2


def load_text(text: str | None, text_file: str | None) -> str:
    if (text is None) == (text_file is None):
        raise ValueError("Pass exactly one of --text or --text-file.")
    if text_file is not None:
        try:
            text = Path(text_file).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"Cannot read --text-file {text_file}: {exc}") from exc
    text = text.strip()
    if not text:
        raise ValueError("Post text is empty.")
    n = utf16_len(text)
    if n > MAX_TEXT:
        raise ValueError(f"Post text is {n} characters; LinkedIn allows {MAX_TEXT}.")
    return text


def _dimensions(mime: str, data: bytes) -> tuple[int, int] | None:
    if mime == "image/png" and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    if mime == "image/gif" and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    if mime == "image/jpeg":
        i = 2
        while i + 9 < len(data) and data[i] == 0xFF:
            marker = data[i + 1]
            if marker == 0xFF:  # fill byte
                i += 1
            elif marker == 0x01 or 0xD0 <= marker <= 0xD8:  # standalone, no length
                i += 2
            # SOF0..SOF15 carry the frame size, except DHT (C4), JPG (C8), DAC (CC).
            elif 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                return w, h
            else:
                i += 2 + struct.unpack(">H", data[i + 2 : i + 4])[0]
    return None


def load_image(path: str) -> dict[str, Any]:
    """Validate one image and return {path, filename, type, bytes, width, height, data}."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise ValueError(f"Image not found: {path}")
    with p.open("rb") as fh:
        data = fh.read(MAX_IMAGE_BYTES + 1)
    if not data:
        raise ValueError(f"Image is empty: {path}")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"Image is over {MAX_IMAGE_BYTES} bytes (10 MB): {path}")
    mime = next((m for magic, m in _MAGIC if data.startswith(magic)), None)
    if mime is None:
        raise ValueError(f"Image must be JPEG, PNG, or GIF (checked file contents): {path}")
    dims = _dimensions(mime, data)
    return {
        "path": str(p.resolve()),
        "filename": p.name,
        "type": mime,
        "bytes": len(data),
        "width": dims[0] if dims else None,
        "height": dims[1] if dims else None,
        "data": data,
    }


def image_summary(image: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in image.items() if k != "data"}


def _value(data: Any) -> dict:
    """Restli action/record payload, whether or not the normalized response wraps it in data."""
    if not isinstance(data, dict):
        return {}
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    value = inner.get("value")
    return value if isinstance(value, dict) else inner


def parse_me(data: Any) -> dict[str, Any] | None:
    """Signed-in author from GET /voyager/api/me, or None unless the payload pins down one profile.

    `included` can hold other people's profiles, so the author is the one data["*miniProfile"]
    (or data.miniProfile.entityUrn) points at. Without that pointer, only a lone profile counts.
    It must carry a profile urn and a member urn, so the author re-check compares real ids.
    """
    if not isinstance(data, dict):
        return None
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    mini = inner.get("miniProfile") if isinstance(inner.get("miniProfile"), dict) else {}
    candidates = [
        o for o in [*(data.get("included") or []), mini] if isinstance(o, dict) and o.get("publicIdentifier")
    ]
    ref = inner.get("*miniProfile") or mini.get("entityUrn")
    if ref:
        candidates = [o for o in candidates if ref in (o.get("entityUrn"), o.get("dashEntityUrn"))]
    if len(candidates) != 1:
        return None
    profile = candidates[0]
    urn = str(profile.get("dashEntityUrn") or profile.get("entityUrn") or "")
    plain_id = inner.get("plainId")
    member_urn = profile.get("objectUrn") or (f"urn:li:member:{plain_id}" if plain_id else None)
    if not urn or not member_urn:
        return None
    return {
        "name": f"{profile.get('firstName') or ''} {profile.get('lastName') or ''}".strip(),
        "publicIdentifier": profile.get("publicIdentifier"),
        "profileUrn": urn.replace("fs_miniProfile", "fsd_profile"),
        "memberUrn": member_urn,
    }


def parse_register(data: Any) -> tuple[str, str, dict[str, str]]:
    """(asset urn, upload url, extra upload headers) from the register response."""
    value = _value(data)
    if value.get("type") not in (None, "SINGLE"):
        raise RuntimeError(f"LinkedIn asked for a {value['type']} upload, which webcli does not do. Nothing was posted.")
    urn = value.get("urn")
    url = value.get("singleUploadUrl")
    if not urn or not url:
        raise RuntimeError("Image register response had no asset urn or upload URL. Nothing was posted.")
    headers = value.get("singleUploadHeaders")
    return str(urn), str(url), {str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {}


def parse_created_urn(data: Any, restli_id: str | None = None) -> str | None:
    """Post urn from the create response: data.status.urn, value.urn, else the x-restli-id header.

    Never "some post urn somewhere in the body": that can be another post (even someone
    else's). With no urn, the caller reports the post as live but unidentified.
    """
    value = _value(data)
    for holder in (value.get("status"), value):
        if isinstance(holder, dict):
            m = _POST_URN.fullmatch(str(holder.get("urn") or ""))
            if m:
                return m.group(0)
    m = _POST_URN.fullmatch(unquote(restli_id or ""))
    return m.group(0) if m else None


def share_body(text: str, asset_urn: str) -> dict[str, Any]:
    return {
        "visibleToConnectionsOnly": False,
        "externalAudienceProviders": [],
        "commentaryV2": {"text": text, "attributes": []},
        "origin": "FEED",
        "allowedCommentersScope": "ALL",
        "postState": "PUBLISHED",
        "media": [{"category": "IMAGE", "mediaUrn": asset_urn, "tapTargets": []}],
    }


def check_upload_url(url: str) -> None:
    parts = urlparse(url)
    host = parts.hostname or ""
    # Chrome reads a backslash as "/", so https://evil\@www.linkedin.com/ goes to evil; urlparse disagrees.
    odd = "\\" in url or any(c.isspace() or ord(c) < 32 for c in url) or parts.netloc.lower() != host
    if odd or parts.scheme != "https" or not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        raise RuntimeError(f"Refusing image upload to unexpected URL host {parts.netloc!r}. Nothing was posted.")



def post_url(urn: str) -> str:
    return FEED_UPDATE.format(urn=urn)
