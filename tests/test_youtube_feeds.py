"""webcli youtube subs / uploads / home parsers, offline: synthetic browse responses. Shorts shelves and Shorts are
dropped unless asked for; uploads skips the "Most relevant" shelf; counts, ages, and --since parse."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from webcli.sites.youtube import feeds

NOW = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)


def vr(vid: str, age: str = "2 hours ago", *, short=False) -> dict:
    r = {"videoId": vid, "title": {"simpleText": f"Video {vid}"},
         "ownerText": {"runs": [{"text": "Example Channel", "navigationEndpoint": {"browseEndpoint": {
             "browseId": "UCexample", "canonicalBaseUrl": "/@example"}}}]},
         "publishedTimeText": {"simpleText": age}, "viewCountText": {"simpleText": "1,234 views"},
         "lengthText": {"simpleText": "10:00"}}
    if short:
        r["thumbnailOverlays"] = [{"thumbnailOverlayTimeStatusRenderer": {"style": "SHORTS"}}]
    return {"videoRenderer": r}


def shelf(title: str, contents: list) -> dict:
    return {"richSectionRenderer": {"content": {"richShelfRenderer": {"title": {"runs": [{"text": title}]},
                                                                       "contents": contents}}}}


def resp(*contents) -> dict:
    return {"contents": {"twoColumnBrowseResultsRenderer": {"tabs": [{"tabRenderer": {"content": {
        "richGridRenderer": {"contents": list(contents)}}}}]}}}


def test_counts_and_ages():
    assert feeds.count("9.3M") == 9_300_000 and feeds.count("1,234 views") == 1234
    assert feeds.count("642K subscribers") == 642_000 and feeds.count("@handle") is None
    assert feeds.age_seconds("2d ago") == 172_800 and feeds.age_seconds("Streamed 1 week ago") == 604_800
    assert feeds.age_seconds("LIVE") is None


def test_parse_since():
    assert feeds.parse_since("48h", NOW) == datetime(2026, 1, 8, 12, 0, tzinfo=timezone.utc)
    assert feeds.parse_since("1w", NOW) == datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc)
    assert feeds.parse_since("2026-01-01", NOW) == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert feeds.parse_since(None) is None
    with pytest.raises(ValueError):
        feeds.parse_since("last tuesday")


def test_shorts_are_dropped_unless_asked_for():
    r = resp(vr("a"), shelf("Shorts", [vr("s1", short=True)]), vr("b", short=True), vr("c"))
    assert [x["id"] for x in feeds.videos(r, NOW)] == ["a", "c"]
    assert [x["id"] for x in feeds.videos(r, NOW, include_shorts=True)] == ["a", "s1", "b", "c"]


def test_video_row_shape():
    row = feeds.videos(resp(vr("a", "3 hours ago")), NOW)[0]
    assert row["publishedAt"] == "2026-01-10T09:00:00Z" and row["publishedText"] == "3 hours ago"
    assert (row["source"], row["url"], row["author"], row["auth"]) == (
        "youtube", "https://www.youtube.com/watch?v=a", "Example Channel", "signed_in")
    assert row["metrics"] == {"views": 1234, "durationSec": 600} and row["isShort"] is False


def test_uploads_skip_most_relevant():
    r = resp(shelf("Most relevant", [vr("old", "5 days ago")]), vr("new"))
    assert [x["id"] for x in feeds.videos(r, NOW, skip_shelves=feeds.UPLOADS_SKIP)] == ["new"]
    assert [x["id"] for x in feeds.videos(r, NOW)] == ["old", "new"]


def test_channels_sniff_handle_and_count():
    r = {"contents": {"x": [{"channelRenderer": {
        "channelId": "UC1", "title": {"simpleText": "Example"}, "subscriberCountText": {"simpleText": "@example"},
        "videoCountText": {"simpleText": "642K subscribers"},
        "navigationEndpoint": {"browseEndpoint": {"canonicalBaseUrl": "/@example"}}}},
        {"channelRenderer": {"channelId": "UC1"}}]}}
    rows = feeds.channels(r)
    assert len(rows) == 1
    assert (rows[0]["author"], rows[0]["metrics"], rows[0]["url"]) == (
        "@example", {"subscribers": 642_000}, "https://www.youtube.com/@example")
