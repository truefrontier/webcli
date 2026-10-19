"""Pure parsers for ChatGPT's JSON API and llmcli chatgpt's ids. No browser code.

A conversation record (GET /backend-api/conversation/<id>) is a tree: mapping{node_id: {message, parent, children}}
and current_node, the leaf of the branch the page shows. llmcli chatgpt reads that branch, root first. A message is
{id, author{role, name}, content{content_type, parts}, status, end_turn, recipient, metadata{model_slug,
requested_model_slug, resolved_model_slug, default_model_slug, ...}}. A generated image is a part
{content_type: "image_asset_pointer", asset_pointer: "sediment://file_..." (or "file-service://file-..."), width,
height, size_bytes, mime_type, metadata{generation, dalle}} in a message from the image tool (author role "tool").
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

ORIGIN = "https://chatgpt.com"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_CHAT_URL = re.compile(r"^https?://(?:www\.)?chatgpt\.com/(?:g/[\w-]+/)?c/([^/?#]+)", re.IGNORECASE)
_EMAIL = re.compile(r"^([^@\s])[^@\s]*(@[^@\s]+)$")
IMAGE_PART = "image_asset_pointer"
# Content types whose string parts are the visible answer. thoughts, reasoning_recap, code, and execution_output
# are the model's working, not the reply.
TEXT_TYPES = ("text", "multimodal_text")


def clean_chat_id(value: str) -> str:
    """A chat id as it appears in chatgpt.com/c/<id> (a UUID). Accepts the full URL, project URLs included."""
    text = value.strip()
    if match := _CHAT_URL.match(text):
        text = match.group(1)
    if not _UUID.fullmatch(text):
        raise ValueError(f"{value!r} is not a ChatGPT chat id (the id in chatgpt.com/c/<id>).")
    return text.lower()


def url_chat_id(url: str) -> str | None:
    match = _CHAT_URL.match(url or "")
    return match.group(1).lower() if match and _UUID.fullmatch(match.group(1)) else None


def chat_url(chat_id: str) -> str:
    return f"{ORIGIN}/c/{chat_id}"


def mask_email(email: str | None) -> str | None:
    """"owner.name@example.com" -> "o***@example.com"."""
    if not email:
        return None
    match = _EMAIL.match(email.strip())
    return f"{match.group(1)}***{match.group(2)}" if match else "***"


# Cloudflare's interstitial ("Just a moment...") and its interactive check ("Verify you are human").
CHALLENGE_TITLES = ("just a moment", "attention required", "verify you are human", "one more step",
                    "checking your browser", "please wait")


def cloudflare_challenge(state: dict) -> bool:
    """Whether a page is Cloudflare's challenge rather than ChatGPT. `state` is browser.CHALLENGE_JS's answer:
    {title, url, form (a challenge form/Turnstile widget in the page), iframe (a challenges.cloudflare.com frame),
    app (ChatGPT's own composer or nav)}. The app itself loads /cdn-cgi/challenge-platform/ scripts on every page,
    so those don't count; a Turnstile frame only counts when the app isn't there."""
    title = " ".join((state.get("title") or "").split()).casefold()
    url = state.get("url") or ""
    return (any(title.startswith(t) for t in CHALLENGE_TITLES) or "__cf_chl" in url or "cf_chl_" in url
            or bool(state.get("form")) or (bool(state.get("iframe")) and not state.get("app")))


def iso_time(value: Any) -> str | None:
    """An API timestamp (epoch seconds, or an ISO string) as ISO 8601 UTC."""
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value:
        try:
            when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return None


def models(payload: Any) -> list[dict]:
    """GET /backend-api/models -> [{slug, title, description, default, reasoning, images}] in the API's order.
    default: ChatGPT's default model (default_model_slug); images: the model can call the image tool."""
    payload = payload if isinstance(payload, dict) else {}
    default = payload.get("default_model_slug")
    rows = []
    for item in payload.get("models") or []:
        if not isinstance(item, dict) or not item.get("slug"):
            continue
        rows.append({"slug": item["slug"], "title": item.get("title"), "description": item.get("description"),
                     "default": item["slug"] == default, "reasoning": item.get("reasoning_type"),
                     "images": "image_gen_tool_enabled" in (item.get("enabled_tools") or [])})
    return rows


def match_model(rows: list[dict], wanted: str) -> str:
    """The slug `wanted` names: a slug (case-insensitive), else a title or slug fragment that names exactly one
    model. LookupError (with what's available) otherwise."""
    key = " ".join(wanted.casefold().split())
    slugs = [r["slug"] for r in rows]
    for test in (lambda r: r["slug"].casefold() == key,
                 lambda r: " ".join((r["title"] or "").casefold().split()) == key,
                 lambda r: key in r["slug"].casefold()):
        hits = [r["slug"] for r in rows if test(r)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise LookupError(f"{wanted!r} matches more than one model: {', '.join(hits)}. Use a slug.")
    raise LookupError(f"No model matches {wanted!r}. Available: {', '.join(slugs) or 'none'}.")


def picker_choice(payload: Any, slug: str) -> dict | None:
    """Where the model picker (model_picker_version 2) offers `slug`: {version, labels, preset, index, sends}.
    version is the radio's label ("GPT-6", "GPT-5.6 Sol"; labels lists every name the radio may show), preset the
    Power slider's stop ("Instant", "Medium", "High"), index its position, and sends the model slugs the page may put
    in the prompt's body for that stop. The first stop (Instant) sends the version's auto slug (VERIFIED 2026-10-18:
    "Instant" on GPT-6 sends "gpt-6", which ChatGPT routes), so an auto slug picks that stop too. The first preset
    with the slug wins (gpt-6-thinking is Medium; High is the same slug with more thinking). None when the picker
    doesn't offer it."""
    payload = payload if isinstance(payload, dict) else {}
    autos = [m["slug"] for m in payload.get("models") or [] if isinstance(m, dict) and m.get("slug")
             and m.get("reasoning_type") == "auto"]
    for version in payload.get("versions") or []:
        if not isinstance(version, dict) or version.get("enabled") is False:
            continue
        presets = [p for p in version.get("intelligence_presets") or [] if isinstance(p, dict)
                   and p.get("preset_type", "available") == "available"]
        if not presets:
            continue
        first = presets[0].get("model_slug")
        auto = next((a for a in autos if first and same_model(a, first)), None)
        for index, preset in enumerate(presets):
            target = preset.get("model_slug")
            if slug not in (target, auto if index == 0 else None):
                continue
            labels = [version.get(k) for k in ("display_text_for_intelligence", "display_text",
                                               "short_display_text_for_intelligence", "display_text_full")]
            labels = [label for label in dict.fromkeys(labels) if label]
            sends = [target] + ([auto] if index == 0 and auto else [])
            return {"version": labels[0] if labels else None, "labels": labels, "preset": preset.get("title"),
                    "index": preset["id"] if isinstance(preset.get("id"), int) else index,
                    "sends": [s for s in dict.fromkeys(sends) if s]}
    return None


def display_name(payload: Any, slug: str | None) -> str | None:
    """A model's name as ChatGPT shows it: its title plus its category ("GPT-6 Thinking", "GPT-6 Instant"), else
    its title, else None."""
    if not slug:
        return None
    payload = payload if isinstance(payload, dict) else {}
    title = next((m.get("title") for m in payload.get("models") or [] if isinstance(m, dict) and m.get("slug") == slug),
                 None)
    category = next((c.get("human_category_name") for c in payload.get("categories") or []
                     if isinstance(c, dict) and (c.get("default_model") == slug or slug in (c.get("supported_models") or []))),
                    None)
    if title and category and not title.casefold().endswith(category.casefold()):
        return f"{title} {category}"
    return title or category


def _base(slug: str) -> str:
    return re.sub(r"-auto(?=-|$)", "", slug.casefold())


def same_model(requested: str | None, served: str | None) -> bool:
    """Whether `served` is what `requested` asks for. ChatGPT routes an auto slug to a variant of it and records
    the variant: gpt-6 is served as gpt-6-instant or gpt-6-thinking, gpt-5-4-auto-thinking as gpt-5-4-thinking.
    Anything else (gpt-6-mini for gpt-6, gpt-5-6-thinking for gpt-6-thinking) is a fallback.
    ponytail: a naming heuristic; read routing from the models list if ChatGPT renames its variants."""
    if not requested or not served:
        return True
    want, got = _base(requested), _base(served)
    return got == want or got in (f"{want}-instant", f"{want}-thinking")


def chats(payload: Any) -> list[dict]:
    """GET /backend-api/conversations -> [{id, title, updated, created, pinned, url}]."""
    rows = []
    for item in (payload or {}).get("items") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        rows.append({"id": item["id"], "title": item.get("title"), "updated": iso_time(item.get("update_time")),
                     "created": iso_time(item.get("create_time")), "pinned": bool(item.get("pinned_time")),
                     "url": chat_url(item["id"])})
    return rows


def branch(record: Any) -> list[dict]:
    """The messages on the record's current branch, root first (nodes without a message are skipped)."""
    mapping = (record or {}).get("mapping") or {}
    node, seen, path = (record or {}).get("current_node"), set(), []
    while node and node in mapping and node not in seen:
        seen.add(node)
        if message := (mapping[node] or {}).get("message"):
            path.append(message)
        node = (mapping[node] or {}).get("parent")
    return path[::-1]


def _role(message: dict) -> str | None:
    return ((message or {}).get("author") or {}).get("role")


def _meta(message: dict) -> dict:
    return (message or {}).get("metadata") or {}


def text_of(message: dict) -> str:
    """The visible text of a user or assistant message ("" for thoughts, code, tool output, hidden messages)."""
    content = (message or {}).get("content") or {}
    if content.get("content_type") not in TEXT_TYPES or _meta(message).get("is_visually_hidden_from_conversation"):
        return ""
    return "\n\n".join(p for p in content.get("parts") or [] if isinstance(p, str) and p.strip()).strip()


def image_parts(message: dict) -> list[dict]:
    """{fileId, pointer, width, height, bytes, mime} for each image in a message, in order."""
    out = []
    for part in ((message or {}).get("content") or {}).get("parts") or []:
        if isinstance(part, dict) and part.get("content_type") == IMAGE_PART and part.get("asset_pointer"):
            pointer = part["asset_pointer"]
            out.append({"fileId": pointer.split("://", 1)[-1], "pointer": pointer, "width": part.get("width"),
                        "height": part.get("height"), "bytes": part.get("size_bytes"), "mime": part.get("mime_type")})
    return out


def _citations(messages: list[dict]) -> list[dict]:
    """[{n, title, url}] for the web sources the reply's metadata lists (content_references / citations)."""
    out, seen = [], set()
    for message in messages:
        meta = _meta(message)
        refs = list(meta.get("content_references") or []) + list(meta.get("citations") or [])
        for ref in refs:
            items = ref.get("items") if isinstance(ref, dict) and isinstance(ref.get("items"), list) else [ref]
            for item in items:
                item = (item or {}).get("metadata") or item if isinstance(item, dict) else {}
                url = item.get("url") if isinstance(item, dict) else None
                if url and url not in seen:
                    seen.add(url)
                    out.append({"n": len(out) + 1, "title": item.get("title"), "url": url})
    return out


def reply_turn(record: Any, user_message_id: str | None = None) -> dict | None:
    """The turn that answers a user message (by id, else the branch's last user message):
    {userMessageId, answer, servedModel, requestedSlug, images, citations, statuses, endTurn, defaultModel}.
    None when that user message isn't on the branch.

    servedModel is the last assistant message's metadata.model_slug (else its resolved_model_slug, else
    default_model_slug, else the record's default_model_slug). images are the tool's generated images, each once
    (the same pointer can come back in a later message). statuses lists every message's status after the user's."""
    path = branch(record)
    users = [i for i, m in enumerate(path) if _role(m) == "user" and (user_message_id in (None, m.get("id")))]
    if not users:
        return None
    start = users[-1]
    after = path[start + 1:]
    assistants = [m for m in after if _role(m) == "assistant"]
    served = requested = None
    for message in reversed(assistants):
        meta = _meta(message)
        served = served or meta.get("model_slug") or meta.get("resolved_model_slug") or meta.get("default_model_slug")
        requested = requested or meta.get("requested_model_slug")
    images, seen = [], set()
    for message in after:
        if _role(message) in ("tool", "assistant"):
            for image in image_parts(message):
                if image["pointer"] not in seen:
                    seen.add(image["pointer"])
                    images.append(image)
    answer = "\n\n".join(t for t in (text_of(m) for m in assistants if m.get("recipient", "all") == "all") if t)
    last = after[-1] if after else None
    return {"userMessageId": path[start].get("id"), "answer": answer,
            "servedModel": served or (record or {}).get("default_model_slug"), "requestedSlug": requested,
            "images": images, "citations": _citations(assistants), "statuses": [m.get("status") for m in after],
            "endTurn": bool(last and _role(last) == "assistant" and last.get("end_turn")),
            "defaultModel": (record or {}).get("default_model_slug")}


def messages(record: Any) -> list[dict]:
    """`chats show`: the branch's visible messages, root first: {id, role, text, model, images, created}."""
    out = []
    for message in branch(record):
        role = _role(message)
        text = text_of(message) if role in ("user", "assistant") else ""
        shown = image_parts(message) if role in ("user", "tool", "assistant") else []
        if not text and not shown:
            continue
        out.append({"id": message.get("id"), "role": role, "text": text,
                    "model": _meta(message).get("model_slug") if role == "assistant" else None,
                    "images": [{k: i[k] for k in ("fileId", "width", "height", "bytes", "mime")} for i in shown],
                    "created": iso_time(message.get("create_time"))})
    return out


def stream_error(body: str | None) -> dict | None:
    """The first error a conversation answer reports, as {message, code, clearsIn}: an HTTP error body
    ({"detail": {"message", "code", "clears_in"}} or {"detail": "..."}) or an SSE line `data: {"error": ...}`.
    None when it reports none."""
    for line in (body or "").splitlines() or [""]:
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        detail = data.get("detail")
        error = data.get("error")
        if isinstance(detail, dict):
            return {"message": detail.get("message") or detail.get("code") or str(detail),
                    "code": detail.get("code"), "clearsIn": detail.get("clears_in")}
        if isinstance(detail, str):
            return {"message": detail, "code": None, "clearsIn": None}
        if isinstance(error, dict):
            return {"message": error.get("message") or str(error), "code": error.get("code"),
                    "clearsIn": error.get("clears_in")}
        if isinstance(error, str) and error:
            return {"message": error, "code": data.get("code"), "clearsIn": data.get("clears_in")}
    return None


# ChatGPT's own error text where the answer belongs: "Something went wrong while generating the response.",
# "An error occurred. Either the engine you requested does not exist or there was another issue...",
# "Network error", "Hmm...something seems to have gone wrong."
_ERROR_REPLY = re.compile(
    r"(?:something went wrong(?: while generating the response)?(?:\. if this issue persists.*)?"
    r"|hmm\.*\s*something seems to have gone wrong"
    r"|an error occurred(?:\..*)?|network error|error in message stream|conversation not found)[\s.!]*",
    re.IGNORECASE | re.DOTALL)


def error_reply(answer: str | None) -> bool:
    """Whether the whole answer is ChatGPT's own error message instead of an answer."""
    text = " ".join((answer or "").replace("’", "'").split())
    return bool(text) and bool(_ERROR_REPLY.fullmatch(text))


# ChatGPT's usage-limit notices (English UI), ported from llmcli gemini's Gemini ones: "You've reached our limit of messages
# per hour.", "You've hit the Plus plan limit for image generations requests. You can create more images when the
# limit resets in 23 hours and 52 minutes.", "You've reached the current usage cap for GPT-6, please try again after
# 4:22 PM.", "Too many requests in 1 hour. Try again later." Replies that explain limits stay answers; upsells such
# as "Upgrade to Pro for higher limits" do not count.
_WORDS = r"(?:[\w.'-]+ ){0,6}?"
_YOU_HIT = (r"you(?:'ve| have) (?:reached|hit|exceeded|used up|run out of|maxed out) " + _WORDS
            + r"(?:limits?|quotas?|caps?)\b")
_YOUR_RESETS = r"your " + _WORDS + r"(?:limits?|quotas?|caps?) (?:resets?|will reset|refresh(?:es)?|will refresh)\b"
_LIMIT_NOTICE = re.compile(
    _YOU_HIT + "|" + _YOUR_RESETS
    + r"|\b(?:limits?|quotas?|caps?) (?:has been |have been |was |were )?(?:reached|exceeded|exhausted)\b"
    r"|\b(?:exceeded|exhausted|used up|out of) (?:your |the )?" + _WORDS + r"quotas?\b"
    r"|\btoo many requests\b|\b(?:usage|message|image generation) caps?\b|\brate.limit(?:ed)?\b",
    re.IGNORECASE)
_LIMIT_REPLY = re.compile(
    r"(?:(?:sorry|oops|uh-oh|whoops)[,!.]?\s+)?(?:(?:it )?(?:looks|seems) like )?(?:" + _YOU_HIT + "|" + _YOUR_RESETS + ")",
    re.IGNORECASE)


def _flat(text: str | None) -> str:
    return " ".join((text or "").replace("’", "'").split())


def limit_notice(text: str | None) -> bool:
    """Whether a banner, toast, or dialog's text is a usage-limit notice."""
    flat = _flat(text)
    return bool(_LIMIT_NOTICE.search(flat) or re.search(r"\busage (?:limit|cap)\b|\bquota\b", flat, re.IGNORECASE))


def limit_reply(answer: str | None) -> bool:
    """Whether the answer contains Gemini's usage-limit notice, such as "You've reached your
    limit..." or "Your limit resets...". Explicit quota/usage-limit notices count as well."""
    text = _flat(answer)
    return bool(text) and (bool(_LIMIT_REPLY.match(text)) or bool(re.match(
        r"(?:(?:sorry|oops)[,!.]?\s+)?(?:(?:daily|weekly|monthly)\s+)?(?:usage limit|usage cap|quota|too many requests)\b",
        text, re.IGNORECASE)) or bool(_LIMIT_NOTICE.search(text)))


# Reset timestamps always use the owner's America/Chicago time zone.
RESET_TZ = ZoneInfo("America/Chicago")
_RESET_WORD = re.compile(r"\b(?:resets?|until|after|again|available|back)\b", re.IGNORECASE)
_CLOCK = re.compile(r"\b(\d{1,2})(?::([0-5]\d))?\s?([ap])\.?\s?m\b\.?|\b([01]?\d|2[0-3]):([0-5]\d)\b",
                    re.IGNORECASE)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_MONTH_DAY = re.compile(_MONTH + r"\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4})\b)?", re.IGNORECASE)
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MONTH + r"(?:,?\s+(\d{4})\b)?", re.IGNORECASE)
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?\b")  # US month/day
_WEEKDAY = re.compile(r"\b(mon|tue|wed|thu|fri|sat|sun)(?:day|sday|nesday|rsday|urday|s|rs)?\b\.?", re.IGNORECASE)
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _reset_day(text: str, now: datetime, clock: time) -> date:
    """The day a reset time in `text` falls on: an explicit date, today/tomorrow, a weekday, or else the next
    occurrence of `clock` (today if it's still ahead, otherwise tomorrow)."""
    today = now.date()
    year = None
    if match := _MONTH_DAY.search(text):
        month, day, year = _MONTHS.index(match.group(1).lower()) + 1, int(match.group(2)), match.group(3)
    elif match := _DAY_MONTH.search(text):
        day, month, year = int(match.group(1)), _MONTHS.index(match.group(2).lower()) + 1, match.group(3)
    elif match := _NUMERIC_DATE.search(text):
        month, day, year = int(match.group(1)), int(match.group(2)), match.group(3)
    else:
        if re.search(r"\btomorrow\b", text, re.IGNORECASE):
            return today + timedelta(days=1)
        if re.search(r"\btoday\b|\btonight\b", text, re.IGNORECASE):
            return today
        # Compare instants, including the repeated hour when DST ends.
        ahead = any(datetime.combine(today, clock, tzinfo=RESET_TZ).replace(fold=fold).timestamp()
                    > now.timestamp() for fold in (0, 1))
        if match := _WEEKDAY.search(text):
            days = (_WEEKDAYS.index(match.group(1).lower()) - today.weekday()) % 7
            return today + timedelta(days=days or (0 if ahead else 7))
        return today if ahead else today + timedelta(days=1)
    if year:
        return date(int(year) + (2000 if len(year) == 2 else 0), month, day)
    candidate = date(today.year, month, day)
    return candidate if candidate >= today - timedelta(days=1) else date(today.year + 1, month, day)


_DURATION = re.compile(r"(\d+)\s*(days?|hours?|hrs?|minutes?|mins?|seconds?|secs?)\b", re.IGNORECASE)
_RELATIVE = re.compile(r"\b(?:resets?|again|available|back|clears?|refresh(?:es)?|wait)\s+in\s+((?:\d+\s*(?:days?|hours?|hrs?|minutes?|mins?|seconds?|secs?)\b[\s,]*(?:and\s+)?)+)",
                       re.IGNORECASE)
_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def resets_after(seconds: float | None, now: datetime | None = None) -> str | None:
    """now + seconds (a 429's clears_in, or "in 23 hours"), as ISO 8601 in America/Chicago, to the second."""
    if seconds is None or seconds < 0:
        return None
    now = now or datetime.now(RESET_TZ)
    when = now.astimezone(RESET_TZ) + timedelta(seconds=round(seconds))
    return when.replace(microsecond=0).isoformat()


def resets_at(text: str | None, now: datetime | None = None) -> str | None:
    """When a limit notice says the limit resets, as ISO 8601 in America/Chicago with its offset
    ("Your limit resets at 1:53 PM" -> "2026-10-16T13:53:00-05:00"). None when it names no time or date.
    A relative time ("resets in 23 hours and 52 minutes") is counted from `now`.

    The time after a reset word ("resets", "until", "after"...) wins over an earlier one. Its date is an explicit
    one ("Oct 17", "10/17", "October 17, 2026"), "today"/"tomorrow", a weekday ("Friday"), or else the next time
    the clock shows that time: today if it's still ahead, otherwise tomorrow. A date without a year more than a
    day in the past is next year's. A date after a reset word with no time ("resets Oct 10") means the start of
    that day. The offset is the one in force on that date (CDT -05:00, CST -06:00)."""
    flat = _flat(text)
    now = now or datetime.now(RESET_TZ)
    now = now.replace(tzinfo=RESET_TZ) if now.tzinfo is None else now.astimezone(RESET_TZ)
    if match := _RELATIVE.search(flat):  # "resets in 23 hours and 52 minutes"
        seconds = sum(int(n) * _UNIT_SECONDS[unit[0].lower()] for n, unit in _DURATION.findall(match.group(1)))
        return resets_after(seconds, now)
    word = _RESET_WORD.search(flat)
    for region in ([flat[word.end():]] if word else []) + [flat]:
        if match := _CLOCK.search(region):
            break
    else:  # "Weekly limit resets Oct 10": the start of that day
        after = flat[word.end():] if word else ""
        dated = _MONTH_DAY.search(after) or _DAY_MONTH.search(after) or _NUMERIC_DATE.search(after) \
            or _WEEKDAY.search(after) or re.search(r"\btomorrow\b", after, re.IGNORECASE)
        if not dated:
            return None
        match, region = None, after
    try:
        if match is None:
            hour = minute = 0
        elif match.group(3):
            hour, minute = int(match.group(1)), int(match.group(2) or 0)
            if not 1 <= hour <= 12:
                return None
            hour = hour % 12 + (12 if match.group(3).lower() == "p" else 0)
        else:
            hour, minute = int(match.group(4)), int(match.group(5))
        clock = time(hour, minute)
        when = datetime.combine(_reset_day(flat, now, clock), clock, tzinfo=RESET_TZ)
        # Prefer the next occurrence of an ambiguous fall-back clock time.
        repeated = when.replace(fold=1)
        if when.utcoffset() > repeated.utcoffset() and when.timestamp() <= now.timestamp() < repeated.timestamp():
            when = repeated
    except ValueError:  # "Feb 30": no such date
        return None
    return when.astimezone(timezone.utc).astimezone(RESET_TZ).isoformat()  # a skipped DST hour moves forward
