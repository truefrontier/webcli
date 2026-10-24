"""Browser-side work for webcli notebooklm. Reads, the chat (`ask`), one Studio create (`generate --confirm`), and
notebook / source creation (`create --confirm`, `source add --confirm`).

Every call is the app's own RPC, made by fetch() inside the signed-in notebooklm.google.com page (BATCH_JS,
QUERY_JS): the `at` token, build label, and session id are read from WIZ_global_data there and never reach Python,
which gets back only the response body. Each command's page runs under one guard policy (guard.READ / ASK /
GENERATE, or a budgeted guard.write_policy for create / source add). Artifact files are fetched with the profile's cookies (page.request), every hop checked
(guard.media_url_allowed), and written 0600 where the command asked.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from webcli.core import browser
from webcli.core.errors import AuthRequired, NotFound, RateLimited, UsageError
from webcli.sites import google
from webcli.sites.notebooklm import parse
from webcli.sites.notebooklm.endpoints import APP_HOSTS, APP_URL, BATCH_PATH, NOTEBOOK_URL, QUERY_PATH, RPC, UPLOAD, \
    UPLOAD_PATH  # noqa: F401
from webcli.sites.notebooklm.guard import ASK, GENERATE, POLICY, READ, media_url_allowed, write_policy  # noqa: F401

if TYPE_CHECKING:
    from playwright.async_api import Page

SNIFF_HINT = ("NotebookLM's API may have changed; compare with a capture (`python -m webcli.core.capture notebooklm`) "
              "and fix webcli/sites/notebooklm/endpoints.py or parse.py.")
NOTEBOOKS_HINT = "run: webcli notebooklm notebooks"
ARTIFACTS_HINT = "run: webcli notebooklm artifacts NOTEBOOK"

# One RPC, in the page. path: batchexecute or the chat endpoint. Returns {status, contentType, body}; status 0 means
# no token (signed out) or a redirect (manual: never followed).
_CALL = """
async ({path, freq, rpcid, sourcePath, timeoutMs}) => {
  const wiz = window.WIZ_global_data || {};
  if (!wiz.SNlM0e) return {status: 0, contentType: '', body: ''};
  const params = new URLSearchParams({hl: document.documentElement.lang || 'en',
    _reqid: String(100000 + Math.floor(Math.random() * 900000)), rt: 'c'});
  if (rpcid) { params.set('rpcids', rpcid); params.set('source-path', sourcePath); }
  if (wiz.cfb2h) params.set('bl', wiz.cfb2h);
  if (wiz.FdrFJe) params.set('f.sid', wiz.FdrFJe);
  const body = new URLSearchParams({'f.req': freq, at: wiz.SNlM0e});
  try {
    const res = await fetch(path + '?' + params, {method: 'POST', credentials: 'include', redirect: 'manual',
      signal: AbortSignal.timeout(timeoutMs),
      headers: {'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8', 'X-Same-Domain': '1'}, body});
    return {status: res.type === 'opaqueredirect' ? 0 : res.status,
            contentType: res.headers.get('content-type') || '', body: await res.text()};
  } catch (e) {
    return {status: -1, contentType: '', body: String(e)};
  }
}
"""
# A file source's bytes: the resumable upload start, then "upload, finalize" to the URL its x-goog-upload-url header
# names, both on the page's own host (so its cookies go along and the guard sees both POSTs). The upload URL carries
# an upload_id and never leaves the page: only the two statuses come back. A URL that is not this host's upload path
# with exactly one upload_id is refused before any bytes are sent.
_UPLOAD = """
async ({uploadPath, authuser, startBody, contentType, b64, timeoutMs}) => {
  const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const common = {method: 'POST', credentials: 'include', redirect: 'manual'};
  let start;
  try {
    start = await fetch(uploadPath + '?' + new URLSearchParams({authuser}), {...common,
      signal: AbortSignal.timeout(timeoutMs),
      headers: {'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8', 'x-goog-authuser': authuser,
        'x-goog-upload-command': 'start', 'x-goog-upload-header-content-length': String(bytes.length),
        'x-goog-upload-header-content-type': contentType, 'x-goog-upload-protocol': 'resumable'},
      body: startBody});
  } catch (e) {
    return {start: -1, upload: null, trusted: null, error: String(e).slice(0, 200)};
  }
  if (start.type === 'opaqueredirect' || !start.ok) return {start: start.type === 'opaqueredirect' ? 0 : start.status,
    upload: null, trusted: null};
  let target;
  try { target = new URL(start.headers.get('x-goog-upload-url') || '', location.origin); } catch (e) { target = null; }
  const trusted = !!target && target.origin === location.origin && target.pathname === uploadPath
    && target.searchParams.getAll('upload_id').filter(Boolean).length === 1;
  if (!trusted) return {start: start.status, upload: null, trusted: false};
  try {
    const up = await fetch(target.href, {...common, signal: AbortSignal.timeout(timeoutMs),
      headers: {'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8', 'x-goog-authuser': authuser,
        'x-goog-upload-command': 'upload, finalize', 'x-goog-upload-offset': '0'},
      body: bytes});
    return {start: start.status, upload: up.type === 'opaqueredirect' ? 0 : up.status, trusted: true,
            final: up.headers.get('x-goog-upload-status')};
  } catch (e) {
    return {start: start.status, upload: -1, trusted: true, error: String(e).slice(0, 200)};
  }
}
"""
UPLOAD_TIMEOUT_MS = 300_000
HAS_TOKEN_JS = "() => !!(window.WIZ_global_data && window.WIZ_global_data.SNlM0e)"
RPC_TIMEOUT_MS = 60_000
ASK_TIMEOUT_MS = 180_000


async def open_app(page: "Page") -> None:
    """Load NotebookLM's home. AuthRequired when Google sends the profile to sign-in or a signed-out page (or the
    page has no `at` token); either app host (notebooklm. / notebook.google.com) is fine."""
    await page.goto(APP_URL, wait_until="domcontentloaded", timeout=60_000)
    landed = urlsplit(page.url).hostname or ""
    if landed in google.SIGNED_OUT_HOSTS:
        raise AuthRequired(f"NotebookLM sent Chrome to {landed}: the Google session is missing or expired.")
    if landed not in APP_HOSTS:
        raise RuntimeError(f"NotebookLM redirected to {landed}.")
    try:
        await page.wait_for_function(HAS_TOKEN_JS, timeout=20_000)
    except Exception:
        raise AuthRequired("NotebookLM's page has no session token: the Google session is missing or "
                           "expired.") from None


def _origin(page: "Page") -> str:
    parts = urlsplit(page.url)
    return f"https://{parts.hostname}"


async def _post(page: "Page", path: str, freq: str, *, rpcid: str | None, source: str, timeout_ms: int,
                what: str) -> str:
    res = await page.evaluate(_CALL, {"path": path, "freq": freq, "rpcid": rpcid, "sourcePath": source,
                                      "timeoutMs": timeout_ms})
    status = res["status"]
    if status in (0, 401, 403):
        raise AuthRequired(f"NotebookLM refused {what} (HTTP {status}); the session is missing or expired.")
    if status == 429:
        raise RateLimited(f"NotebookLM refused {what}: rate limited (HTTP 429).", sent=False)
    if status == -1:
        raise RuntimeError(f"{what} failed in the page ({res['body'][:200]}).{google.blocked()}")
    if not 200 <= status < 300:
        raise RuntimeError(f"{what} failed: HTTP {status}.{google.blocked()} {SNIFF_HINT}")
    return res["body"]


async def rpc(page: "Page", name: str, payload: list, *, notebook_id: str | None = None,
              item: str | None = None) -> Any:
    """batchexecute RPC[name](payload) -> the decoded payload. NotFound (when `item` names what was asked for) on
    Google's not-found codes, AuthRequired on its auth codes."""
    rpcid = RPC[name]
    freq = json.dumps([[[rpcid, json.dumps(payload, separators=(",", ":")), None, "generic"]]], separators=(",", ":"))
    source = f"/notebook/{notebook_id}" if notebook_id else "/"
    body = await _post(page, BATCH_PATH, freq, rpcid=rpcid, source=source, timeout_ms=RPC_TIMEOUT_MS, what=rpcid)
    try:
        return parse.rpc_payload(body, rpcid)
    except parse.RpcError as exc:
        if item and exc.code in parse.NOT_FOUND_CODES | {3, 7}:
            raise NotFound(f"{item} not found (or not shared with this account).", hint=NOTEBOOKS_HINT) from None
        if exc.code in parse.AUTH_CODES:
            raise AuthRequired(f"NotebookLM refused {rpcid} (code {exc.code}); the session is missing or "
                               "expired.") from None
        if exc.code == 8:
            raise RateLimited(f"NotebookLM refused {rpcid}: a usage limit (code 8, resource exhausted).",
                              sent=False) from None
        raise RuntimeError(f"{exc} {SNIFF_HINT}") from None
    except ValueError:
        raise RuntimeError(f"{rpcid} answered something that isn't batchexecute JSON. {SNIFF_HINT}") from None


async def _notebook(page: "Page", notebook_id: str) -> dict:
    nb = parse.notebook(await rpc(page, "notebook", parse.notebook_params(notebook_id), notebook_id=notebook_id,
                                  item=f"Notebook {notebook_id}"))
    if not nb["id"]:
        raise NotFound(f"Notebook {notebook_id} not found (or not shared with this account).", hint=NOTEBOOKS_HINT)
    return nb


def _pick_sources(nb: dict, wanted: tuple[str, ...] | list[str] | None) -> list[str]:
    ids = [s["id"] for s in nb["sources"]]
    if not wanted:
        if not ids:
            raise UsageError(f"Notebook {nb['id']} has no sources.")
        return ids
    missing = [s for s in wanted if s not in ids]
    if missing:
        raise NotFound(f"Notebook {nb['id']} has no source {', '.join(missing)}.",
                       hint=f"run: webcli notebooklm sources {nb['id']}")
    return list(wanted)


async def _artifacts(page: "Page", notebook_id: str) -> list[dict]:
    payload = await rpc(page, "artifacts", parse.artifacts_params(notebook_id), notebook_id=notebook_id,
                        item=f"Notebook {notebook_id}")
    return [{**row, "notebook_id": notebook_id} for row in parse.artifacts(payload)]


async def _find_artifact(page: "Page", artifact_id: str, notebook_id: str | None) -> dict:
    """The artifact row; without notebook_id, every notebook is searched (one ListArtifacts each)."""
    if notebook_id:
        candidates = [notebook_id]
    else:
        candidates = [nb["id"] for nb in parse.notebooks(await rpc(page, "notebooks", parse.notebooks_params()))]
    for nb_id in candidates:
        for row in await _artifacts(page, nb_id):
            if row["id"] == artifact_id:
                return row
    raise NotFound(f"No artifact {artifact_id}" + (f" in notebook {notebook_id}." if notebook_id else "."),
                   hint=ARTIFACTS_HINT)


# Commands (each takes headed= for the daemon contract; data sites always run headless).

async def status(*, headed: bool = False) -> dict:
    """{signedIn, email, api}: signed in when NotebookLM loads with a session token; api "ok" when the notebook list
    answers."""
    async with browser.site_page(READ) as page:
        try:
            await open_app(page)
        except AuthRequired:
            return {"signedIn": False, "email": None, "api": None}
        try:
            email = await page.evaluate(google.EMAIL_JS)
        except Exception:
            email = None
        try:
            await rpc(page, "notebooks", parse.notebooks_params())
            api = "ok"
        except AuthRequired:
            return {"signedIn": False, "email": email, "api": None}
        except Exception as exc:
            api = str(exc)
        return {"signedIn": True, "email": email, "api": api}


async def notebooks(*, headed: bool = False) -> list[dict]:
    async with browser.site_page(READ) as page:
        await open_app(page)
        return parse.notebooks(await rpc(page, "notebooks", parse.notebooks_params()))


async def sources(notebook_id: str, *, headed: bool = False) -> dict:
    async with browser.site_page(READ) as page:
        await open_app(page)
        return await _notebook(page, notebook_id)


async def source(source_id: str, notebook_id: str | None = None, *, guide: bool = False,
                 headed: bool = False) -> dict:
    """{id, title, type, text} or, with guide, {id, summary, keywords}."""
    async with browser.site_page(READ) as page:
        await open_app(page)
        item = f"Source {source_id}"
        if guide:
            data = parse.source_guide(await rpc(page, "source_guide", parse.source_guide_params(source_id),
                                                notebook_id=notebook_id, item=item))
            return {"id": source_id, **data}
        data = parse.source_text(await rpc(page, "source", parse.source_params(source_id), notebook_id=notebook_id,
                                           item=item))
        return {**data, "id": data["id"] or source_id}


async def notes(notebook_id: str, *, headed: bool = False) -> list[dict]:
    async with browser.site_page(READ) as page:
        await open_app(page)
        return parse.notes(await rpc(page, "notes", parse.notes_params(notebook_id), notebook_id=notebook_id,
                                     item=f"Notebook {notebook_id}"))


async def artifacts(notebook_id: str, *, headed: bool = False) -> list[dict]:
    async with browser.site_page(READ) as page:
        await open_app(page)
        return await _artifacts(page, notebook_id)


async def artifact(artifact_id: str, notebook_id: str | None = None, *, headed: bool = False) -> dict:
    async with browser.site_page(READ) as page:
        await open_app(page)
        return await _find_artifact(page, artifact_id, notebook_id)


async def _resolve_notebook(page: "Page", ref: str) -> str:
    """A notebook id (or URL, already cleaned) as is; anything else is an exact notebook title, looked up with one
    ListRecentlyViewedProjects read. No match exits 3; more than one exits 2 naming their ids."""
    if parse.is_uuid(ref):
        return ref
    rows = parse.notebook_by_title(parse.notebooks(await rpc(page, "notebooks", parse.notebooks_params())), ref)
    if not rows:
        raise NotFound(f"No notebook titled {ref!r} (and it is not a notebook id or URL).", hint=NOTEBOOKS_HINT)
    if len(rows) > 1:
        raise UsageError(f"{len(rows)} notebooks are titled {ref!r}: {', '.join(r['id'] for r in rows)}. "
                         "Pass the id instead.")
    return rows[0]["id"]


async def ask(notebook_id: str, question: str, source_ids: list[str] | None = None, *,
              conversation_id: str | None = None, timeout: int | None = None, headed: bool = False) -> dict:
    """{notebook_id, question, answer, citations: [{number, source_id, source_title, excerpt}], conversation_id}.
    notebook_id may also be an exact notebook title. Asks with every source unless source_ids. The question joins
    the notebook's chat history, as in the app: its current conversation, or conversation_id's. timeout: seconds to
    wait for the answer (default ASK_TIMEOUT_MS)."""
    async with browser.site_page(ASK) as page:
        await open_app(page)
        notebook_id = await _resolve_notebook(page, notebook_id)
        nb = await _notebook(page, notebook_id)
        ids = _pick_sources(nb, source_ids)
        params = parse.ask_params(notebook_id, question, ids, conversation_id)
        freq = json.dumps([None, json.dumps(params, separators=(",", ":"))], separators=(",", ":"))
        body = await _post(page, QUERY_PATH, freq, rpcid=None, source=f"/notebook/{notebook_id}",
                           timeout_ms=timeout * 1000 if timeout else ASK_TIMEOUT_MS, what="the question")
    try:
        got = parse.answer(body)
    except parse.RpcError as exc:
        if exc.code == 8:
            raise RateLimited("NotebookLM refused the question: a usage limit (code 8).", sent=True) from None
        raise RuntimeError(f"{exc} {SNIFF_HINT}") from None
    if not got.text:
        raise RuntimeError(f"NotebookLM sent no answer. {SNIFF_HINT}")
    titles = {s["id"]: s["title"] for s in nb["sources"]}
    citations = [{**c, "source_title": titles.get(c["source_id"])} for c in got.citations or []]
    return {"notebook_id": notebook_id, "notebook": nb["title"], "question": question, "answer": got.text,
            "citations": citations, "conversation_id": got.conversation_id}


async def plan(kind: str, notebook_id: str, source_ids: list[str] | None, opts: dict, *,
               headed: bool = False) -> dict:
    """What `generate` would create, checked against the notebook (it exists, the sources are in it). Read-only."""
    async with browser.site_page(READ) as page:
        await open_app(page)
        nb = await _notebook(page, notebook_id)
        ids = _pick_sources(nb, source_ids)
    parse.generate_params(kind, notebook_id, ids, opts)  # validates the choices
    return {"kind": kind, "notebook_id": notebook_id, "notebook": nb["title"], "sources": len(ids),
            "options": {k: v for k, v in opts.items() if v not in (None, "")}}


async def generate(kind: str, notebook_id: str, source_ids: list[str] | None, opts: dict, *, confirm: bool = False,
                   headed: bool = False) -> dict:
    """Create one Studio output (one CreateArtifact) and return it as {id, kind, status: pending, ...}. confirm must
    be True: the CLI's dry run never reaches here."""
    if not confirm:
        raise UsageError("generate needs --confirm.")
    async with browser.site_page(GENERATE) as page:
        await open_app(page)
        nb = await _notebook(page, notebook_id)
        ids = _pick_sources(nb, source_ids)
        payload = await rpc(page, "create_artifact", parse.generate_params(kind, notebook_id, ids, opts),
                            notebook_id=notebook_id, item=f"Notebook {notebook_id}")
    row = parse.created(payload)
    if row is None:
        raise RuntimeError("NotebookLM accepted the request but answered no artifact id; check `webcli notebooklm "
                           f"artifacts {notebook_id}` before generating again. {SNIFF_HINT}")
    return {**row, "notebook_id": notebook_id, "notebook": nb["title"]}


async def download(artifact_id: str, path: str, notebook_id: str | None = None, *, headed: bool = False) -> dict:
    """Save a completed artifact to `path` (a file, or an existing directory: <title><ext> inside it): audio .m4a,
    video .mp4, infographic .png, slide deck .pdf, report .md, quiz / flashcards / mind map / data table .json.
    {id, kind, path, bytes}."""
    async with browser.site_page(READ) as page:
        await open_app(page)
        row = await _find_artifact(page, artifact_id, notebook_id)
        if row["status"] != "completed":
            raise UsageError(f"Artifact {artifact_id} is {row['status']}, not completed yet.")
        how = parse.download_kind(row)
        if how == "media":
            url = row["pdf_url"] if row["kind"] == "slide_deck" else row["url"]
            if not url:
                raise RuntimeError(f"Artifact {artifact_id} has no download link. {SNIFF_HINT}")
            status, _headers, data = await google.fetch_file(page, url, media_url_allowed, what=f"the {row['kind']}")
            if status in (401, 403):
                raise AuthRequired(f"NotebookLM refused the {row['kind']} file (HTTP {status}).")
            if status != 200 or not data:
                raise RuntimeError(f"NotebookLM answered HTTP {status} for the {row['kind']} file.")
        elif how == "markdown":
            data = (row["markdown"] or "").encode()
            if not data:
                raise RuntimeError(f"Report {artifact_id} has no text. {SNIFF_HINT}")
        elif how == "json":
            data = await _app_json(page, row)
        else:
            raise UsageError(f"Artifact {artifact_id} ({row['kind']}) has nothing webcli can download.")
    target = _target(path, row)
    saved = google.save_file(target.parent, target.name, data)
    return {"id": row["id"], "title": row["title"], "kind": row["kind"], "path": str(saved), "bytes": len(data)}


async def _app_json(page: "Page", row: dict) -> bytes:
    """Quiz / flashcards: the questions or cards from the artifact's HTML (its data-app-data JSON). Mind map: its
    {name, children} tree. Data table: the raw row."""
    payload = await rpc(page, "artifact", [row["id"]], notebook_id=row["notebook_id"], item=f"Artifact {row['id']}")
    if row["kind"] == "mind_map":
        tree = parse.mind_map_tree(payload)
        if tree is None:
            raise RuntimeError(f"Mind map {row['id']} has no tree. {SNIFF_HINT}")
        return json.dumps(json.loads(tree), indent=2, ensure_ascii=False).encode()
    if row["kind"] in ("quiz", "flashcards"):
        data = parse.app_data(parse.app_html(payload))
        if data is None:
            raise RuntimeError(f"{row['kind']} {row['id']} has no app data. {SNIFF_HINT}")
        return json.dumps(data, indent=2, ensure_ascii=False).encode()
    return json.dumps(payload, indent=2, ensure_ascii=False).encode()


def _target(path: str, row: dict) -> Path:
    target = Path(path).expanduser()
    if target.is_dir():
        name = google.safe_name(row["title"], row["id"]) + parse.EXTENSIONS.get(row["kind"], "")
        return target / name
    if not target.parent.is_dir():
        raise UsageError(f"{target.parent} is not a directory.")
    return target


# Notebook and source creation.

async def plan_sources(notebook_id: str, items: list[dict], *, headed: bool = False) -> dict:
    """What `source add` would add, checked against the notebook (it exists). Read-only."""
    async with browser.site_page(READ) as page:
        await open_app(page)
        nb = await _notebook(page, notebook_id)
    return {"notebook_id": notebook_id, "notebook": nb["title"], "url": NOTEBOOK_URL.format(id=notebook_id),
            "existing_sources": len(nb["sources"]), "sources": [parse.describe_item(item) for item in items],
            "calls": parse.write_budget(items)}


async def create(title: str, items: list[dict] | None = None, *, confirm: bool = False,
                 headed: bool = False) -> dict:
    """Create a notebook (one CreateProject) and add `items` to it. {notebook_id, title, url, sources: [...]}.
    confirm must be True: the CLI's dry run never reaches here."""
    if not confirm:
        raise UsageError("create needs --confirm.")
    items = items or []
    async with browser.site_page(write_policy(parse.write_budget(items, create=True))) as page:
        await open_app(page)
        payload = await rpc(page, "create_notebook", parse.create_notebook_params(title))
        nb = parse.created_notebook(payload)
        if nb is None:
            raise RuntimeError("NotebookLM accepted the create but answered no notebook id; check `webcli notebooklm "
                               f"notebooks` before creating again. {SNIFF_HINT}")
        rows = await _add_items(page, nb["id"], items) if items else []
    return {"notebook_id": nb["id"], "title": nb["title"] or title, "url": nb["url"], "sources": rows}


async def add_sources(notebook_id: str, items: list[dict], *, confirm: bool = False, headed: bool = False) -> dict:
    """Add `items` (URLs, pasted text, local files) to a notebook, each sent once. {notebook_id, notebook, url,
    sources: [{kind, input, id, title, type, status, error}]}: one row per item, in order; a failed item has id None
    and its error, and the rest still go."""
    if not confirm:
        raise UsageError("source add needs --confirm.")
    if not items:
        raise UsageError("Nothing to add.")
    async with browser.site_page(write_policy(parse.write_budget(items))) as page:
        await open_app(page)
        nb = await _notebook(page, notebook_id)
        rows = await _add_items(page, notebook_id, items)
    return {"notebook_id": notebook_id, "notebook": nb["title"], "url": NOTEBOOK_URL.format(id=notebook_id),
            "sources": rows}


async def _add_items(page: "Page", notebook_id: str, items: list[dict]) -> list[dict]:
    """Each item once, in order; then one GetProject read for the fresh statuses. Sign-in and usage-limit errors stop
    the run (they would stop the rest too); any other error is that item's."""
    rows = []
    for item in items:
        row = parse.describe_item(item)
        try:
            got = await _add_one(page, notebook_id, item)
            row.update({"id": got.get("id"), "title": got.get("title"), "status": got.get("status"), "error": None})
            if got.get("type"):
                row["type"] = got["type"]
        except (AuthRequired, RateLimited):
            raise
        except Exception as exc:
            row.update({"id": None, "title": None, "status": "failed", "error": str(exc)})
        rows.append(row)
    if any(r["id"] for r in rows):
        try:
            fresh = {s["id"]: s for s in (await _notebook(page, notebook_id))["sources"]}
        except Exception:
            fresh = {}
        for row in rows:
            if row["id"] in fresh:
                src = fresh[row["id"]]
                row["status"] = src["status"] or row["status"]
                row["title"] = src["title"] or row["title"]
    return rows


async def _add_one(page: "Page", notebook_id: str, item: dict) -> dict:
    if item["kind"] in ("url", "text"):
        params = (parse.url_source_params(notebook_id, item["url"]) if item["kind"] == "url"
                  else parse.text_source_params(notebook_id, item["title"], item["text"]))
        row = parse.added_source(await rpc(page, "add_source", params, notebook_id=notebook_id))
        if row is None:
            raise RuntimeError("NotebookLM answered no source id; check `webcli notebooklm sources "
                               f"{notebook_id}` before adding it again. {SNIFF_HINT}")
        return row
    return await _upload(page, notebook_id, item)


async def _upload(page: "Page", notebook_id: str, item: dict) -> dict:
    """Register the file (o4cbdc), then upload its bytes in the page (_UPLOAD). {id, title, status}."""
    path = Path(item["path"])
    data = path.read_bytes()
    if not data:
        raise UsageError(f"{path} is empty.")
    if len(data) > parse.MAX_UPLOAD_BYTES:
        raise UsageError(f"{path} is over {parse.MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    name = item.get("name") or path.name
    payload = await rpc(page, "add_source_file", parse.register_file_params(notebook_id, name),
                        notebook_id=notebook_id)
    source_id = parse.registered_source_id(payload, name)
    if not source_id:
        raise RuntimeError(f"NotebookLM registered {name} but answered no single source id; check `webcli notebooklm "
                           f"sources {notebook_id}` before adding it again. {SNIFF_HINT}")
    res = await page.evaluate(_UPLOAD, {"uploadPath": UPLOAD_PATH, "authuser": await _authuser(page),
                                        "startBody": parse.upload_start_body(notebook_id, name, source_id),
                                        "contentType": item["content_type"],
                                        "b64": base64.b64encode(data).decode(), "timeoutMs": UPLOAD_TIMEOUT_MS})
    where = f"source {source_id} was registered but has no content; delete it in NotebookLM before adding {name} again"
    if res.get("start") in (0, 401, 403) or res.get("upload") in (0, 401, 403):
        raise AuthRequired(f"NotebookLM refused the upload of {name}; the session is missing or expired ({where}).")
    if res.get("start") != 200:
        raise RuntimeError(f"Starting the upload of {name} failed (HTTP {res.get('start')}; {where}).{google.blocked()}")
    if res.get("trusted") is False:
        raise RuntimeError(f"NotebookLM's upload URL for {name} was not on its own upload path; nothing was sent "
                           f"({where}). {SNIFF_HINT}")
    if res.get("upload") != 200:
        raise RuntimeError(f"Uploading {name} failed (HTTP {res.get('upload')}; {where}).{google.blocked()}")
    return {"id": source_id, "title": name, "status": "processing"}


async def _authuser(page: "Page") -> str:
    """The upload's authuser: the page URL's, else the signed-in email (what notebooklm-py prefers), else 0."""
    from urllib.parse import parse_qs

    found = parse_qs(urlsplit(page.url).query).get("authuser")
    if found and found[0]:
        return found[0]
    try:
        email = await page.evaluate(google.EMAIL_JS)
    except Exception:
        email = None
    return email.strip() if isinstance(email, str) and email.strip() else "0"
