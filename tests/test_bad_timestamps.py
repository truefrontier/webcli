"""A site's timestamp outside datetime's range (epoch milliseconds read as seconds, or garbage) is no time, not a
crash that fails the whole list. The same bug chatgpt-web-cli's overflow/chatgptwebclitests fixed in its iso_time
(761ee2c); LinkedIn's posts parser already guards it this way."""

from __future__ import annotations

from datetime import timezone

import pytest

from webcli.sites.contacts import parse as contacts
from webcli.sites.gemini import parse as gemini
from webcli.sites.gsc import parse as gsc
from webcli.sites.linkedin import boost
from webcli.sites.linkedin import parse as linkedin
from webcli.sites.recorder import parse as recorder

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
