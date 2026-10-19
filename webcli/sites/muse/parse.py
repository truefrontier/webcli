"""Pure functions: muse.ai URLs, DOM snapshots, and request bodies -> llmcli muse data (musecli's parse.py). No
browser, no network."""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, urlsplit

from webcli.sites.chatgpt.parse import resets_at  # noqa: F401  (the cap notice's reset time, America/Chicago)
from webcli.sites.muse.endpoints import APP_HOSTS, GATE_PREFIXES, MAIN, NOT_THREADS, thread_url

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
THREAD_RE = re.compile(r"/thread/([^/?#]+)")
SIGNED_OUT_FLAGS = ("authenticated", "isAuthenticated", "loggedIn", "logged_in", "valid")
NAME_KEYS = ("name", "full_name", "display_name", "displayName", "username")
# The only /api/auth/check fields fetch.AUTH_CHECK_JS lets out of the page (the body holds a token).
AUTH_KEYS = (*SIGNED_OUT_FLAGS, *NAME_KEYS, "error")
AUTH_NESTED = ("user", "viewer", "account")
# Screen-reader labels muse.ai puts before message text (the user's reads "You:\n\n...").
LABELS = {"user": re.compile(r"(?:You|User message):"), "assistant": re.compile(r"Assistant message:")}


def clean_chat_id(value: str) -> str:
    """A bare thread id or a muse.ai/thread/<id> link. Anything path-like is rejected."""
    value = value.strip()
    if "/" in value:
        match = THREAD_RE.search(urlsplit(value).path)
        value = match.group(1) if match else value
    if not ID_RE.match(value) or value in NOT_THREADS:
        raise ValueError(f"not a Muse chat id: {value!r}")
    return value


def thread_id(url: str) -> str | None:
    """The chat a muse.ai page shows: the <id> in /thread/<id>, or "main" at "/" (the main chat).
    None for /thread/new and other pages."""
    parts = urlsplit(url)
    if (parts.hostname or "muse.ai") not in APP_HOSTS:
        return None
    if parts.path == "/":
        return MAIN
    match = THREAD_RE.match(parts.path)
    return match.group(1) if match and match.group(1) not in NOT_THREADS and ID_RE.match(match.group(1)) else None


def on_chat(url: str, chat_id: str) -> bool:
    """Is this page showing that chat? The main chat shows at "/" as well as /thread/main."""
    return thread_id(url) == chat_id


def gate(path: str) -> str | None:
    """Which hold page the account is on: '/access/waitlist' -> 'waitlist', '/blocked' -> 'blocked'."""
    for prefix in GATE_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            rest = path[len(prefix) + 1:].split("/")[0]
            return rest if prefix == "/access" and rest else prefix.lstrip("/")
    return None


def auth_check(data: object) -> tuple[bool, str | None]:
    """(not signed out, account name) from POST /api/auth/check's JSON. The body shape is unverified:
    only an explicit false flag counts as signed out. Never pass the raw body on."""
    data = data if isinstance(data, dict) else {}
    if any(data.get(flag) is False for flag in SIGNED_OUT_FLAGS) or "error" in data:
        return False, None
    sources = [data] + [data[k] for k in AUTH_NESTED if isinstance(data.get(k), dict)]
    for source in sources:
        for key in NAME_KEYS:
            if isinstance(source.get(key), str) and source[key].strip():
                return True, source[key].strip()
    return True, None


def _oneline(value: object) -> str | None:
    return " ".join(str(value or "").split()) or None


def chat_row(raw: dict, chat_id: str) -> dict:
    """A sidebar row as fetch.ROWS_JS reads it ({title, updated}) and the chat id its click led to
    -> {id, title, updated, url}. updated is the relative time the sidebar shows ("2m", "Tue")."""
    return {"id": chat_id, "title": _oneline(raw.get("title")), "updated": _oneline(raw.get("updated")),
            "url": thread_url(chat_id)}


def citations(links: list[dict]) -> list[dict]:
    """External http(s) links in an answer -> [{title, url}], deduped. muse.ai's own links are dropped."""
    out: dict[str, dict] = {}
    for link in links:
        url = str(link.get("href") or "")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.hostname in APP_HOSTS or url in out:
            continue
        out[url] = {"title": " ".join(str(link.get("text") or "").split()) or None, "url": url}
    return list(out.values())


def clean_text(text: str, role: str | None) -> str:
    """Message text without a leading screen-reader label ("You:", "User message:", "Assistant message:").
    fetch.py already leaves the label elements out; this catches one that slipped through."""
    text = text.strip()
    label = LABELS.get(role or "")
    match = label.match(text) if label else None
    return text[match.end():].strip() if match else text


def message(raw: dict) -> dict | None:
    """{id, role, text, links} from the page -> {id, role, text, citations?}; None when there is no text."""
    role = raw.get("role")
    text = clean_text(str(raw.get("text") or ""), role)
    if not text:
        return None
    out = {"id": raw.get("id") or None, "role": role, "text": text}
    cites = citations(raw.get("links") or [])
    if cites:
        out["citations"] = cites
    return out


def post_op(headers: dict[str, str], body: str | None) -> str | None:
    """The operation a POST runs, for the guard: a GraphQL operation name, else a Next.js
    server-action id, else a GraphQL doc_id, else None.

    Raises ValueError when the body is a batch or names disagree (the header, the form field, or a
    repeated field), so the guard fails closed instead of trusting one label over another.
    """
    headers = {k.lower(): v for k, v in headers.items()}
    names = {headers["x-fb-friendly-name"]} if headers.get("x-fb-friendly-name") else set()
    text = (body or "").strip()
    if text.startswith(("{", "[")):
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("batched or non-object JSON body")
        for key in ("operationName", "fb_api_req_friendly_name"):
            if data.get(key):
                names.add(str(data[key]))
        doc_id = data.get("doc_id") or data.get("docId")
    else:
        pairs = parse_qsl(text, keep_blank_values=True)
        names |= {v for k, v in pairs if k == "fb_api_req_friendly_name" and v}
        doc_ids = {v for k, v in pairs if k == "doc_id" and v}
        if len(doc_ids) > 1:
            raise ValueError("more than one doc_id")
        doc_id = next(iter(doc_ids), None)
    if len(names) > 1:
        raise ValueError(f"operation names disagree: {sorted(names)}")
    if names:
        return names.pop()
    if headers.get("next-action"):
        return "next-action:" + headers["next-action"]
    return f"doc_id:{doc_id}" if doc_id else None


# A usage-cap notice in place of an answer. Muse's own wording is unknown (no capture yet), so this is a
# heuristic kept strict: the whole reply must be short and open with the notice ("You've reached your daily limit",
# "Your message limit resets at 4 PM", "Too many requests"). A longer answer that only mentions limits stays an
# answer.
LIMIT_REPLY_CHARS = 300
_WORDS = r"(?:[\w.'-]+ ){0,6}?"
_LIMIT_REPLY = re.compile(
    r"(?:(?:sorry|oops|uh-oh|whoops)[,!.]?\s+)?(?:(?:it )?(?:looks|seems) like )?(?:"
    r"you(?:'ve| have) (?:reached|hit|exceeded|used up|run out of|maxed out) " + _WORDS + r"(?:limits?|quotas?|caps?)\b"
    r"|your " + _WORDS + r"(?:limits?|quotas?|caps?) (?:resets?|will reset|refresh(?:es)?|will refresh)\b"
    r"|(?:(?:daily|weekly|monthly)\s+)?(?:usage limit|usage cap|message limit|quota|too many requests)\b)",
    re.IGNORECASE)


def limit_reply(answer: str | None) -> bool:
    """Whether the whole (short) reply is a usage-cap notice instead of an answer."""
    text = " ".join((answer or "").replace("’", "'").split())
    return 0 < len(text) <= LIMIT_REPLY_CHARS and bool(_LIMIT_REPLY.match(text))
