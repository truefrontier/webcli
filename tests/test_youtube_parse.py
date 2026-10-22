"""Offline tests for webcli youtube's youtubei parsers against authored fixtures (ported from ytcli)."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from webcli.sites.youtube import parse
from webcli.sites.youtube.parse import (
    account_from_menu,
    continuation_token,
    history_page,
    parse_day_header,
    parse_duration,
    parse_since,
    take_newer,
    video_row,
)

FIXTURES = Path(__file__).parent / "fixtures" / "youtube"
CHICAGO = ZoneInfo("America/Chicago")
TODAY = date(2026, 10, 14)  # a Wednesday


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Today", date(2026, 10, 14)),
        ("Yesterday", date(2026, 10, 13)),
        ("Monday", date(2026, 10, 12)),
        ("Thursday", date(2026, 10, 8)),
        ("Wednesday", date(2026, 10, 7)),
        ("Oct 3", date(2026, 10, 3)),
        ("October 3", date(2026, 10, 3)),
        ("Dec 25", date(2025, 12, 25)),  # future this year -> last year
        ("Oct 3, 2025", date(2025, 10, 3)),
        ("3 Oct 2024", date(2024, 10, 3)),
        ("Sept 9", date(2026, 9, 9)),
        ("Feb 30", None),
        ("This week", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_day_header(header, expected):
    assert parse_day_header(header, TODAY) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("12:34", 754), ("1:02:03", 3723), ("0:59", 59), ("10:00:00", 36000), ("LIVE", None), ("SHORTS", None), ("", None), (None, None)],
)
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


def test_first_page_rows():
    rows, token, day = history_page(load("fehistory_initial.json"), today=TODAY, tz=CHICAGO)
    assert token == "TOKEN_PAGE_2"
    assert day == date(2026, 10, 12)
    assert [(r["videoId"], r["watchedAt"], r["durationSec"], r["isShort"]) for r in rows] == [
        ("dQw4w9WgXcQ", "2026-10-14T00:00:00-05:00", 213, False),
        ("jNQXAC9IVRw", "2026-10-14T00:00:00-05:00", 19, False),
        ("aaaaaaaaaaa", "2026-10-13T00:00:00-05:00", None, True),
        ("bbbbbbbbbbb", "2026-10-13T00:00:00-05:00", None, True),
        ("ddddddddddd", "2026-10-12T00:00:00-05:00", 3723, False),
    ]
    assert rows[0] == {
        "title": "Never Gonna Give You Up",
        "channel": "Rick Astley",
        "channelUrl": "https://www.youtube.com/@RickAstleyYT",
        "videoId": "dQw4w9WgXcQ",
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "watchedAt": "2026-10-14T00:00:00-05:00",
        "watchedAtPrecision": "day",
        "durationSec": 213,
        "isShort": False,
        "source": "youtube-history",
    }
    assert (rows[1]["title"], rows[1]["channel"], rows[1]["channelUrl"]) == ("Me at the zoo", "jawed", "https://www.youtube.com/@jawed")
    assert (rows[2]["title"], rows[2]["url"]) == ("Short one", "https://www.youtube.com/shorts/aaaaaaaaaaa")


def test_continuation_page_carries_day_and_handles_unknown_header():
    rows, token, day = history_page(load("fehistory_continuation.json"), today=TODAY, day=date(2026, 10, 12), tz=CHICAGO)
    assert token is None
    assert day is None
    assert [(r["videoId"], r["watchedAt"], r["watchedAtPrecision"], r["durationSec"], r["isShort"]) for r in rows] == [
        ("eeeeeeeeeee", "2026-10-12T00:00:00-05:00", "day", None, True),
        ("fffffffffff", "2026-10-03T00:00:00-05:00", "day", None, False),
        ("ggggggggggg", "2025-10-03T00:00:00-05:00", "day", 605, False),
        ("hhhhhhhhhhh", None, None, 240, False),
    ]


def test_day_precision_uses_the_offset_of_that_date():
    resp = {"contents": [{"itemSectionRenderer": {
        "contents": [{"videoRenderer": {"videoId": "xxxxxxxxxxx", "title": {"simpleText": "Winter"}}}],
        "header": {"itemSectionHeaderRenderer": {"title": {"simpleText": "Dec 25"}}},
    }}]}
    rows, _, _ = history_page(resp, today=TODAY, tz=CHICAGO)
    assert rows[0]["watchedAt"] == "2025-12-25T00:00:00-06:00"


def test_continuation_token_ignores_sidebar():
    assert continuation_token(load("fehistory_initial.json")) == "TOKEN_PAGE_2"
    assert continuation_token({"contents": {"secondaryContents": {"continuationItemRenderer": {
        "continuationEndpoint": {"continuationCommand": {"token": "NOPE"}}}}}}) is None


def test_video_row_skips_playlists_and_missing_ids():
    assert video_row("lockupViewModel", {"contentId": "PL1", "contentType": "LOCKUP_CONTENT_TYPE_PLAYLIST"}) is None
    assert video_row("videoRenderer", {"title": {"simpleText": "no id"}}) is None


def test_account_from_menu():
    assert account_from_menu(load("account_menu.json")) == {
        "accountName": "Test Viewer",
        "email": "viewer@example.com",
        "handle": "@testviewer",
    }
    assert account_from_menu({}) == {"accountName": None, "email": None, "handle": None}


def test_parse_since():
    assert parse_since(None) is None
    assert parse_since("2026-10-01", tz=CHICAGO).isoformat() == "2026-10-01T00:00:00-05:00"
    assert parse_since("2026-10-01T06:00:00Z", tz=CHICAGO).isoformat() == "2026-10-01T01:00:00-05:00"
    with pytest.raises(ValueError):
        parse_since("last tuesday")


def _row(at: str | None, precision: str | None) -> dict:
    return {"watchedAt": at, "watchedAtPrecision": precision}


def test_take_newer_stops_at_first_older_row():
    cutoff = datetime(2026, 10, 12, 15, 0, tzinfo=CHICAGO)
    rows = [
        _row("2026-10-14T09:00:00-05:00", "exact"),
        _row(None, None),  # undated rows ride along until an older row appears
        _row("2026-10-12T00:00:00-05:00", "day"),  # same day as cutoff: kept
        _row("2026-10-12T14:59:00-05:00", "exact"),  # older than cutoff: stop
        _row("2026-10-14T00:00:00-05:00", "day"),
    ]
    assert take_newer(rows, cutoff) == (rows[:3], True)
    assert take_newer(rows[:3], cutoff) == (rows[:3], False)
    assert take_newer(rows, None) == (rows, False)


def _pl_row(video: str) -> dict:
    return {"playlistVideoRenderer": {"videoId": video}}


def _more(token: str) -> dict:
    return {"continuationItemRenderer": {"continuationEndpoint": {"continuationCommand": {"token": token}}}}


def test_playlist_page_reads_only_the_playlists_own_rows_and_token():
    # Live shape (2026-10-21): the list, then a recommendations section with its own continuation.
    first = {"contents": {"twoColumnBrowseResultsRenderer": {"tabs": [{"tabRenderer": {"content": {
        "sectionListRenderer": {"contents": [
            {"itemSectionRenderer": {"contents": [{"playlistVideoListRenderer": {
                "contents": [_pl_row("_0d4Hm5Hs6g"), _pl_row("ABQLEtCyb1g"), _more("list-2")]}}]}},
            {"itemSectionRenderer": {"contents": [_pl_row("jNQXAC9IVRw")]}},
            _more("recommended")]}}}}]}}}
    assert parse.playlist_page(first) == (["_0d4Hm5Hs6g", "ABQLEtCyb1g"], "list-2")
    later = {"onResponseReceivedActions": [{"appendContinuationItemsAction": {"continuationItems": [
        _pl_row("mJ7L9RK7kyc")]}}]}
    assert parse.playlist_page(later) == (["mJ7L9RK7kyc"], None)
    recommended = {"onResponseReceivedActions": [{"appendContinuationItemsAction": {"continuationItems": [
        {"itemSectionRenderer": {"contents": [_pl_row("jNQXAC9IVRw")]}}]}}]}
    assert parse.playlist_page(recommended) == ([], None)


@pytest.mark.parametrize(("text", "video"), [
    ("_0d4Hm5Hs6g", "_0d4Hm5Hs6g"), ("https://youtu.be/ABQLEtCyb1g?si=x", "ABQLEtCyb1g"),
    ("youtube.com/watch?v=mJ7L9RK7kyc&t=3", "mJ7L9RK7kyc"), ("https://m.youtube.com/shorts/jNQXAC9IVRw", "jNQXAC9IVRw"),
    ("https://www.youtube.com/embed/jNQXAC9IVRw", "jNQXAC9IVRw"),
])
def test_video_id_takes_ids_and_links(text, video):
    assert parse.video_id(text) == video


@pytest.mark.parametrize("text", ["nope", "https://evil.example/watch?v=jNQXAC9IVRw", "https://youtu.be/short",
                                  "https://www.youtube.com/playlist?list=PL1", "jNQXAC9IVRw1"])
def test_video_id_refuses_anything_else(text):
    with pytest.raises(ValueError):
        parse.video_id(text)
