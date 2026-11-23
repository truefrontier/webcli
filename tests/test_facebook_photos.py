"""Photos of a person, and friend search, offline. Fixtures use made-up names and ids. Nothing reaches Facebook.

The page is a fake: its HTML carries the recorded JSON, and a scroll can deliver the next GraphQL body the real
page would have requested. Image bytes come from that fake too. Face counts are a stand-in for OpenCV, so the
suite does not need the faces extra.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from click.testing import CliRunner

from webcli.apps import webcli as webcli_app
from webcli.core import browser, daemon, guard
from webcli.core.errors import AuthRequired, NotFound, UsageError
from webcli.sites.facebook import endpoints, faces, fetch, parse
from webcli.sites.facebook import photos as photos_io
from webcli.sites.facebook.guard import image_url_allowed, read_policy, request_allowed
from webcli.sites.facebook.photos import DOC_HTML_JS

from conftest import FIXTURES, make_profile

FB = "https://www.facebook.com"
GRAPHQL = FB + "/api/graphql/"
JANE = "100004567890123"
WHO = {"key": "jane.doe", "username": "jane.doe", "numericId": None}
LOGIN_PAGE = '<div id="root"><form id="login_form"><input name="email"><input name="pass" type="password"></form></div>'
FULL = "https://scontent.xx.fbcdn.net/v/t39/9001001_original.jpg"
JPEG = b"\xff\xd8\xff\xd0fake-jpeg"


def fixture(name: str) -> str:
    return (FIXTURES / "facebook" / name).read_text()


def tab(body: str, *, name: str = "Jane Doe") -> str:
    return f'<div role="main"><h1>{name}</h1></div><script type="application/json">{body}</script>'


def person():
    return parse.person_from_arg("jane.doe")


class _FileResponse:
    def __init__(self, status: int, headers: dict, body: bytes) -> None:
        self.status, self.headers, self._body = status, headers, body

    async def body(self) -> bytes:
        return self._body

    async def dispose(self) -> None:
        return None


class _Request:
    def __init__(self, files: dict[str, tuple[bytes, str]]) -> None:
        self.files, self.got = files, []

    async def get(self, url, **_options):
        self.got.append(url)
        if url not in self.files:
            return _FileResponse(404, {"content-type": "text/html"}, b"missing")
        body, ctype = self.files[url]
        return _FileResponse(200, {"content-type": ctype}, body)


class _GraphQLResponse:
    def __init__(self, data) -> None:
        self.url, self.status, self.data = GRAPHQL, 200, data

    async def json(self):
        if isinstance(self.data, str):
            raise ValueError("not json")
        return self.data

    async def text(self) -> str:
        return self.data if isinstance(self.data, str) else json.dumps(self.data)


class PhotoPage:
    """FakePage plus the two things photos.py asks for: document HTML, and the page's own GraphQL responses."""

    def __init__(self, pages: dict, responses: dict | None = None, files: dict | None = None) -> None:
        self.pages = pages
        self.responses = responses or {}
        self.url, self.visits, self.scrolls = "about:blank", [], 0
        self.handlers: list = []
        self.request = _Request(files or {})

    async def goto(self, url, **_options):
        self.visits.append(url)
        self.url, self.scrolls = url, 0
        self._emit(0)

    def html(self) -> str:
        page = self.pages.get(self.url, "")
        return page[min(self.scrolls, len(page) - 1)] if isinstance(page, list) else page

    def on(self, event, handler):
        assert event == "response"
        self.handlers.append(handler)

    def _emit(self, index: int) -> None:
        batch = self.responses.get(self.url) or []
        if index >= len(batch) or batch[index] is None:
            return
        response = _GraphQLResponse(batch[index])
        for handler in list(self.handlers):
            handler(response)

    def is_closed(self) -> bool:
        return False

    async def evaluate(self, script, arg=None):
        if script == DOC_HTML_JS:
            return self.html()
        if script == fetch.SIGNED_OUT_JS:
            return 'name="pass"' in self.html()
        if script == fetch.SCROLL_JS:
            self.scrolls += 1
            self._emit(self.scrolls)
            return 1000 * self.scrolls
        raise AssertionError(f"unexpected page script: {script[:60]}")


@pytest.fixture
def photo_page(monkeypatch):
    async def no_pause(_span):
        return None

    monkeypatch.setattr(fetch, "pause", no_pause)
    policies: list = []

    def install(pages, responses=None, files=None):
        fake = PhotoPage(pages, responses, files)

        @asynccontextmanager
        async def site_page(policy, **options):
            assert options == {}
            policies.append(policy)
            yield fake

        monkeypatch.setattr(browser, "site_page", site_page)
        return fake, policies

    return install


def photos_of_url() -> str:
    return parse.photos_url(person(), "of")


def _ids(result) -> list[str]:
    return [row["id"] for row in result["photos"]]


# --- parsers -------------------------------------------------------------------------------------

def test_a_photo_keeps_the_full_image_the_caption_and_the_tags():
    [photo] = [row for row in parse.photos_in(json.loads(fixture("photos_of.json"))) if row["id"] == "9001001"]
    assert photo["imageUrl"].endswith("/9001001_full.jpg")
    assert "jane_avatar" not in photo["imageUrl"] and "9001001_thumb" not in photo["imageUrl"]
    assert photo["caption"] == "At the lake"  # accessibility_caption is not the caption
    assert photo["createdTime"] == "2024-07-01T00:00:00Z"
    assert photo["owner"] == {"id": "100009876543210", "name": "Eve Adams",
                              "url": f"{FB}/eve.adams"}
    assert photo["photoUrl"] == f"{FB}/photo/?fbid=9001001"
    assert photo["tags"] == [{"id": JANE, "name": "Jane Doe", "username": "jane.doe"}]
    assert photo["tagCount"] == 1 and photo["faceCount"] is None


def test_solo_is_exactly_one_tag_and_it_is_the_subject():
    rows = {row["id"]: row for row in parse.photos_in(json.loads(fixture("photos_of.json")))}
    subject = {"id": JANE, "ids": [JANE], "name": "Jane Doe", "names": ["Jane Doe"], "username": "jane.doe"}
    assert parse.is_solo(rows["9001001"], subject) is True
    assert parse.is_solo(rows["9001002"], subject) is False  # Jane and Sam
    assert rows["9001002"]["tagCount"] == 2
    assert parse.is_solo(rows["9001004"], subject) is False  # no tags at all
    assert parse.on_or_after(rows["9001001"], "2024-01-01T00:00:00Z") is True
    assert parse.on_or_after(rows["9001003"], "2024-01-01T00:00:00Z") is False
    assert parse.on_or_after({"createdTime": None}, "2024-01-01T00:00:00Z") is False
    hidden = {"__typename": "Photo", "id": "9001008", "tags": {"count": 3, "edges": [
        {"node": {"taggee": {"__typename": "User", "id": JANE, "name": "Jane Doe"}}}]}}
    record = parse.photo_record(hidden)
    assert record["tagCount"] == 3 and parse.is_solo(record, subject) is False
    by_name = {"tags": [{"id": None, "name": "Jane Doe"}], "tagCount": 1}
    assert parse.is_solo(by_name, {"ids": [], "names": ["Jane Doe"], "username": None}) is True
    mismatch = {"tags": [{"id": "100001111111111", "name": "Jane Doe"}], "tagCount": 1}
    assert parse.is_solo(mismatch, {"id": JANE, "ids": [JANE], "names": ["Jane Doe"]}) is False


def test_image_urls_drop_session_cookies_and_stay_on_the_cdn():
    dirty = "https://scontent.xx.fbcdn.net/v/t39/p.jpg?oh=abc&c_user=1&xs=secret&oe=def"
    cleaned = parse.clean_image_url(dirty)
    assert cleaned is not None and "c_user" not in cleaned and "xs" not in cleaned
    assert "oh=abc" in cleaned and "oe=def" in cleaned
    record = parse.photo_record({"__typename": "Photo", "id": "9001001", "image": {"uri": dirty, "width": 8, "height": 8}})
    assert record["imageUrl"] == cleaned
    assert image_url_allowed(cleaned) is True
    assert image_url_allowed("https://scontent.xx.fbcdn.net/v/t39/p.jpg") is True
    assert image_url_allowed("https://evil.example/p.jpg") is False
    assert image_url_allowed(f"{FB}/photo/?fbid=9001001") is False
    assert image_url_allowed("http://scontent.xx.fbcdn.net/v/a.jpg") is False
    assert image_url_allowed("https://user:pw@scontent.xx.fbcdn.net/v/a.jpg") is False


def test_dom_fallback_has_no_tags_and_is_not_solo():
    [photo] = parse.photos_from([fixture("photos_dom.html")])
    assert photo["id"] == "9001009" and photo["tags"] == [] and photo["tagCount"] == 0
    assert photo["imageUrl"].endswith("/9001009.jpg")
    assert parse.is_solo(photo, {"id": JANE, "ids": [JANE], "names": ["Jane Doe"]}) is False


def test_photo_and_search_urls_and_the_friends_filter():
    assert parse.photos_url(person(), "of") == f"{FB}/jane.doe/photos_of"
    assert parse.photos_url(person(), "uploads") == f"{FB}/jane.doe/photos_by"
    numeric = parse.person_from_arg(JANE)
    assert parse.photos_url(numeric, "of") == f"{FB}/profile.php?id={JANE}&sk=photos_of"
    url = parse.friends_search_url("Jane Doe")
    query = parse_qs(urlsplit(url).query)
    assert urlsplit(url).path == "/search/people/" and query["q"] == ["Jane Doe"]
    filters = json.loads(base64.b64decode(query["filters"][0]))
    assert json.loads(filters["friends"]) == {"name": "friends", "args": ""}


def test_friend_search_keeps_friends_and_whole_words():
    payload = json.loads(fixture("friends_search.json"))
    jane = parse.friend_hits([payload], "Jane Doe")
    assert [row["name"] for row in jane] == ["Jane Doe"]
    assert jane[0]["id"] == JANE and jane[0]["username"] == "jane.doe"
    assert [row["name"] for row in parse.friend_hits([payload], "Doe")] == ["Jane Doe", "Janet Doe-Example"]
    assert parse.friend_hits([payload], "Jane") == jane  # not Janet
    assert parse.friend_hits([payload], "Eve") == []  # not a friend
    snippet_only = {"profile": {"__typename": "User", "id": "100007777777777", "name": "Pat Example",
                                "url": f"{FB}/pat.example"},
                    "primary_snippet_text": "Friends · Springfield"}
    assert parse.friend_hits([snippet_only], "Pat")[0]["id"] == "100007777777777"


def test_photo_queries_are_reads_and_only_the_photos_command_may_send_them():
    op = "ProfileCometAppCollectionPhotosRendererPaginationQuery"
    body = "fb_api_req_friendly_name=" + op + "&doc_id=7123456789012345"
    assert request_allowed("POST", GRAPHQL, {"x-fb-friendly-name": op}, body, reads=endpoints.PHOTO_QUERIES)
    assert request_allowed("POST", GRAPHQL, {"x-fb-friendly-name": op}, body, reads=endpoints.SYNC_QUERIES) is False
    search = "SearchCometResultsInitialResultsQuery"
    search_body = "fb_api_req_friendly_name=" + search + "&doc_id=7123456789012345"
    assert request_allowed("POST", GRAPHQL, {"x-fb-friendly-name": search}, search_body,
                           reads=endpoints.FRIEND_SEARCH_QUERIES)
    assert request_allowed("POST", GRAPHQL, {"x-fb-friendly-name": search}, search_body,
                           reads=endpoints.PHOTO_QUERIES) is False
    for name in endpoints.PHOTO_QUERIES | endpoints.FRIEND_SEARCH_QUERIES:
        assert name.endswith("Query") and "mutation" not in name.lower()


def test_the_face_counter_stays_on_this_machine_and_names_the_extra_when_missing(monkeypatch):
    text = Path(faces.__file__).read_text()
    for needle in ("import socket", "import urllib", "import requests", "import httpx", "https://"):
        assert needle not in text
    monkeypatch.setattr(faces, "_cv2", lambda: None)
    assert faces.available() is False
    with pytest.raises(UsageError) as caught:
        faces.require()
    assert "truefrontier-webcli[faces]" in (caught.value.hint or "")
    assert "cloud" in (caught.value.hint or "")


# --- fetch ---------------------------------------------------------------------------------------

def _install_tab(photo_page, *, page2=True, files=None):
    url = photos_of_url()
    responses = {url: [None, json.loads(fixture("photos_of_page2.json"))]} if page2 else None
    pages = {
        url: tab(fixture("photos_of.json")),
        f"{FB}/photo/?fbid=9001001": fixture("photo_page_9001001.html"),
        f"{FB}/photo/?fbid=9001002": tab(json.dumps({"__typename": "Photo", "id": "9001002", "image": {
            "uri": "https://scontent.xx.fbcdn.net/v/t39/9001002_original.jpg", "width": 3000, "height": 2000}})),
    }
    files = files if files is not None else {
        FULL: (JPEG, "image/jpeg"),
        "https://scontent.xx.fbcdn.net/v/t39/9001002_original.jpg": (b"\xff\xd8group", "image/jpeg"),
    }
    return photo_page(pages, responses, files)


def test_list_reads_the_photos_of_tab_paginates_and_filters(photo_page):
    fake, policies = _install_tab(photo_page)
    result = asyncio.run(fetch.photos(WHO, solo=True, since="2024-01-01T00:00:00Z"))
    assert _ids(result) == ["9001001", "9001005"]
    assert result["subject"]["id"] == JANE and result["subject"]["name"] == "Jane Doe"
    assert result["complete"] is True and result["scrolls"] == 1 + endpoints.STALL_SCROLLS
    assert result["blocked"] == [] and result["mode"] == "of" and result["dropped"] == 0
    assert fake.visits[0] == photos_of_url()
    request = SimpleNamespace(method="POST", url=GRAPHQL, headers={"x-fb-friendly-name": endpoints.MUTATIONS[0]},
                              post_data="fb_api_req_friendly_name=" + endpoints.MUTATIONS[0] + "&doc_id=1")
    assert policies[0].allowed(request, guard.Guard()) is False
    allowed = SimpleNamespace(method="POST", url=GRAPHQL,
                              headers={"x-fb-friendly-name": "CometPhotoRootQuery"},
                              post_data="fb_api_req_friendly_name=CometPhotoRootQuery&doc_id=1")
    assert policies[0].allowed(allowed, guard.Guard()) is True


def test_list_limit_stops_before_the_next_page_and_uploads_uses_photos_by(photo_page):
    fake, _ = _install_tab(photo_page, page2=False)
    one = asyncio.run(fetch.photos(WHO, limit=1))
    assert _ids(one) == ["9001001"] and one["scrolls"] == 0 and one["complete"] is True
    uploads = f"{FB}/jane.doe/photos_by"
    fake.pages[uploads] = fake.pages[photos_of_url()]
    asyncio.run(fetch.photos(WHO, mode="uploads", limit=1))
    assert fake.visits[-1] == uploads


def test_signed_out_is_an_auth_error_and_a_blocked_empty_tab_is_not_an_empty_success(photo_page):
    photo_page({photos_of_url(): LOGIN_PAGE})
    with pytest.raises(AuthRequired, match="login form"):
        asyncio.run(fetch.photos(WHO))
    photo_page({photos_of_url(): fixture("profile_unavailable.html")})
    with pytest.raises(NotFound, match="isn't available"):
        asyncio.run(fetch.photos(WHO))
    photo_page({photos_of_url(): '<div role="main"><h1>Jane Doe</h1></div>'})
    guard.state.blocked.append("POST www.facebook.com/api/graphql/ op=SomeOtherQuery")
    with pytest.raises(RuntimeError, match="blocked"):
        asyncio.run(fetch.photos(WHO))


def test_friend_search_paginates_and_refuses_a_blocked_empty_page(photo_page):
    url = parse.friends_search_url("Doe")
    page1 = json.loads(fixture("friends_search.json"))
    edges = page1["data"]["serpResponse"]["results"]["edges"]
    janet = edges[2]
    page1["data"]["serpResponse"]["results"]["edges"] = edges[:2]
    fake, policies = photo_page({url: '<div role="main"><h1>Search</h1></div>'}, {url: [page1, {"data": {"serpResponse": {
        "results": {"edges": [janet]}}}}]})
    result = asyncio.run(fetch.friend_search("Doe"))
    assert [row["name"] for row in result["friends"]] == ["Jane Doe", "Janet Doe-Example"]
    assert fake.visits == [url] and result["complete"] is True
    search = SimpleNamespace(method="POST", url=GRAPHQL,
                             headers={"x-fb-friendly-name": "SearchCometResultsPaginatedResultsQuery"},
                             post_data="fb_api_req_friendly_name=SearchCometResultsPaginatedResultsQuery&doc_id=1")
    assert policies[0].allowed(search, guard.Guard()) is True
    photo_page({parse.friends_search_url("Eve"): '<div role="main"><h1>Search</h1></div>'})
    with pytest.raises(NotFound, match="No friend"):
        asyncio.run(fetch.friend_search("Eve"))
    photo_page({parse.friends_search_url("Zoe"): LOGIN_PAGE})
    with pytest.raises(AuthRequired):
        asyncio.run(fetch.friend_search("Zoe"))
    photo_page({parse.friends_search_url("Nobody"): '<div role="main"><h1>Search</h1></div>'})
    guard.state.blocked.append("POST www.facebook.com/api/graphql/ op=SearchCometResultsQuery")
    with pytest.raises(RuntimeError, match="blocked"):
        asyncio.run(fetch.friend_search("Nobody"))


def test_a_cdn_url_is_fetched_and_any_other_host_is_refused_before_the_get(photo_page):
    fake, _ = photo_page({}, files={FULL: (JPEG, "image/jpeg")})

    async def once():
        async with browser.site_page(read_policy(endpoints.PHOTO_QUERIES)) as page:
            body, ctype = await photos_io._file_bytes(page, FULL)
            assert (body, ctype) == (JPEG, "image/jpeg")
            with pytest.raises(RuntimeError, match="Refused"):
                await photos_io._file_bytes(page, "https://evil.example/a.jpg")

    asyncio.run(once())
    assert fake.request.got == [FULL]


def test_download_writes_the_full_image_and_skips_it_the_next_time(photo_page, tmp_path):
    fake, _ = _install_tab(photo_page)
    out = tmp_path / "jane"
    first = asyncio.run(fetch.photos_download(WHO, str(out), solo=True, since="2024-01-01", limit=1))
    path = out / "9001001.jpg"
    assert first["downloaded"] == 1 and first["skipped"] == 0 and path.read_bytes() == JPEG
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    manifest = json.loads((out / "manifest.json").read_text())
    assert stat.S_IMODE((out / "manifest.json").stat().st_mode) == 0o600
    [entry] = manifest["photos"]
    assert entry["id"] == "9001001" and entry["file"] == "9001001.jpg" and entry["kept"] is True
    assert entry["imageUrl"] == FULL and "c_user" not in entry["imageUrl"] and "xs=" not in entry["imageUrl"]
    assert entry["tags"][0]["name"] == "Jane Doe" and entry["tagCount"] == 1 and entry["faceCount"] is None
    assert manifest["profile"]["name"] == "Jane Doe" and manifest["solo"] is True
    manifest["photos"].append({"id": "9001999", "file": None, "kept": False, "faceCount": None})
    (out / "manifest.json").write_text(json.dumps(manifest))
    fake.request.got.clear()
    before = list(fake.visits)
    second = asyncio.run(fetch.photos_download(WHO, str(out), solo=True, since="2024-01-01", limit=1))
    assert second["downloaded"] == 0 and second["skipped"] == 1
    assert fake.request.got == [] and fake.visits[len(before):] == [photos_of_url()]
    saved = json.loads((out / "manifest.json").read_text())
    assert {row["id"] for row in saved["photos"]} == {"9001001", "9001999"}


def test_max_faces_records_the_count_and_drops_the_file(photo_page, monkeypatch, tmp_path):
    monkeypatch.setattr(faces, "require", lambda: None)
    monkeypatch.setattr(faces, "count_faces", lambda data: 4 if data == JPEG else 1)
    fake, _ = _install_tab(photo_page)
    out = tmp_path / "jane"
    saved = asyncio.run(fetch.photos_download(WHO, str(out), limit=1, max_faces=1))
    assert saved["downloaded"] == 0 and saved["dropped"] == 1 and saved["kept"] == 0
    assert not (out / "9001001.jpg").exists()
    [entry] = json.loads((out / "manifest.json").read_text())["photos"]
    assert entry["faceCount"] == 4 and entry["kept"] is False and entry["file"] is None
    with pytest.raises(NotFound, match="No photos of jane.doe") as caught:
        asyncio.run(fetch.photos(WHO, limit=1, max_faces=1))
    assert "over 1 faces" in (caught.value.hint or "")


def test_max_faces_on_a_file_already_saved_counts_it_without_fetching_again(photo_page, monkeypatch, tmp_path):
    fake, _ = _install_tab(photo_page)
    out = tmp_path / "jane"
    asyncio.run(fetch.photos_download(WHO, str(out), limit=1))
    assert (out / "9001001.jpg").is_file()
    fake.request.got.clear()
    monkeypatch.setattr(faces, "require", lambda: None)
    monkeypatch.setattr(faces, "count_faces", lambda _data: 3)
    again = asyncio.run(fetch.photos_download(WHO, str(out), limit=1, max_faces=1))
    assert again["dropped"] == 1 and again["skipped"] == 0 and fake.request.got == []
    assert not (out / "9001001.jpg").exists()
    [entry] = json.loads((out / "manifest.json").read_text())["photos"]
    assert entry["faceCount"] == 3 and entry["kept"] is False


def test_list_max_faces_keeps_a_photo_under_the_cap_and_stores_the_count(photo_page, monkeypatch):
    monkeypatch.setattr(faces, "require", lambda: None)
    monkeypatch.setattr(faces, "count_faces", lambda _data: 1)
    fake, _ = _install_tab(photo_page)
    result = asyncio.run(fetch.photos(WHO, solo=True, since="2024-01-01", limit=1, max_faces=1))
    [photo] = result["photos"]
    assert photo["faceCount"] == 1 and photo["imageUrl"] == FULL
    assert FULL in fake.request.got and result["dropped"] == 0


# --- commands ------------------------------------------------------------------------------------

@pytest.fixture
def calls(monkeypatch):
    make_profile("facebook:default")
    seen, answers = [], {}

    async def call(ident, site, op, *args, **kwargs):
        assert ident.id == "facebook:default" and site == "facebook"
        seen.append((op, args, kwargs))
        value = answers[op]
        return value(*args, **kwargs) if callable(value) else value

    monkeypatch.setattr(daemon, "call", call)
    return seen, answers


def run(*args):
    return CliRunner().invoke(webcli_app.build(), ["facebook", *args])


SAMPLE = {
    "id": "9001001", "imageUrl": FULL, "caption": "At the lake", "createdTime": "2024-07-01T00:00:00Z",
    "owner": {"id": "100009876543210", "name": "Eve Adams", "url": f"{FB}/eve.adams"},
    "photoUrl": f"{FB}/photo/?fbid=9001001",
    "tags": [{"id": JANE, "name": "Jane Doe"}], "tagCount": 1, "faceCount": None,
}


def test_photos_list_json_includes_tags_and_signed_out_is_not_an_empty_list(calls):
    seen, answers = calls
    answers["photos"] = {"photos": [SAMPLE], "subject": {"name": "Jane Doe", "id": JANE}, "complete": True,
                         "blocked": [], "dropped": 0}
    out = json.loads(run("photos", "list", "jane.doe", "--solo", "--json").stdout)
    assert out == [SAMPLE]
    assert seen[0][0] == "photos" and seen[0][2]["solo"] is True and seen[0][2]["mode"] == "of"
    assert seen[0][2]["max_faces"] is None and seen[0][1][0] == WHO

    def signed_out(*_args, **_kwargs):
        raise AuthRequired("Facebook shows a login form; the session is missing or expired.")

    answers["photos"] = signed_out
    result = run("photos", "list", "jane.doe", "--json")
    assert result.exit_code == 4 and "facebook:default" in result.stderr
    assert result.stdout.strip() != "[]" and not result.stdout.strip().startswith("[")


def test_max_faces_without_the_extra_exits_before_chrome(calls, monkeypatch):
    seen, _answers = calls
    monkeypatch.setattr(faces, "available", lambda: False)
    result = run("photos", "download", "jane.doe", "--out", "/tmp/jane-photos", "--max-faces", "1")
    assert result.exit_code == 2 and "truefrontier-webcli[faces]" in result.stderr
    assert seen == []
    assert run("photos", "list", "jane.doe", "--since", "not-a-date").exit_code == 2
    assert seen == []


def test_friends_search_json_and_download_flags(calls, tmp_path):
    seen, answers = calls
    answers["friend_search"] = {"friends": [{"id": JANE, "name": "Jane Doe", "username": "jane.doe",
                                             "profileUrl": f"{FB}/jane.doe", "snippet": "Friends"}],
                                "complete": True, "blocked": [], "scrolls": 3}
    out = json.loads(run("friends", "search", "Jane Doe", "--json").stdout)
    assert out[0]["id"] == JANE and out[0]["profileUrl"] == f"{FB}/jane.doe"
    answers["friend_search"] = {"friends": [], "complete": True, "blocked": [], "scrolls": 3}
    assert run("friends", "search", "Nobody").exit_code == 3
    answers["photos_download"] = {"dir": str(tmp_path), "manifest": str(tmp_path / "manifest.json"),
                                  "downloaded": 1, "skipped": 0, "dropped": 0, "kept": 1, "complete": True,
                                  "blocked": [], "mode": "uploads", "solo": True, "maxFaces": None}
    result = run("photos", "download", "jane.doe", "--uploads", "--solo", "--since", "2024-01-01",
                 "--out", str(tmp_path), "--json")
    assert result.exit_code == 0, result.output
    saved = json.loads(result.stdout)
    assert saved["downloaded"] == 1 and saved["solo"] is True
    _op, args, kwargs = seen[-1]
    assert kwargs["mode"] == "uploads" and kwargs["solo"] is True
    assert kwargs["since"] == "2024-01-01T00:00:00Z" and Path(args[1]) == tmp_path.resolve()


def test_help_names_solo_and_max_faces():
    result = run("photos", "list", "--help")
    assert result.exit_code == 0
    assert "--solo" in result.output and "--max-faces" in result.output and "--uploads" in result.output
    download = run("photos", "download", "--help")
    assert "--out" in download.output and "--max-faces" in download.output
