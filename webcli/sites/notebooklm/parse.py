"""NotebookLM's batchexecute payloads, both ways. Pure: no browser code.

Request builders (`*_params`, `generate_params`) and response readers. The framing is gemini's (")]}'", length
frames, ["wrb.fr", rpcid, "<payload json>", ...] envelopes), so the envelope readers are gemini.parse's. Index paths
come from teng-lin/notebooklm-py's row adapters (MIT; see endpoints.py) and were checked against live answers; a
`[2][0]`-style comment names the path inside the decoded payload. Everything reads softly: a row that doesn't parse
is skipped, a missing leaf is None.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Any

from webcli.sites.gemini.parse import AUTH_CODES, NOT_FOUND_CODES, RpcError, at, envelopes, freq_entries, iso_time
from webcli.sites.notebooklm import endpoints as ep

__all__ = ["AUTH_CODES", "NOT_FOUND_CODES", "RpcError", "rpc_payload", "freq_rpcids"]

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# The request context NotebookLM's own client sends as a read's / create's options block.
CONTEXT = [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]]
ARTIFACT_CONTEXT = [*CONTEXT, [[1, 4, 8, 2, 3, 6]]]


def freq_rpcids(body: str | None) -> list[str]:
    return [entry[0] for entry in freq_entries(body)]


def rpc_payload(body: str, rpcid: str) -> Any:
    """The decoded payload of rpcid's answer (None when Google answered with an empty payload). RpcError when it
    answered with an error code, or not at all."""
    for env in envelopes(body):
        if env[0] == "er":
            raise RpcError(rpcid, at(env, 5), f"NotebookLM rejected {rpcid} (er {at(env, 5)}).")
        if env[0] != "wrb.fr" or at(env, 1) != rpcid:
            continue
        payload = at(env, 2)
        if isinstance(payload, str):
            return json.loads(payload)
        status = at(env, 5)
        code = status[0] if isinstance(status, list) and status and isinstance(status[0], int) else None
        if code is None:
            return None
        raise RpcError(rpcid, code, f"NotebookLM answered {rpcid} with error code {code}.")
    raise RpcError(rpcid, None, f"NotebookLM's answer had no {rpcid} result.")


def is_uuid(value: str) -> bool:
    return bool(_UUID.fullmatch(value or ""))


def clean_id(value: str) -> str:
    """A notebook / source / artifact id; accepts a notebooklm.google.com/notebook/<id> URL too."""
    text = (value or "").strip()
    match = _UUID.search(text.lower()) if "/" in text else None
    return match.group(0) if match else text


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _first_uuid(node: Any, depth: int = 0) -> str | None:
    if isinstance(node, str):
        return node if is_uuid(node) else None
    if isinstance(node, list) and depth < 8:
        for item in node:
            if found := _first_uuid(item, depth + 1):
                return found
    return None


def _texts(node: Any, depth: int = 0) -> list[str]:
    """Every string under node, depth first (a source's text blocks nest their runs)."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, list) and depth < 40:
        return [text for item in node for text in _texts(item, depth + 1)]
    return []


# Request builders.

def notebooks_params() -> list:
    return [None, 1, None, [2]]


def notebook_params(notebook_id: str) -> list:
    return [notebook_id, None, CONTEXT, None, 0]


def source_params(source_id: str) -> list:
    return [[source_id], [2], [2]]


def source_guide_params(source_id: str) -> list:
    return [[[[source_id]]]]


def notes_params(notebook_id: str) -> list:
    return [notebook_id]


def artifacts_params(notebook_id: str) -> list:
    return [[2], notebook_id, 'NOT artifact.status = "ARTIFACT_STATUS_SUGGESTED"']


def ask_params(notebook_id: str, question: str, source_ids: list[str], conversation_id: str | None = None) -> list:
    """GenerateFreeFormStreamed's params: [[[[sid]]...], question, history, options, conversation, _, _, nb, 1].
    No history: the server keeps the history itself. No conversation id: it continues the notebook's current
    conversation; with one, that conversation (the id an earlier answer returned)."""
    return [[[[sid]] for sid in source_ids], question, None, [2, None, [1], [1]], conversation_id or None, None, None,
            notebook_id, 1]


def notebook_by_title(rows: list[dict], title: str) -> list[dict]:
    """The notebooks titled exactly `title` (surrounding spaces ignored); case-insensitive only when no exact match."""
    want = title.strip()
    exact = [r for r in rows if (r["title"] or "").strip() == want]
    return exact or [r for r in rows if (r["title"] or "").strip().casefold() == want.casefold()]


# Responses.

def notebooks(payload: Any) -> list[dict]:
    """wXbhsf [[notebook, ...]] -> rows. A notebook: [title, [source, ...], id, emoji, _, meta]; meta[5][0] last
    viewed, meta[8][0] created (seconds)."""
    rows = []
    for item in at(payload, 0) or []:
        notebook_id = _str(at(item, 2))
        if not notebook_id:
            continue
        sources = at(item, 1)
        rows.append({
            "id": notebook_id,
            "title": (_str(at(item, 0)) or "").replace("thought\n", "").strip(),
            "emoji": _str(at(item, 3)),
            "sources": len(sources) if isinstance(sources, list) else 0,
            "created_at": iso_time(at(item, 5, 8)),
            "viewed_at": iso_time(at(item, 5, 5)),
            "url": ep.NOTEBOOK_URL.format(id=notebook_id),
        })
    return rows


SOURCE_TYPES = {1: "google_docs", 2: "google_slides", 3: "pdf", 4: "pasted_text", 5: "web_page", 8: "markdown",
                9: "youtube", 10: "audio", 11: "docx", 13: "image", 14: "google_sheets", 16: "csv", 17: "epub"}
SOURCE_STATUS = {1: "processing", 2: "ready", 3: "error", 5: "preparing"}


def _source_row(item: Any) -> dict | None:
    """One source: [[id], title, meta, settings, ...]; meta[1] words, meta[2][0] created, meta[4] type, meta[7][0]
    url (else meta[5][0] YouTube), settings[1] status."""
    source_id = _first_uuid(at(item, 0)) or _str(at(item, 0, 0))
    if not source_id:
        return None
    type_code = at(item, 2, 4)
    status = at(item, 3, 1)
    return {
        "id": source_id,
        "title": _str(at(item, 1)),
        "type": SOURCE_TYPES.get(type_code, f"type_{type_code}") if isinstance(type_code, int) else None,
        "status": SOURCE_STATUS.get(status, f"status_{status}") if isinstance(status, int) else None,
        "words": at(item, 2, 1) if isinstance(at(item, 2, 1), int) else None,
        "created_at": iso_time(at(item, 2, 2)),
        "url": _str(at(item, 2, 7, 0)) or _str(at(item, 2, 5, 0)),
    }


def notebook(payload: Any) -> dict:
    """rLM1Ne [notebook] -> {id, title, sources: [...]}."""
    item = at(payload, 0)
    rows = [row for src in (at(item, 1) or []) if (row := _source_row(src))]
    return {"id": _str(at(item, 2)), "title": (_str(at(item, 0)) or "").strip(), "sources": rows}


def source_text(payload: Any) -> dict:
    """hizoJc [descriptor, _, _, [text blocks], ...] -> {id, title, type, text}. The text is every string in the
    text block, joined; blocks are paragraphs."""
    descriptor = at(payload, 0)
    row = _source_row(descriptor) if isinstance(descriptor, list) else None
    blocks = at(payload, 3, 0)
    paragraphs = []
    for block in blocks if isinstance(blocks, list) else []:
        text = "".join(_texts(block)).strip()
        if text:
            paragraphs.append(text)
    return {"id": (row or {}).get("id"), "title": (row or {}).get("title"), "type": (row or {}).get("type"),
            "text": "\n\n".join(paragraphs)}


def source_guide(payload: Any) -> dict:
    """tr032e [[[_, [summary], [[keyword, ...]]]]] -> {summary, keywords}."""
    inner = at(payload, 0, 0)
    keywords = at(inner, 2, 0)
    return {"summary": _str(at(inner, 1, 0)) or "",
            "keywords": [k for k in keywords if isinstance(k, str)] if isinstance(keywords, list) else []}


def notes(payload: Any) -> list[dict]:
    """cFji9 [[row, ...]] -> notes. A row: [id, [id, content, meta, None, title]] (or legacy [id, content]);
    [id, None, 2] is deleted. JSON content ({"name": ..., "children": ...}) is a mind map."""
    rows = []
    container = at(payload, 0)
    for item in container if isinstance(container, list) else []:
        note_id = _str(at(item, 0))
        body = at(item, 1)
        if not note_id or body is None:
            continue
        content = _str(body) if isinstance(body, str) else _str(at(body, 1))
        title = None if isinstance(body, str) else _str(at(body, 4))
        kind = "mind_map" if (content or "").lstrip().startswith("{") else "note"
        rows.append({"id": note_id, "title": title, "kind": kind,
                     "updated_at": None if isinstance(body, str) else iso_time(at(body, 2, 2)),
                     "content": content or ""})
    return rows


def _artifact_kind(item: Any) -> str:
    code = at(item, 2)
    if code == ep.TYPE_APP:
        return {ep.VARIANT_FLASHCARDS: "flashcards", ep.VARIANT_QUIZ: "quiz",
                ep.VARIANT_MIND_MAP: "mind_map"}.get(at(item, 9, 1, 0), "app")
    return {ep.TYPE_AUDIO: "audio", ep.TYPE_REPORT: "report", ep.TYPE_VIDEO: "video", ep.TYPE_MIND_MAP: "mind_map",
            ep.TYPE_INFOGRAPHIC: "infographic", ep.TYPE_SLIDE_DECK: "slide_deck",
            ep.TYPE_DATA_TABLE: "data_table"}.get(code, f"type_{code}")


def _media(items: Any, prefer: str, kind: int | None = None) -> str | None:
    """The best https url in a media list [[url, kind, mime], ...]: mime `prefer` (and `kind`, when given) first,
    then any `prefer`, then any url."""
    ranked: list[tuple[int, str]] = []
    for item in items if isinstance(items, list) else []:
        url = _str(at(item, 0))
        if not url or not url.startswith("https://"):
            continue
        mime_ok = at(item, 2) == prefer
        score = 0 if mime_ok and (kind is None or at(item, 1) == kind) else 1 if mime_ok else 2
        ranked.append((score, url))
    return min(ranked, key=lambda pair: pair[0])[1] if ranked else None


def artifact(item: Any) -> dict | None:
    """One ListArtifacts row: [id, title, type, sources, status, _, audio, report, video, options, modified, ...,
    infographic (14), [created] (15), slide deck (16), _, data table (18)]."""
    artifact_id = _str(at(item, 0))
    if not artifact_id:
        return None
    kind = _artifact_kind(item)
    status = at(item, 4)
    row = {"id": artifact_id, "title": _str(at(item, 1)), "kind": kind,
           "status": ep.STATUS.get(status, f"status_{status}"), "created_at": iso_time(at(item, 15, 0)),
           "url": None, "pdf_url": None, "markdown": None}
    if kind == "audio":
        row["url"] = _media(at(item, 6, 5), "audio/mp4")
    elif kind == "video":
        row["url"] = _media(at(item, 8, 4), "video/mp4", kind=4)  # [8][4]: [[url, kind, mime], ...]
    elif kind == "report":
        body = at(item, 7)
        row["markdown"] = body if isinstance(body, str) else _str(at(body, 0))
    elif kind == "infographic":
        row["url"] = _str(at(item, 14, 2, 0, 1, 0))
        if not row["url"]:
            for block in item:
                url = at(block, 2, 0, 1, 0)
                if isinstance(url, str) and url.startswith("https://"):
                    row["url"] = url
                    break
    elif kind == "slide_deck":
        row["pdf_url"] = _str(at(item, 16, 3))
    return row


def artifacts(payload: Any) -> list[dict]:
    """gArtLc [[row, ...]] (or a bare row list) -> artifacts, newest first."""
    container = at(payload, 0)
    if isinstance(container, list) and container and isinstance(at(container, 0), str):
        container = payload  # the unwrapped shape: payload is the row list
    rows = [row for item in (container if isinstance(container, list) else []) if (row := artifact(item))]
    return sorted(rows, key=lambda r: r["created_at"] or "", reverse=True)


def app_html(payload: Any) -> str | None:
    """v9rmvd [artifact] -> the quiz / flashcards HTML at [0][9][0]."""
    return _str(at(payload, 0, 9, 0))


def app_data(html_text: str | None) -> Any:
    """The JSON a quiz / flashcards app embeds in its HTML as data-app-data="..." (HTML-escaped); None if absent."""
    match = re.search(r'data-app-data="([^"]+)"', html_text or "")
    if not match:
        return None
    try:
        return json.loads(html.unescape(match.group(1)))
    except ValueError:
        return None


def mind_map_tree(payload: Any) -> str | None:
    """v9rmvd [artifact] -> an interactive mind map's {"name", "children"} JSON at [0][9][3]."""
    value = at(payload, 0, 9, 3)
    return value if isinstance(value, str) else (json.dumps(value) if isinstance(value, (dict, list)) else None)


# The chat answer.

@dataclass
class Answer:
    text: str = ""
    citations: list | None = None
    final: bool = False
    conversation_id: str | None = None


def _citations(objects: Any) -> list[dict]:
    """[4][3] citation objects: [[chunk_id], [_, _, score, [[None, start, end]], fragment, source-id data]]."""
    out = []
    for cite in objects if isinstance(objects, list) else []:
        detail = at(cite, 1)
        source_id = _first_uuid(at(detail, 5))
        excerpt = " ".join(t.strip() for t in _texts(at(detail, 4, 0)) if t.strip())
        out.append({"number": len(out) + 1, "source_id": source_id, "excerpt": excerpt[:500] or None})
    return out


def answer(body: str) -> Answer:
    """GenerateFreeFormStreamed's body -> the last (longest) answer and its citations. Each wrb.fr envelope's
    payload: [[text, _, [session, turn, code], _, [doc, _, _, citations, 1 when an answer]], _, _, _, final]."""
    best = Answer()
    for env in envelopes(body):
        if env[0] == "er":
            raise RpcError(None, at(env, 5), f"NotebookLM rejected the question (er {at(env, 5)}).")
        if env[0] != "wrb.fr" or not isinstance(at(env, 2), str):
            continue
        try:
            inner = json.loads(at(env, 2))
        except ValueError:
            continue
        record = at(inner, 0)
        text = _str(at(record, 0))
        if text and len(text) >= len(best.text):
            best = Answer(text=text, citations=_citations(at(record, 4, 3)), final=bool(at(inner, 4)),
                          conversation_id=_str(at(record, 2, 0)) or best.conversation_id)
        elif at(inner, 4):
            best.final = True
    return best


# Studio outputs: what `generate` can create, and the CreateArtifact descriptor for each.

AUDIO_FORMATS = {"deep-dive": 1, "brief": 2, "critique": 3, "debate": 4}
AUDIO_LENGTHS = {"short": 1, "default": 2, "long": 3}
VIDEO_FORMATS = {"explainer": 1, "brief": 2, "cinematic": 3, "short": 4}
VIDEO_STYLES = {"auto": 1, "custom": 0, "classic": 2, "whiteboard": 3, "heritage": 4, "paper-craft": 5,
                "watercolor": 6, "anime": 7, "retro-print": 8, "kawaii": 9}
QUANTITIES = {"fewer": 1, "standard": 2, "more": 3}
DIFFICULTIES = {"easy": 1, "medium": 2, "hard": 3}
ORIENTATIONS = {"landscape": 1, "portrait": 2, "square": 3}
DETAILS = {"concise": 1, "standard": 2, "detailed": 3}
INFOGRAPHIC_STYLES = {"auto": 1, "sketch-note": 2, "professional": 3, "bento-grid": 4, "editorial": 5,
                      "instructional": 6, "bricks": 7, "clay": 8, "anime": 9, "kawaii": 10, "scientific": 11}
SLIDE_FORMATS = {"detailed": 1, "presenter": 2}
SLIDE_LENGTHS = {"default": 1, "short": 2}
REPORTS = {
    "briefing-doc": ("Briefing Doc", "Key insights and important quotes",
                     "Create a comprehensive briefing document that includes an Executive Summary, detailed analysis "
                     "of key themes, important quotes with context, and actionable insights."),
    "study-guide": ("Study Guide", "Short-answer quiz, essay questions, glossary",
                    "Create a comprehensive study guide that includes key concepts, short-answer practice questions, "
                    "essay prompts for deeper exploration, and a glossary of important terms."),
    "blog-post": ("Blog Post", "Insightful takeaways in readable article format",
                  "Write an engaging blog post that presents the key insights in an accessible, reader-friendly "
                  "format. Include an attention-grabbing introduction, well-organized sections, and a compelling "
                  "conclusion with takeaways."),
    "custom": ("Custom Report", "Custom format", "Create a report based on the provided sources."),
}
KINDS = ("audio", "video", "report", "quiz", "flashcards", "mind-map", "infographic", "slide-deck", "data-table")


def generate_params(kind: str, notebook_id: str, source_ids: list[str], opts: dict) -> list:
    """CreateArtifact params [context, notebook_id, descriptor]; descriptor [None, None, type, [[[sid]]...], ...,
    config at the type's slot]. opts: language, instructions, and the kind's choices (keys as in the CLI)."""
    nested = [[[sid]] for sid in source_ids]
    flat = [[sid] for sid in source_ids]
    lang, prompt = opts.get("language") or "en", opts.get("instructions") or None
    if kind == "audio":
        type_code, slot = ep.TYPE_AUDIO, 6
        config: Any = [None, [prompt, AUDIO_LENGTHS[opts.get("length") or "default"], None, flat, lang, None,
                              AUDIO_FORMATS[opts.get("format") or "deep-dive"]]]
    elif kind == "video":
        type_code, slot = ep.TYPE_VIDEO, 8
        fmt = VIDEO_FORMATS[opts.get("format") or "explainer"]
        style = VIDEO_STYLES[opts.get("style") or "auto"]
        video = [flat, lang, prompt, None, fmt]
        if fmt != VIDEO_FORMATS["cinematic"]:
            video.append(None if style == VIDEO_STYLES["custom"] else style)
            if style == VIDEO_STYLES["custom"]:
                video.append(opts.get("style_prompt") or "")
        config = [None, None, video]
    elif kind == "report":
        title, description, directive = REPORTS[opts.get("format") or "briefing-doc"]
        if (opts.get("format") or "briefing-doc") == "custom":
            directive = prompt or directive
        elif prompt:
            directive = f"{directive}\n\n{prompt}"
        type_code, slot = ep.TYPE_REPORT, 7
        config = [None, [title, description, None, flat, lang, directive, None, True]]
    elif kind in ("quiz", "flashcards"):
        type_code, slot = ep.TYPE_APP, 9
        pair = [QUANTITIES[opts.get("quantity") or "standard"], DIFFICULTIES[opts.get("difficulty") or "medium"]]
        if kind == "quiz":
            config = [None, [ep.VARIANT_QUIZ, None, prompt, None, None, None, None, pair]]
        else:
            config = [None, [ep.VARIANT_FLASHCARDS, None, prompt, None, None, None, pair]]
    elif kind == "mind-map":
        type_code, slot = ep.TYPE_APP, 9
        config = [None, [ep.VARIANT_MIND_MAP, None, prompt] if prompt else [ep.VARIANT_MIND_MAP]]
    elif kind == "infographic":
        type_code, slot = ep.TYPE_INFOGRAPHIC, 14
        config = [[prompt, lang, None, ORIENTATIONS[opts.get("orientation") or "landscape"],
                   DETAILS[opts.get("detail") or "standard"], INFOGRAPHIC_STYLES[opts.get("style") or "auto"]]]
    elif kind == "slide-deck":
        type_code, slot = ep.TYPE_SLIDE_DECK, 16
        config = [[prompt, lang, SLIDE_FORMATS[opts.get("format") or "detailed"],
                   SLIDE_LENGTHS[opts.get("length") or "default"]]]
    elif kind == "data-table":
        type_code, slot = ep.TYPE_DATA_TABLE, 18
        config = [None, [prompt, lang]]
    else:
        raise ValueError(f"Unknown output kind {kind!r}; one of: {', '.join(KINDS)}.")
    descriptor: list = [None, None, type_code, nested]
    descriptor.extend([None] * (slot - len(descriptor)))
    descriptor.append(config)
    return [ARTIFACT_CONTEXT, notebook_id, descriptor]


def created(payload: Any) -> dict | None:
    """R7cb6c [artifact row] -> the new artifact (pending)."""
    return artifact(at(payload, 0))


def download_kind(row: dict) -> str:
    """How `download` saves an artifact: media (audio/video/infographic/slide PDF bytes), markdown (reports), json
    (quiz, flashcards, mind maps), or none."""
    if row["kind"] in ("audio", "video", "infographic", "slide_deck"):
        return "media"
    if row["kind"] == "report":
        return "markdown"
    if row["kind"] in ("quiz", "flashcards", "mind_map", "data_table"):
        return "json"
    return "none"


EXTENSIONS = {"audio": ".m4a", "video": ".mp4", "infographic": ".png", "slide_deck": ".pdf", "report": ".md",
              "quiz": ".json", "flashcards": ".json", "mind_map": ".json", "data_table": ".json"}


# Notebook and source creation (`create`, `source add`). Shapes per notebooklm-py's web payload builders (MIT): every
# one ends in CONTEXT, the request-options block NotebookLM's web client sends (the flat `[2], [1]` tail it replaced
# is rejected by migrated accounts).

# Local files `source add --file` uploads, by suffix: the content type the upload start announces.
UPLOAD_TYPES = {".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain", ".pdf": "application/pdf"}
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # webcli's cap (the bytes cross into the page as base64); NotebookLM allows 200 MB
_YOUTUBE = re.compile(r"(?:^|\.)(?:youtube\.com|youtu\.be)$")


def create_notebook_params(title: str) -> list:
    """CCqFvf (CreateProject): [title, None, None, ctx]."""
    return [title, None, None, CONTEXT]


def is_youtube(url: str) -> bool:
    from urllib.parse import urlsplit

    return bool(_YOUTUBE.search((urlsplit(url).hostname or "").lower()))


def url_source_params(notebook_id: str, url: str) -> list:
    """izAoDd for one URL: a web page's URL sits at spec[2], a YouTube video's at spec[7]; spec[10] is always 1."""
    spec: list = [None] * 11
    spec[7 if is_youtube(url) else 2] = [url]
    spec[10] = 1
    return [[spec], notebook_id, CONTEXT]


def text_source_params(notebook_id: str, title: str, text: str) -> list:
    """izAoDd for pasted text: spec[1] [title, text], spec[3] 2 (the web UI's pasted-text code), spec[10] 1."""
    return [[[None, [title, text], None, 2, None, None, None, None, None, None, 1]], notebook_id, CONTEXT]


def register_file_params(notebook_id: str, filename: str) -> list:
    """o4cbdc: registers a file source by name; the bytes follow by resumable upload under the id it answers."""
    return [[[filename]], notebook_id, CONTEXT]


def upload_start_body(notebook_id: str, filename: str, source_id: str) -> str:
    """The resumable upload start request's JSON body."""
    return json.dumps({"PROJECT_ID": notebook_id, "SOURCE_NAME": filename, "SOURCE_ID": source_id})


def created_notebook(payload: Any) -> dict | None:
    """CCqFvf -> {id, title, url}. The answer is the new notebook's row [title, sources, id, ...]; one extra wrapper
    ([[title, sources, id, ...]]) is accepted too. None when there is no id."""
    for row in (payload, at(payload, 0)):
        notebook_id = _str(at(row, 2))
        if notebook_id and is_uuid(notebook_id):
            title = (_str(at(row, 0)) or "").replace("thought\n", "").strip()
            return {"id": notebook_id, "title": title, "url": ep.NOTEBOOK_URL.format(id=notebook_id)}
    return None


def added_source(payload: Any) -> dict | None:
    """izAoDd -> the new source's row ({id, title, type, status, ...}). Google answers it nested once or twice
    ([[[[id], title, meta, settings]]] or [[[id], title, ...]]); the entry is the first list whose [0] is [id]."""
    for row in (at(payload, 0, 0), at(payload, 0), payload):
        if isinstance(row, list) and isinstance(at(row, 0), list) and is_uuid(_str(at(row, 0, 0)) or ""):
            return _source_row(row)
    return None


def registered_source_id(payload: Any, filename: str) -> str | None:
    """o4cbdc -> the registered source's id: the one id in its answer ([[id]], [[[[id]]]], or a row naming it with
    the filename). None when there is no id, or more than one."""
    found: list[str] = []

    def walk(node: Any, depth: int) -> None:
        if depth > 8:
            return
        if isinstance(node, str):
            if is_uuid(node) and node != filename and node not in found:
                found.append(node)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    walk(payload, 0)
    return found[0] if len(found) == 1 else None


def describe_item(item: dict) -> dict:
    """One source item ({kind: url|text|file, ...}, as the CLI builds them) as a plan / result row."""
    if item["kind"] == "url":
        return {"kind": "url", "input": item["url"], "type": "youtube" if is_youtube(item["url"]) else "web_page"}
    if item["kind"] == "text":
        return {"kind": "text", "input": item["title"], "type": "pasted_text", "chars": len(item["text"])}
    return {"kind": "file", "input": item["path"], "type": item["content_type"], "bytes": item["bytes"]}


def write_budget(items: list[dict], *, create: bool = False) -> dict[str, int]:
    """The guard budget for one create / source add: one CreateProject (create), one AddSources per URL or text, and
    per file one register plus two upload POSTs (start, then upload+finalize)."""
    files = sum(1 for item in items if item["kind"] == "file")
    budget = {ep.RPC["add_source"]: len(items) - files, ep.RPC["add_source_file"]: files, ep.UPLOAD: 2 * files}
    if create:
        budget = {ep.RPC["create_notebook"]: 1, **budget}
    return {k: v for k, v in budget.items() if v}


# Per-source rejections. On an add, notebooklm-py (_web/sources/add.py) treats any RPC status other than auth, rate
# limit, or server errors as that one source's failure (SourceAddError: invalid, inaccessible, paywalled, empty, or
# unparseable input), not an API change. These are the gRPC codes NotebookLM answers a refused import with.
SOURCE_REJECT_CODES = {3: "INVALID_ARGUMENT", 5: "NOT_FOUND", 9: "FAILED_PRECONDITION"}


def source_rejected_message(code: int, kind: str) -> str:
    """What to tell the user when NotebookLM refuses one source (kind: url, text, or file)."""
    head = f"NotebookLM couldn't import this source (code {code})."
    if kind == "url":
        return (f"{head} The page may be paywalled or blocking imports. Try uploading a PDF or pasting the text "
                "instead.")
    if kind == "text":
        return f"{head} Check that the text isn't empty or too long, or upload it as a .txt or .md file instead."
    return f"{head} The file may be unsupported, empty, or unreadable. Try another format or paste the text instead."
