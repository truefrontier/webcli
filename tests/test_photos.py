"""webcli photos, offline: the fail-closed guard (batchexecute rpcids, download hops), the parsers, the grid, info,
and download read off a scripted page, and the commands over a faked daemon."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from urllib.parse import urlencode

import pytest
from click.testing import CliRunner

from webcli.apps import webcli as webcli_app
from webcli.core import daemon, guard
from webcli.core.errors import AuthRequired, NotFound
from webcli.sites.photos import fetch, parse
from webcli.sites.photos.endpoints import MUTATING_RPCIDS, READ_RPCIDS
from webcli.sites.photos.guard import download_url_allowed, request_allowed

from google_fakes import APIResponse, ScriptedPage, install

BATCH = "https://photos.google.com/_/PhotosUi/data/batchexecute"
KEY = "AF1QipPhotoKey0123"
BASE = "https://photos.fife.usercontent.google.com/pw/AP1GczMediaBase"


def batch(*rpcids: str, label: str | None = None) -> tuple[str, str, str]:
    body = urlencode({"f.req": json.dumps([[[r, "[]", None, "generic"] for r in rpcids]]), "at": "AJvLN6M-fake:1"})
    return "POST", f"{BATCH}?{urlencode({'rpcids': label or ','.join(rpcids), 'rt': 'c'})}", body


# The guard.

@pytest.mark.parametrize("request_", [
    ("GET", "https://photos.google.com/", None),
    ("GET", "https://photos.google.com/photo/AF1Qip", None),
    ("GET", f"{BASE}=w400-h300", None),
    ("GET", "https://lh3.googleusercontent.com/thumb", None),
    batch("lcxiM"),
    batch("Z5xsfc", "snAcKc"),
    ("POST", "https://accounts.google.com/RotateCookies", None),
])
def test_listed_reads_pass(request_):
    assert request_allowed(*request_)


@pytest.mark.parametrize("request_", [
    *[batch(rpcid) for rpcid in MUTATING_RPCIDS],
    batch("lcxiM", "XwAOJf"),  # a read riding with a trash
    batch("zzNever"),
    batch("XwAOJf", label="lcxiM"),  # the URL's label and f.req disagree
    ("POST", f"{BATCH}?rpcids=lcxiM", urlencode({"f.req": "[]"}) + "&" + urlencode({"f.req": "[]"})),
    ("POST", f"{BATCH}?rpcids=lcxiM", "not a form"),
    ("POST", "https://evil.example/_/PhotosUi/data/batchexecute?rpcids=lcxiM", batch("lcxiM")[2]),
    ("POST", "http://photos.google.com/_/PhotosUi/data/batchexecute?rpcids=lcxiM", batch("lcxiM")[2]),
    ("POST", "https://photos.google.com/_/upload/uploadmedia/interactive", "x"),
    ("POST", "https://play.google.com/log?format=json", "{}"),
    ("PUT", "https://photos.google.com/", None),
    ("GET", "https://www.googletagmanager.com/gtm.js", None),
    ("GET", "https://r2---sn-ojq4f5-co.googlevideo.com/videoplayback", None),
])
def test_writes_unknown_rpcids_and_other_hosts_are_blocked(request_):
    assert not request_allowed(*request_)


def test_reads_and_writes_never_overlap():
    assert not READ_RPCIDS & set(MUTATING_RPCIDS)


@pytest.mark.parametrize(("url", "allowed"), [
    (f"{BASE}=d", True), (f"{BASE}=dv", True), ("https://video-downloads.googleusercontent.com/x", True),
    ("http://photos.fife.usercontent.google.com/pw/x=d", False), ("https://photos.fife.usercontent.google.com:8443/x", False),
    ("https://user@photos.fife.usercontent.google.com/x", False), ("https://evil.example/x", False),
    ("https://photos.google.com/x", False), ("https://googleusercontent.com.evil.example/x", False), ("not a url", False),
])
def test_download_hops(url, allowed):
    assert download_url_allowed(url) is allowed


def test_a_blocked_batch_is_described_by_its_rpcids():
    class Request:
        method, url, post_data = batch("XwAOJf")

    guard.state.begin(fetch.POLICY)
    assert not guard.check(Request(), own_page=True)
    assert guard.state.blocked == ["POST photos.google.com/_/PhotosUi/data/batchexecute rpcids=XwAOJf"]


# The parsers.

def test_ids_from_keys_and_urls():
    assert parse.photo_id(f"https://photos.google.com/photo/{KEY}?x=1") == KEY == parse.photo_id(f" {KEY} ")
    assert parse.album_id("https://photos.google.com/album/AF1QipAlbum/photo/AF1QipPhoto") == "AF1QipAlbum"
    for bad in ("hello", "https://photos.google.com/albums"):
        with pytest.raises(ValueError):
            parse.album_id(bad)
    with pytest.raises(ValueError, match="expected a photo id or URL"):
        parse.photo_id("hello")


def test_labels_titles_and_download_names():
    assert parse.described("Photo - Portrait - Nov 10, 2016, 11:22:34 AM") == {
        "kind": "Photo", "taken_at": "Nov 10, 2016, 11:22:34 AM"}
    assert parse.described("Video - Oct 1") == {"kind": "Video", "taken_at": ""}
    assert parse.clean_title("Summer 2026  42 items More options") == "Summer 2026"
    assert parse.download_name('attachment; filename="IMG_1234.JPG"', "image/jpeg", KEY) == "IMG_1234.JPG"
    assert parse.download_name(None, "video/mp4", KEY) == f"{KEY}.mp4"
    assert parse.download_name("", "image/png", KEY) == f"{KEY}.png"
    assert parse.download_name(None, None, KEY) == f"{KEY}.bin"


# The page.

def tiles(*ids: str) -> list[dict]:
    return [{"id": i, "label": f"Photo - Portrait - Oct {n}, 2026", "url": f"https://photos.google.com/photo/{i}"}
            for n, i in enumerate(ids, 1)]


def test_the_grid_collects_tiles_across_scrolls_until_the_limit(monkeypatch):
    shown = iter([tiles("a", "b"), tiles("b", "c"), tiles("d", "e"), tiles("e")])  # tiles scroll away
    page = install(monkeypatch, ScriptedPage(answers={"querySelectorAll(selector)": lambda _arg: next(shown)}))
    rows = asyncio.run(fetch.photos(limit=4))
    assert [r["id"] for r in rows] == ["a", "b", "c", "d"] and page.scrolls == 2
    assert rows[0] == {"id": "a", "kind": "Photo", "taken_at": "Oct 1, 2026", "label": "Photo - Portrait - Oct 1, 2026",
                       "url": "https://photos.google.com/photo/a"}


def test_the_grid_stops_at_its_end_and_search_and_album_urls_are_quoted(monkeypatch):
    page = install(monkeypatch, ScriptedPage(answers={"querySelectorAll(selector)": tiles("a")}))
    assert [r["id"] for r in asyncio.run(fetch.photos(query="dogs/cats & me", limit=50))] == ["a"]
    assert page.gotos == ["https://photos.google.com/search/dogs%2Fcats%20%26%20me"] and page.scrolls == 3
    asyncio.run(fetch.photos(album="AF1QipAlbum", limit=1))
    assert page.gotos[-1] == "https://photos.google.com/album/AF1QipAlbum"


def test_no_tiles_is_not_found_naming_what_the_guard_blocked(monkeypatch):
    install(monkeypatch, ScriptedPage(answers={"querySelectorAll(selector)": []}))
    guard.state.blocked.append("POST photos.google.com/_/PhotosUi/data/batchexecute rpcids=Qwerty")
    with pytest.raises(NotFound) as caught:
        asyncio.run(fetch.photos(query="dogs"))
    assert "a search for 'dogs'" in str(caught.value) and "rpcids=Qwerty" in caught.value.hint


def test_albums(monkeypatch):
    install(monkeypatch, ScriptedPage(answers={"querySelectorAll(selector)": [
        {"id": "AF1QipA", "label": "Trip  12 items More options", "url": "https://photos.google.com/album/AF1QipA"}]}))
    assert asyncio.run(fetch.albums()) == [{"id": "AF1QipA", "title": "Trip", "url": "https://photos.google.com/album/AF1QipA"}]


def photo_page(**extra):
    url = f"https://photos.google.com/photo/{KEY}"
    shown = {"title": "Photo", "aria": "Video - Landscape - Oct 3, 2026, 9:00:00 AM", "image": True, "url": url}
    return ScriptedPage(answers={"labelledSel": shown}, **extra)


def test_info_reads_the_photo_page_and_a_redirect_is_not_found(monkeypatch):
    install(monkeypatch, photo_page())
    assert asyncio.run(fetch.info(KEY)) == {
        "id": KEY, "kind": "Video", "taken_at": "Oct 3, 2026, 9:00:00 AM",
        "label": "Video - Landscape - Oct 3, 2026, 9:00:00 AM", "url": f"https://photos.google.com/photo/{KEY}",
        "page_url": f"https://photos.google.com/photo/{KEY}"}
    install(monkeypatch, ScriptedPage(answers={"labelledSel": {"title": "Photos", "aria": "", "image": False,
                                                               "url": "https://photos.google.com/"}}))
    with pytest.raises(NotFound, match="No photo AF1QipNope"):
        asyncio.run(fetch.info("AF1QipNope"))


def test_download_saves_the_video_original(monkeypatch, tmp_path):
    page = install(monkeypatch, photo_page(
        requests={f"https://photos.google.com/photo/{KEY}": [f"{BASE}=w800-h600", f"{BASE}=m18"]},
        files={f"{BASE}=dv": APIResponse(200, b"v" * 60_000, {"content-type": "video/mp4",
                                                              "content-disposition": 'attachment; filename="PXL_1.mp4"'})}))
    saved = asyncio.run(fetch.download(KEY, str(tmp_path)))
    assert saved == {"id": KEY, "path": str(tmp_path / "PXL_1.mp4"), "bytes": 60_000, "mime": "video/mp4"}
    assert page.fetched == [f"{BASE}=dv"] and page.keys == ["Space"]
    assert stat.S_IMODE(os.stat(tmp_path / "PXL_1.mp4").st_mode) == 0o600


def test_download_falls_back_to_the_still_original_and_never_trusts_the_name(monkeypatch, tmp_path):
    install(monkeypatch, photo_page(
        requests={f"https://photos.google.com/photo/{KEY}": [f"{BASE}=w800"]},
        files={f"{BASE}=dv": APIResponse(200, b"poster", {"content-type": "image/jpeg"}),
               f"{BASE}=d": APIResponse(200, b"jpeg!", {"content-type": "image/jpeg",
                                                        "content-disposition": 'filename="../../evil.jpg"'})}))
    saved = asyncio.run(fetch.download(KEY, str(tmp_path)))
    assert saved["path"] == str(tmp_path / ".._.._evil.jpg") and (tmp_path / ".._.._evil.jpg").read_bytes() == b"jpeg!"


def test_a_poster_is_never_the_original_and_a_refusal_is_sign_in(monkeypatch, tmp_path):
    request = {f"https://photos.google.com/photo/{KEY}": [f"{BASE}=w800"]}
    install(monkeypatch, photo_page(requests=request, files={
        f"{BASE}=dv": APIResponse(200, b"poster", {"content-type": "image/jpeg"}), f"{BASE}=d": APIResponse(404)}))
    with pytest.raises(RuntimeError, match="answered no original"):
        asyncio.run(fetch.download(KEY, str(tmp_path)))
    install(monkeypatch, photo_page(requests=request, files={f"{BASE}=dv": APIResponse(401)}))
    with pytest.raises(AuthRequired, match="HTTP 401"):
        asyncio.run(fetch.download(KEY, str(tmp_path)))
    assert list(tmp_path.iterdir()) == []


def test_download_without_a_media_url_fails(monkeypatch, tmp_path):
    install(monkeypatch, photo_page())
    with pytest.raises(RuntimeError, match="loaded no media URL"):
        asyncio.run(fetch.download(KEY, str(tmp_path)))
    assert list(tmp_path.iterdir()) == []


# The commands, over a faked daemon.

@pytest.fixture
def calls(monkeypatch):
    seen = []

    async def call(ident, site, op, *args, **kwargs):
        seen.append((op, args, kwargs))
        if op == "download":
            return {"id": args[0], "path": f"{args[1]}/x.jpg", "bytes": 5, "mime": "image/jpeg"}
        if op == "info":
            return {"id": args[0], "kind": "Photo", "taken_at": "Oct 1", "label": "Photo - x - Oct 1",
                    "url": "u", "page_url": "u"}
        return [parse.photo(tile) for tile in tiles("a", "b")]

    monkeypatch.setattr(daemon, "call", call)
    return seen


def run(*args):
    return CliRunner().invoke(webcli_app.build(), ["photos", *args])


def test_commands_pass_clean_ids_and_an_absolute_output_dir(calls, tmp_path, monkeypatch):
    assert run("list", "--album", "https://photos.google.com/album/AF1QipAlbum", "--id-only").stdout == "a\nb\n"
    assert calls[-1] == ("photos", (), {"album": "AF1QipAlbum", "limit": 50, "headed": False})
    assert json.loads(run("search", "dogs", "--compact").stdout)[0] == {"id": "a", "kind": "Photo", "taken_at": "Oct 1, 2026"}
    assert calls[-1] == ("photos", (), {"query": "dogs", "limit": 50, "headed": False})
    assert json.loads(run("info", f"https://photos.google.com/photo/{KEY}", "--compact").stdout) == {
        "id": KEY, "kind": "Photo", "taken_at": "Oct 1", "url": "u"}
    monkeypatch.chdir(tmp_path)
    result = run("download", KEY, "-o", "out")
    assert result.exit_code == 0 and calls[-1][1] == (KEY, str(tmp_path.resolve() / "out"))
    assert (tmp_path / "out").is_dir() and json.loads(result.stdout)["bytes"] == 5
