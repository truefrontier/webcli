"""Browser-side work for webcli recorder: status, the recording list, a transcript, audio, and sync. READ-ONLY.

The list and transcripts are the page's own gRPC-web answers (INTERCEPT): GetRecordingList when the app opens,
GetTranscription when a recording's page opens. Audio is fetched with the profile's cookies (page.request), every
hop checked (guard.audio_url_allowed). Files are written where the command asked, 0600, by whichever process runs
this (the identity daemon, or the command itself).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from webcli.core import browser
from webcli.core.errors import AuthRequired, NotFound
from webcli.core.redact import redact_text
from webcli.sites import google
from webcli.sites.recorder import parse
from webcli.sites.recorder.endpoints import APP_URL, AUDIO_URL, RECORDING_URL, RPC_SECONDS
from webcli.sites.recorder.guard import POLICY, audio_url_allowed, rpc_method

if TYPE_CHECKING:
    from playwright.async_api import Page

SNIFF_HINT = ("Recorder's page may have changed; run `python -m webcli.core.capture recorder` and fix "
              "webcli/sites/recorder/endpoints.py.")
LIST_HINT = "run: webcli recorder list"


async def _answer(page: "Page", method: str, url: str) -> Any:
    """Open `url` and return the JSON of the first `method` answer the page gets (None after RPC_SECONDS)."""
    answered = asyncio.get_running_loop().create_future()

    async def on_response(response) -> None:
        if answered.done() or response.request.method != "POST" or rpc_method(response.url) != method:
            return
        try:
            data = await response.json()
        except Exception:
            return
        if not answered.done():
            answered.set_result(data)

    page.on("response", on_response)
    try:
        await google.open_app(page, url)
        return await asyncio.wait_for(asyncio.shield(answered), RPC_SECONDS)
    except asyncio.TimeoutError:
        return None
    finally:
        page.remove_listener("response", on_response)


async def _recordings(page: "Page") -> list[dict]:
    # ponytail: the first GetRecordingList page only (its cursor is ignored), as google-recorder-cli did; follow the
    # cursor if a library outgrows it.
    data = await _answer(page, "GetRecordingList", APP_URL)
    if data is None:
        raise RuntimeError(f"Recorder sent no recording list within {RPC_SECONDS}s.{google.blocked()} {SNIFF_HINT}")
    return parse.recordings(data)


def _find(rows: list[dict], recording_id: str) -> dict:
    found = parse.find(rows, recording_id)
    if found is None:
        raise NotFound(f"No recording {recording_id}.", hint=LIST_HINT)
    return found


async def _transcript(page: "Page", rec: dict) -> str:
    if not rec["has_transcript"] or not rec["audio_id"]:
        raise NotFound(f"Recording {rec['id']} has no transcript.", hint=LIST_HINT)
    data = await _answer(page, "GetTranscription", RECORDING_URL.format(audio_id=quote(rec["audio_id"], safe="")))
    if data is None:
        raise RuntimeError(f"Recorder sent no transcript for {rec['id']} within {RPC_SECONDS}s.{google.blocked()} "
                           f"{SNIFF_HINT}")
    return parse.transcript(data)


async def _audio(page: "Page", rec: dict) -> bytes:
    if not rec["audio_id"]:
        raise NotFound(f"Recording {rec['id']} has no audio id.", hint=LIST_HINT)
    status, _headers, body = await google.fetch_file(page, AUDIO_URL.format(audio_id=quote(rec["audio_id"], safe="")),
                                                     audio_url_allowed, what="the audio")
    if status in (401, 403):
        raise AuthRequired(f"Recorder refused the audio for {rec['id']} (HTTP {status}).")
    if status != 200 or not body:
        raise RuntimeError(f"Recorder answered HTTP {status} for {rec['id']}'s audio.")
    return body


async def status(*, headed: bool = False) -> dict:
    """{signedIn, email}. headed is accepted for the daemon contract; data sites always run headless."""
    return await google.status(POLICY, APP_URL)


async def recordings(*, headed: bool = False) -> list[dict]:
    """[{id, title, created_at, duration_seconds, has_transcript, audio_id}] in Recorder's order."""
    async with browser.site_page(POLICY) as page:
        return await _recordings(page)


async def transcript(recording_id: str, *, headed: bool = False) -> dict:
    """{id, title, text}: Recorder's on-device transcript, words joined by spaces."""
    async with browser.site_page(POLICY) as page:
        rec = _find(await _recordings(page), recording_id)
        return {"id": rec["id"], "title": rec["title"], "text": await _transcript(page, rec)}


async def download(recording_id: str, out_dir: str, *, headed: bool = False) -> dict:
    """Save a recording's audio as out_dir/<title>.m4a (replacing a file of that name). {id, title, path, bytes}."""
    async with browser.site_page(POLICY) as page:
        rec = _find(await _recordings(page), recording_id)
        body = await _audio(page, rec)
    path = google.save_file(out_dir, f"{google.safe_name(rec['title'], rec['id'])}.m4a", body)
    return {"id": rec["id"], "title": rec["title"], "path": str(path), "bytes": len(body)}


async def sync(out_dir: str, *, headed: bool = False) -> dict:
    """Every recording into out_dir: transcripts/<title>.txt (those that have one) and audio/<title>.m4a, replacing
    files of those names. A title already used in this sync (ignoring case) gets the recording id appended.
    {transcripts, audio, errors}: saved paths, and what failed (the rest still runs)."""
    root = Path(out_dir)
    folders = {kind: root / kind for kind in ("transcripts", "audio")}
    for folder in folders.values():
        folder.mkdir(exist_ok=True)
    result: dict[str, list[str]] = {"transcripts": [], "audio": [], "errors": []}
    names: set[str] = set()
    async with browser.site_page(POLICY) as page:
        for rec in await _recordings(page):
            name = google.safe_name(rec["title"], rec["id"])
            while name.casefold() in names:
                name = google.safe_name(f"{name}-{rec['id']}", rec["id"])
            names.add(name.casefold())
            if rec["has_transcript"]:
                try:
                    text = await _transcript(page, rec)
                    path = google.save_file(folders["transcripts"], f"{name}.txt", text.encode())
                    result["transcripts"].append(str(path))
                except Exception as exc:
                    result["errors"].append(redact_text(f"Transcript {rec['id']}: {exc}"))
            try:
                path = google.save_file(folders["audio"], f"{name}.m4a", await _audio(page, rec))
                result["audio"].append(str(path))
            except Exception as exc:
                result["errors"].append(redact_text(f"Audio {rec['id']}: {exc}"))
    return result
