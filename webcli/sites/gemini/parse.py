"""Pure parsers for Gemini's wire formats and llmcli gemini's ids. No browser code.

Both internal endpoints answer with the same framing:

    )]}'                      anti-XSSI prefix
    <length>                  frame length (UTF-16 units; not needed here)
    [[envelope], ...]         one frame: a list of envelopes

An envelope is ["wrb.fr", rpcid, "<payload json>", null, null, [status], "generic"]; StreamGenerate
envelopes have rpcid null and send one per update. Other envelopes ("di", "af.httprm", "e") are
bookkeeping. When an RPC fails, the payload is null and [5][0] holds the error code.

Index paths below come from gemini_webapi and Leechael/gemini-web-cli (see endpoints.py); a
`[3][0][0]`-style comment names the path inside the decoded payload.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

XSSI = ")]}'"
_DECODER = json.JSONDecoder()
# Placeholders Gemini leaves where a rendered card/image/video belongs.
_ARTIFACT = re.compile(r"https?://googleusercontent\.com/(?:\w+/)+\d+(?:_\d+)*\n*")  # .../image_generation_content/0_784
_IMAGE_SLOT = re.compile(r"https?://googleusercontent\.com/image_generation_content/\d+(?:_\d+)*")  # an image's place
_CARD = re.compile(r"^https?://googleusercontent\.com/card_content/\d+")
_CHAT_URL = re.compile(r"^https?://gemini\.google\.com/(?:u/\d+/)?app/([^/?#]+)")
_CHAT_ID = re.compile(r"(?:c_)?([A-Za-z0-9]{6,64})")  # usually 16 hex digits; letters are accepted too
CITATIONS_FIELD = 43  # field of a candidate's rich-content block [12] holding [cite: N] sources (VERIFIED 2026-10-18)
GROUNDING_FIELD = 2  # candidate field whose [1] lists the sentences a web search grounded (VERIFIED 2026-10-18)
IMAGES_FIELD = 7  # field of [12] holding [[generated image, ...]] (VERIFIED 2026-10-17)
CANVAS_FIELD = 30  # candidate field holding the Canvas docs the reply wrote (VERIFIED 2026-10-18)

# gRPC status codes Google puts in an envelope's [5][0].
NOT_FOUND_CODES = {5}
AUTH_CODES = {7, 16}  # PERMISSION_DENIED, UNAUTHENTICATED


class RpcError(RuntimeError):
    def __init__(self, rpcid: str | None, code: int | None, message: str) -> None:
        self.rpcid, self.code = rpcid, code
        super().__init__(message)


def clean_chat_id(value: str) -> str:
    """A chat id as it appears in gemini.google.com/app/<id>. Accepts c_<id> and full URLs."""
    text = value.strip()
    if match := _CHAT_URL.match(text):
        text = match.group(1)
    match = _CHAT_ID.fullmatch(text)
    if not match:
        raise ValueError(f"{value!r} is not a Gemini chat id (the id in gemini.google.com/app/<id>).")
    return match.group(1)


def url_chat_id(url: str) -> str | None:
    """The chat id a gemini.google.com/app/<id> page shows; None for /app (a new chat) and other pages."""
    match = _CHAT_URL.match(url or "")
    id_match = _CHAT_ID.fullmatch(match.group(1)) if match else None
    return id_match.group(1) if id_match else None


def rpc_chat_id(chat_id: str) -> str:
    return "c_" + chat_id


def freq_entries(body: str | None) -> list[list]:
    """The [rpcid, payload json, null, tag] entries in a batchexecute form body's f.req.

    [] when there isn't exactly one readable f.req, or when any entry looks odd: the guard then blocks.
    """
    values = [v for k, v in parse_qsl(body or "", keep_blank_values=True) if k == "f.req"]
    if len(values) != 1:
        return []  # two f.req fields could show the guard one call and the server another
    try:
        groups = json.loads(values[0])
    except ValueError:
        return []
    if not isinstance(groups, list):
        return []
    entries = []
    for group in groups:
        for entry in group if isinstance(group, list) else []:
            if not (isinstance(entry, list) and entry and isinstance(entry[0], str)):
                return []
            entries.append(entry)
    return entries


def freq_rpcids(body: str | None) -> list[str]:
    return [entry[0] for entry in freq_entries(body)]


def envelopes(body: str) -> Iterator[list]:
    """Every envelope in a batchexecute or StreamGenerate body, in order. Stops at a cut-off frame."""
    text = body.lstrip()
    if text.startswith(XSSI):
        text = text[len(XSSI):]
    idx = 0
    while True:
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text):
            return
        try:
            value, idx = _DECODER.raw_decode(text, idx)
        except json.JSONDecodeError:
            return
        if isinstance(value, list):  # bare ints are frame lengths
            yield from (e for e in value if isinstance(e, list) and e)


def _code(envelope: list) -> int | None:
    status = at(envelope, 5)
    code = status[0] if isinstance(status, list) and status else None
    return code if isinstance(code, int) else None


def rpc_payload(body: str, rpcid: str) -> Any:
    """The decoded payload of rpcid's answer in a batchexecute body.

    RpcError when Google answered with an error code for it, or didn't answer it at all.
    """
    for env in envelopes(body):
        if env[0] == "er":
            raise RpcError(rpcid, at(env, 5), f"Gemini rejected the {rpcid} request (er {at(env, 5)}).")
        if env[0] != "wrb.fr" or at(env, 1) != rpcid:
            continue
        payload = at(env, 2)
        if isinstance(payload, str):
            return json.loads(payload)
        code = _code(env)
        raise RpcError(rpcid, code, f"Gemini answered {rpcid} with error code {code}.")
    raise RpcError(rpcid, None, f"Gemini's answer had no {rpcid} result.")


def at(node: Any, *path: int) -> Any:
    """node[path[0]][path[1]]..., or None as soon as a step is missing."""
    for i in path:
        if not isinstance(node, list) or not -len(node) <= i < len(node):
            return None
        node = node[i]
    return node


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def iso_time(stamp: Any) -> str | None:
    """[seconds, nanos] (or bare seconds) -> 2026-10-14T17:03:40Z."""
    seconds = stamp[0] if isinstance(stamp, list) and stamp else stamp
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bare_id(cid: Any) -> str | None:
    return cid[2:] if isinstance(cid, str) and cid.startswith("c_") else _str(cid)


def chat_url(chat_id: str) -> str:
    return f"https://gemini.google.com/app/{chat_id}"


def chats(payload: Any) -> tuple[list[dict], str | None]:
    """MaZiqc -> (rows, next cursor). Payload [_, cursor, [[c_id, title, pinned, _, _, [sec, nanos]], ...]]."""
    if payload in (None, []):
        return [], None
    if not isinstance(payload, list):
        raise ValueError(f"Chat list: expected a JSON array, got {type(payload).__name__}.")
    rows = []
    for item in at(payload, 2) or []:
        chat_id = _bare_id(at(item, 0))
        if not chat_id:
            continue
        rows.append({
            "id": chat_id,
            "title": _str(at(item, 1)),
            "updated": iso_time(at(item, 5)),
            "pinned": bool(at(item, 2)),
            "url": chat_url(chat_id),
        })
    return rows, _str(at(payload, 1))


def clean_text(text: str | None, card: Any = None) -> str:
    """Answer text without googleusercontent placeholders; a card-only answer uses its card text."""
    text = text or ""
    if _CARD.match(text) and isinstance(card, str) and card:
        text = card
    return _ARTIFACT.sub("", text).strip()


def _field(block: Any, index: int) -> Any:
    """Field `index` of a JSPB array: positional, or in the trailing sparse dict under str(index + 1)."""
    if not isinstance(block, list) or not block:
        return None
    value = block[index] if index < len(block) else None
    if value in (None, [], {}) or isinstance(value, dict):
        bundle = block[-1] if isinstance(block[-1], dict) else {}
        value = bundle.get(str(index + 1))
    return None if value in (None, [], {}) else value


_CITE = re.compile(r"\[cite:\s*([\d,\s]+)\]")
CHIP = ""  # LAST_RESPONSE_JS leaves this where the page showed a citation chip


def _host(url: str) -> str | None:
    host = urlsplit(url).hostname or ""
    return host.removeprefix("www.") or None


def _web(url: str, title: str | None, label: str | None = None) -> dict:
    """A web source. Its label is the site name a chip shows ("Wikipedia"), else the host ("mapoficeland.is")."""
    return {"label": label or _host(url), "title": title, "url": url}


def _file(name: str) -> dict:
    """An attached file. A chip labels it by its extension ("MD")."""
    return {"label": PurePosixPath(name).suffix[1:].upper() or None, "file": name}


def _cite_sources(candidate: Any) -> dict[int, dict]:
    """[cite: N] number -> source, from field 43 of [12]: groups [[marker, i], [entry, ...], ...] with one entry per
    number in the marker. A web entry has [favicon, url, title] at [3][0]; an attached file has its name at [1][0][2].
    The first entry for a number wins."""
    found: dict[int, dict] = {}
    for group in _field(at(candidate, 12), CITATIONS_FIELD) or []:
        marker, entries = at(group, 0, 0), at(group, 1)
        if not isinstance(marker, str) or not isinstance(entries, list):
            continue
        bracket = _CITE.search(marker)
        numbers = re.findall(r"\d+", bracket.group(1) if bracket else marker)
        for number, entry in zip(numbers, entries):
            url, name = at(entry, 3, 0, 1), _str(at(entry, 1, 0, 2))
            source = _web(url, _str(at(entry, 3, 0, 2))) if isinstance(url, str) else _file(name) if name else None
            if source:
                found.setdefault(int(number), source)
    return found


def cite_refs(text: str, candidate: Any) -> list[tuple[int, int, list[dict]]]:
    """Where `text` (the candidate's answer) cites what, in text order: (start, end, sources).

    A [cite: N, M] marker is replaced (text[start:end]); its numbers resolve through field 43 of [12], and a number
    with no source is dropped. A web-grounded sentence (field 2's [1]: [[sentence, _, _, offsets], [n], [[url, title,
    favicon, snippet, _, _, site name, ...], ...], ...]) has nothing to replace: its refs go right after the sentence,
    or at the end when the sentence isn't found in `text`."""
    numbers = _cite_sources(candidate)
    refs = [(m.start(), m.end(), [numbers[int(n)] for n in re.findall(r"\d+", m.group(1)) if int(n) in numbers])
            for m in _CITE.finditer(text)]
    text = _CITE.sub(lambda m: "\0" * len(m.group(0)), text)  # a sentence is never found inside a marker
    cursor = 0
    for entry in at(candidate, GROUNDING_FIELD, 1) or []:
        sources = [_web(at(s, 0), _str(at(s, 1)), _str(at(s, 6))) for s in at(entry, 2) or []
                   if isinstance(at(s, 0), str)]
        span = _str(at(entry, 0, 0))
        where = text.find(span, cursor) if span else -1
        where = text.find(span) if span and where < 0 else where
        if sources:
            cursor = where + len(span) if where >= 0 else cursor
            refs.append((cursor, cursor, sources) if where >= 0 else (len(text), len(text), sources))
    return sorted(refs, key=lambda ref: ref[:2])


_CHIP_ARIA = re.compile(r"\bfrom (.+?)\.\s*Press Enter", re.S)


def chip_refs(text: str, chips: list[dict]) -> tuple[str, list[tuple[int, int, list[dict]]]]:
    """The page's answer without its CHIP marks, and a ref for each chip from what the chip says, for when the
    transcript has no sources. chips are LAST_RESPONSE_JS's {title, aria} in page order; an aria-label reads "View
    source details for citations from MD: a.md and Wikipedia. Press Enter to open sources dialog.": "MD: a.md" is an
    attached file, "Wikipedia" a web source whose title and URL only the transcript has.
    ponytail: an aria-label split on ", " / " and "; a site name holding one is cut in two."""
    out, refs = "", []
    for i, piece in enumerate(text.split(CHIP)):
        if i:
            chip = chips[i - 1] if i - 1 < len(chips) else {}
            match = _CHIP_ARIA.search(chip.get("aria") or "")
            names = re.split(r",\s+(?:and\s+)?|\s+and\s+", match.group(1)) if match else [chip.get("title") or ""]
            sources = []
            for name in names:
                label, _, title = name.strip().partition(": ")
                if title and title.lower().endswith("." + label.lower()):
                    sources.append({"label": label, "file": title})
                elif label:
                    sources.append({"label": label, "title": title or None, "url": None})
            refs.append((len(out), len(out), sources))
        out += piece
    return out, refs


def _source_key(source: dict) -> str | None:
    """Same key, same footnote. ponytail: a web source the page named only by its label is keyed by that label."""
    return source.get("file") or source.get("url") or source.get("title") or source.get("label")


def _footnote_line(citation: dict) -> str:
    if "file" in citation:
        return citation["file"]
    return ", ".join(v for v in (citation["title"], citation["url"]) if v) or citation["label"] or "source"


_CLOSING = ".,;:!?)]}\"'"
_SPACE_BEFORE_CLOSING = re.compile(r"[ \t]+(?=[.,;:!?)\]}])")


def footnote(text: str, refs: list[tuple[int, int, list[dict]]], *, footnotes: bool = True) -> tuple[str, list[dict]]:
    """`text` with each ref (start, end, sources) replaced by its [^n] marks, then a footnote block
    ("[^1]: story-spine.md", "[^2]: Page Title, https://example.com/page"), and the citations
    [{n, label, file} | {n, label, title, url}]. The same source keeps its number; numbers follow first appearance,
    and a ref's marks are in number order. A mark sits right after the word before it ("quiet [cite: 1, 2]." ->
    "quiet[^1][^2]."). footnotes=False drops
    the marks and the block, leaving no doubled space or space before punctuation; the citations are the same."""
    numbers: dict[str | None, int] = {}
    citations: list[dict] = []
    out, last, lstrip, shown_here = "", 0, False, set()
    for start, end, sources in refs:
        piece = text[last:start]
        out += piece.lstrip(" \t") if lstrip else piece
        last = max(last, end)
        if space := _SPACE_BEFORE_CLOSING.match(text, last):  # "word [cite: 1] ." -> "word[^1]." or "word."
            last = space.end()
        if piece.strip():
            shown_here = set()  # marks already right here aren't repeated: "x [cite: 1] [cite: 1]" -> "x[^1]"
        marks = set()
        for source in sources:
            key = _source_key(source)
            if key not in numbers:
                numbers[key] = len(numbers) + 1
                citations.append({"n": numbers[key], **source})
            marks.add(numbers[key])
        marks, shown_here = marks - shown_here, shown_here | marks
        trimmed = out.rstrip(" \t")
        shown = "".join(f"[^{n}]" for n in sorted(marks)) if footnotes else ""
        if trimmed and not trimmed.endswith("\n"):  # "word [cite: 1]." -> "word[^1]." or "word."
            following = text[last:last + 1]
            word_next = following and not following.isspace() and following not in _CLOSING
            out, lstrip = trimmed + shown + (out[len(trimmed):] if word_next else ""), False
        else:  # at a line's start: "\n[cite: 1] Next" -> "\n[^1] Next" or "\nNext"
            out, lstrip = out + shown, not shown
    tail = text[last:]
    out += tail.lstrip(" \t") if lstrip else tail
    if footnotes and citations:
        out = out.rstrip() + "\n\n" + "\n".join(f"[^{c['n']}]: {_footnote_line(c)}" for c in citations)
    return out, citations


def generated_images(candidate: Any) -> list[dict]:
    """The images a candidate generated: [{url, imageId}], in reply order. An image is
    [[_, _, _, [_, _, _, url]], [image_id], ...]; url is the lh3 /gg/ preview, image_id the
    googleusercontent.com/image_generation_content/N placeholder the answer text carries."""
    images = []
    for image in at(_field(at(candidate, 12), IMAGES_FIELD), 0) or []:
        url, image_id = at(image, 0, 3, 3), at(image, 1, 0)
        if isinstance(url, str) and isinstance(image_id, str):
            images.append({"url": url, "imageId": image_id})
    return images


def canvas(candidate: Any) -> dict | None:
    """The Canvas doc a candidate wrote: {title, markdown}, or None. Field 30 is [[doc_id, _, title, _, markdown,
    _, _, [[sec, nanos], ...], edits, file_name, ...], ...]; [13][0] holds another copy of the markdown.
    ponytail: the first doc only; Gemini writes one per reply so far."""
    for doc in _field(candidate, CANVAS_FIELD) or []:
        if text := _str(at(doc, 4)) or _str(at(doc, 13, 0)):
            return {"title": _str(at(doc, 2)), "markdown": text}
    return None


def reply_turn(payload: Any, response_id: str | None = None) -> dict | None:
    """hNvQHb -> {chatId, responseId, candidateId, answer, refs, images, canvas} for the turn whose r_ id is
    `response_id` (default: the newest turn), from its first candidate, the one the chat shows. answer is the stored
    markdown and refs its cite_refs. None if absent."""
    for turn in at(payload, 0) or []:
        chat_id, rid, rcid = at(turn, 0, 0), at(turn, 0, 1), at(turn, 3, 0, 0, 0)
        if response_id and rid != response_id:
            continue
        if not all(isinstance(v, str) for v in (chat_id, rid, rcid)):
            return None
        candidate = at(turn, 3, 0, 0)
        answer = _answer(candidate)
        return {"chatId": chat_id, "responseId": rid, "candidateId": rcid, "answer": answer,
                "refs": cite_refs(answer, candidate), "images": generated_images(candidate),
                "canvas": canvas(candidate)}
    return None


def full_size_payload(turn: dict, image_id: str) -> list:
    """The c8o8Fe payload for one image of a reply_turn(), as the web app's download button sends it."""
    return [[[None, None, None, [None, None, None, None, None, ""]], [image_id, 0], None, [19, ""],
             None, None, None, None, None, ""],
            [turn["responseId"], turn["candidateId"], turn["chatId"], None, ""], 1, 0, 1]


def _answer(candidate: Any) -> str:
    return clean_text(at(candidate, 1, 0), at(candidate, 22, 0))


def chat_messages(payload: Any) -> tuple[list[dict], str | None]:
    """hNvQHb -> (messages oldest first, cursor for older turns).

    Payload [[turn, ...], cursor] with turns newest first. A turn is
    [[c_id, r_id], _, [[prompt]], [[candidate, ...]], [sec, nanos]] and a candidate is
    [rc_id, [answer], ...]. The first candidate is the one the chat shows. A model message that cites sources has
    footnote marks and a footnote block in its text and lists them ("citations", see footnote). One that made
    images says how many ("images": N); its text is often empty.
    """
    if payload in (None, []):
        return [], None
    if not isinstance(payload, list):
        raise ValueError(f"Chat transcript: expected a JSON array, got {type(payload).__name__}.")
    messages = []
    for turn in reversed(at(payload, 0) or []):
        when = iso_time(at(turn, 4))
        prompt = _str(at(turn, 2, 0, 0))
        candidate = at(turn, 3, 0, 0)
        if prompt:
            messages.append({"role": "user", "text": prompt, "at": when})
        if isinstance(candidate, list):
            text = _answer(candidate)
            text, cites = footnote(text, cite_refs(text, candidate))
            message = {"role": "model", "text": text, "at": when}
            if cites:
                message["citations"] = cites
            if made := generated_images(candidate):
                message["images"] = len(made)
            messages.append(message)
    return messages, _str(at(payload, 1))


def generate(body: str) -> dict:
    """The final state of a StreamGenerate answer: {chatId, answer, refs, images, done, error}. refs: cite_refs.

    Each update carries the whole answer so far, so the last candidate seen wins. images counts the
    candidate's generated images, or else the distinct image placeholders in its raw text. done is true
    once a candidate reports status 2 ([8][0]) or the closing context string ([25]) arrives.
    error is Google's numeric error code ([5][2][0][1][0]), e.g. 1037 usage limit.
    """
    out: dict = {"chatId": None, "answer": None, "refs": [], "images": 0, "done": False, "error": None}
    for env in envelopes(body):
        if env[0] == "er":  # the whole call was rejected
            out["error"] = out["error"] or at(env, 5) or "er"
            continue
        if env[0] != "wrb.fr":
            continue
        if code := at(env, 5, 2, 0, 1, 0):
            out["error"] = code
        if not isinstance(at(env, 2), str):
            continue
        try:
            part = json.loads(env[2])
        except ValueError:
            continue
        if chat_id := _bare_id(at(part, 1, 0)):
            out["chatId"] = chat_id
        if isinstance(at(part, 25), str):
            out["done"] = True
        candidate = at(part, 4, 0)
        if isinstance(candidate, list):
            out["answer"] = _answer(candidate)
            out["refs"] = cite_refs(out["answer"], candidate)
            slots = set(_IMAGE_SLOT.findall(_str(at(candidate, 1, 0)) or ""))
            out["images"] = len(generated_images(candidate)) or len(slots)
            out["done"] = out["done"] or at(candidate, 8, 0) == 2
    return out


# Gemini's own error text, shown where the answer belongs (English UI). Seen 2026: "I encountered an error
# doing what you asked. Could you try again?", "I seem to be encountering an error. Can I try something else
# for you?", "Something went wrong (13)", "An error occurred – please try again (1076)".
_ERROR_REPLY = re.compile(
    r"(?:right now:\s*)?(?:(?:sorry|i'm sorry|i am sorry|oops|uh-oh)[,!.]?\s+)?"
    r"(?:something went wrong(?: on my end)?(?:,? and (?:the|an?|your) (?:ai )?(?:content|response|answer|reply|image)"
    r" (?:wasn't|was not|couldn't be|could not be) (?:generated|created|completed))?"
    r"|i (?:encountered|ran into|hit) an? (?:unexpected |internal )?error(?: doing what you asked| on my end)?"
    r"|i seem to be (?:encountering|having) an? (?:unexpected |internal )?error"
    r"|i'm having a hard time fulfilling your request"
    r"|an? (?:unexpected |internal )?error (?:has )?occurred"
    r"|(?:i )?(?:couldn't|could not|can't|cannot) connect to (?:the )?server)"
    r"(?:[\s.!?,;:()–—-]+"  # then only retry advice and error codes, e.g. ". Please try again later. (13)"
    r"(?:(?:please |could you |can you |you can |you could )?(?:try|retry)"
    r"(?: (?:your|the|that) (?:request|prompt|question))?(?: again)?"
    r"(?: (?:later|shortly|in a (?:few minutes|moment|bit|little while)))?"
    r"|can i help you with something else(?: instead)?"
    r"|can i (?:try|do|help (?:you )?with) something else(?: for you)?"
    r"|(?:please )?(?:refresh|reload) (?:the )?page"
    r"|\d{1,4}))*[\s.!?)]*",
    re.IGNORECASE)


def error_reply(answer: str | None) -> bool:
    """Whether the whole answer is Gemini's own error message ("Sorry, something went wrong. Please try
    again.") instead of an answer. Refusals ("I can't help with that") are answers. An answer that mentions
    an error ("Something went wrong with your regex: ...") doesn't match, but one that is exactly such a
    sentence (say, a translation of one) does."""
    text = " ".join((answer or "").replace("’", "'").split())
    return bool(text) and bool(_ERROR_REPLY.fullmatch(text))


# Gemini's usage-limit notices (English UI): "You've reached your limit for 3 Pro.", "Your limit resets at
# 1:53 PM.", "Usage limit reached", "Quota exceeded". A notice in a banner, toast, or dialog may use any form;
# Replies that explain limits stay answers; explicit limit notices and short quota headings count.
# Upsells such as "Upgrade to Google AI Pro for higher limits" do not count.
_WORDS = r"(?:[\w.'-]+ ){0,6}?"
_YOU_HIT = (r"you(?:'ve| have) (?:reached|hit|exceeded|used up|run out of|maxed out) " + _WORDS
            + r"(?:limits?|quotas?)\b")
_YOUR_RESETS = r"your " + _WORDS + r"(?:limits?|quotas?) (?:resets?|will reset|refresh(?:es)?|will refresh)\b"
_LIMIT_NOTICE = re.compile(
    _YOU_HIT + "|" + _YOUR_RESETS
    + r"|\b(?:limits?|quotas?) (?:has been |have been |was |were )?(?:reached|exceeded|exhausted)\b"
    r"|\b(?:exceeded|exhausted|used up|out of) (?:your |the )?" + _WORDS + r"quotas?\b",
    re.IGNORECASE)
_LIMIT_REPLY = re.compile(
    r"(?:(?:sorry|oops|uh-oh|whoops)[,!.]?\s+)?(?:(?:it )?(?:looks|seems) like )?(?:" + _YOU_HIT + "|" + _YOUR_RESETS + ")",
    re.IGNORECASE)


def _flat(text: str | None) -> str:
    return " ".join((text or "").replace("’", "'").split())


def limit_notice(text: str | None) -> bool:
    """Whether a banner, toast, or dialog's text is a usage-limit notice."""
    flat = _flat(text)
    return bool(_LIMIT_NOTICE.search(flat) or re.search(r"\busage limit\b|\bquota\b", flat, re.IGNORECASE))


def limit_reply(answer: str | None) -> bool:
    """Whether the answer contains Gemini's usage-limit notice, such as "You've reached your
    limit..." or "Your limit resets...". Explicit quota/usage-limit notices count as well."""
    text = _flat(answer)
    return bool(text) and (bool(_LIMIT_REPLY.match(text)) or bool(re.match(
        r"(?:(?:sorry|oops)[,!.]?\s+)?(?:(?:daily|weekly|monthly)\s+)?(?:usage limit|quota)\b",
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


def resets_at(text: str | None, now: datetime | None = None) -> str | None:
    """When a limit notice says the limit resets, as ISO 8601 in America/Chicago with its offset
    ("Your limit resets at 1:53 PM" -> "2026-10-16T13:53:00-05:00"). None when it names no time or date.

    The time after a reset word ("resets", "until", "after"...) wins over an earlier one. Its date is an explicit
    one ("Oct 17", "10/17", "October 17, 2026"), "today"/"tomorrow", a weekday ("Friday"), or else the next time
    the clock shows that time: today if it's still ahead, otherwise tomorrow. A date without a year more than a
    day in the past is next year's. A date after a reset word with no time ("resets Oct 10") means the start of
    that day. The offset is the one in force on that date (CDT -05:00, CST -06:00)."""
    flat = _flat(text)
    now = now or datetime.now(RESET_TZ)
    now = now.replace(tzinfo=RESET_TZ) if now.tzinfo is None else now.astimezone(RESET_TZ)
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
