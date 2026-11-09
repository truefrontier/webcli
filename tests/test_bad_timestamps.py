"""A site's timestamp outside datetime's range (epoch milliseconds read as seconds, or garbage) is no time, not a
crash that fails the whole list. The same bug chatgpt-web-cli's overflow/chatgptwebclitests fixed in its iso_time
(761ee2c); LinkedIn's posts parser already guards it this way."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

from webcli.apps import webcli as webcli_app
from webcli.core import daemon
from webcli.sites import data
from webcli.sites.contacts import parse as contacts
from webcli.sites.gemini import parse as gemini
from webcli.sites.gsc import parse as gsc
from webcli.sites.linkedin import boost
from webcli.sites.linkedin import parse as linkedin
from webcli.sites.linkedin import posts
from webcli.sites.recorder import parse as recorder
from webcli.sites.whatsapp import parse as whatsapp
from webcli.sites.youtube import feeds

HUGE = 10**20  # past datetime's range as seconds, milliseconds or microseconds
MILLIS = 1760814000000  # epoch milliseconds where seconds belong: year 57767


@pytest.mark.parametrize(("read", "value", "nothing"), [
    (gemini.iso_time, MILLIS, None), (gemini.iso_time, [HUGE, 0], None), (gemini.iso_time, 1e300, None),
    (contacts._iso_ms, HUGE, None), (contacts._iso_ms, str(HUGE), None),
    (gsc.iso_ms, HUGE, None), (gsc.iso_ms, 1e300, None),
    (gsc.report_day, HUGE, None),
    (boost.ms_to_dt, HUGE, None),
    (linkedin._ms_to_iso, HUGE, ""), (linkedin._ms_to_iso, str(HUGE), ""),
])
def test_an_out_of_range_timestamp_is_no_time(read, value, nothing):
    assert read(value) == nothing


@pytest.mark.parametrize(("read", "value", "expected"), [
    (gemini.iso_time, [1760450400, 999999999], "2025-10-14T14:00:00Z"),
    (contacts._iso_ms, 1760450400000, "2025-10-14T14:00:00Z"),
    (gsc.iso_ms, 1760450400000, "2025-10-14T14:00:00Z"),
    (linkedin._ms_to_iso, 1760450400000, "2025-10-14T14:00:00Z"),
    (linkedin._ms_to_iso, 1760450400, "2025-10-14T14:00:00Z"),  # seconds, by its own heuristic
])
def test_a_good_timestamp_still_reads(read, value, expected):
    assert read(value) == expected


def test_linkedin_conversations_survive_a_bad_time():
    data = {"elements": [{"entityUrn": "urn:li:msg_conversation:(urn:li:fsd_profile:A,2-x)", "lastActivityAt": HUGE}]}
    rows = linkedin.conversations(data)
    assert [row["time"] for row in rows] == [""]


def test_a_recording_with_a_bad_time_is_skipped_not_the_list():
    def rec(id_: str, created: int) -> list:
        return [id_, id_, [str(created), 0], ["125", 0], None, None, None, None, None, None, None, None, None, ""]

    rows = recorder.recordings([[rec("r1", 1760900000), rec("r2", HUGE)], "cur"], tz=timezone.utc)
    assert [row["id"] for row in rows] == ["r1"]


# A relative time ("3yr", "2 days ago", --since 1w) past datetime's range: the same crash as above, through timedelta.
NOW = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
FAR = ["3000000w", "99999999999d", "9" * 30 + "h"]  # past year 1; past timedelta's range; past a C int


@pytest.mark.parametrize("value", FAR)
@pytest.mark.parametrize("since", [
    lambda v: whatsapp.parse_since(v, NOW.replace(tzinfo=None)),
    lambda v: linkedin.parse_since(v, now=NOW),
    lambda v: feeds.parse_since(v, NOW),
], ids=["whatsapp", "linkedin", "youtube-feeds"])
def test_a_since_too_far_back_is_a_bad_value_not_a_crash(since, value):
    with pytest.raises(ValueError, match="too far back"):
        since(value)


@pytest.mark.parametrize("args", [
    ["whatsapp", "read", "Mom", "--since"],
    ["linkedin", "feed", "--since"],
    ["linkedin", "activity", "export", "--since"],
    ["youtube", "uploads", "--since"],
])
def test_the_cli_says_so_and_exits_2(args, monkeypatch):
    def never(*a, **k):
        raise AssertionError("a bad --since must stop before any site call")

    monkeypatch.setattr(daemon, "call", never)
    monkeypatch.setattr(data, "call", never)
    result = CliRunner().invoke(webcli_app.build(), [*args, "3000000w"])
    assert result.exit_code == 2, result.output
    assert "too far back" in result.output


def test_a_linkedin_relative_time_too_far_back_is_no_time():
    assert linkedin.linkedin_time_to_iso("9999yr", now=NOW) == ""
    assert linkedin.linkedin_time_to_iso("99999999999d", now=NOW) == ""
    # In range: only the unit it names is computed (weeks of 10**9 would overflow).
    assert linkedin.linkedin_time_to_iso("1000000000s", now=NOW) == "1995-03-03T10:13:20Z"
    assert linkedin.linkedin_time_to_iso("3yr", now=NOW) == "2023-11-10T12:00:00Z"


def test_a_linkedin_post_card_too_old_to_date_is_undated_not_the_feed():
    card = {"actorName": "Jane Doe", "actorHref": "https://www.linkedin.com/in/jane-doe/",
            "actorLines": ["Jane Doe", "Widget engineer at Example Co", "9999yr •"], "header": [], "text": "Hi",
            "reactions": "", "comments": "", "reposts": "", "articleLinks": [], "links": [], "ids": ""}
    row = posts.card_row(card, now=NOW)
    assert (row["author_name"], row["posted_at"]) == ("Jane Doe", None)


def test_a_youtube_video_too_old_to_date_keeps_its_text():
    video = {"videoId": "a", "title": {"simpleText": "Video a"}, "publishedTimeText": {"simpleText": "99999 years ago"}}
    old, fresh = feeds.video("videoRenderer", video, NOW), feeds.video("videoRenderer", {
        **video, "videoId": "b", "publishedTimeText": {"simpleText": "2 days ago"}}, NOW)
    assert (old["publishedAt"], old["publishedText"]) == (None, "99999 years ago")
    assert fresh["publishedAt"] == "2026-11-07T12:00:00Z"
