"""Offline tests for webcli youtube's best-effort My Activity parsers and source (ported from ytcli; only
myactivity_gate.txt is real page text)."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from webcli.sites.youtube import fetch, myactivity
from webcli.sites.youtube.fetch import VerificationRequired
from webcli.sites.youtube.myactivity import entries_from_payload, iter_payloads, parse_bodies, parse_dom, verification_required

FIXTURES = Path(__file__).parent / "fixtures" / "youtube"
CHICAGO = ZoneInfo("America/Chicago")
BATCH = (FIXTURES / "myactivity_batchexecute.txt").read_text()
HTML = (FIXTURES / "myactivity_page.html").read_text()
GATE = (FIXTURES / "myactivity_gate.txt").read_text()  # real page text, behind the verification gate
DOM = (FIXTURES / "myactivity_dom.html").read_text()  # authored: no ungated page has been captured


def test_batchexecute_stream_decodes_inner_payload():
    payloads = list(iter_payloads(BATCH))
    assert len(payloads) == 1
    assert payloads[0][1] == "next-page-token"


def test_html_init_data_is_decoded():
    payloads = list(iter_payloads(HTML))
    assert payloads[0] == ["en", None, True]
    assert payloads[1][1] == "first-page-token"


def test_parse_bodies_reads_watches_only_newest_first_and_dedupes():
    rows = parse_bodies([HTML, BATCH], tz=CHICAGO)
    assert [(r["videoId"], r["title"], r["watchedAt"]) for r in rows] == [
        ("jNQXAC9IVRw", "Me at the zoo", "2025-10-14T22:46:40-05:00"),
        ("dQw4w9WgXcQ", "Never Gonna Give You Up", "2025-10-14T09:00:00-05:00"),
        ("dQw4w9WgXcQ", "Never Gonna Give You Up", "2025-10-13T19:00:00-05:00"),
        ("aaaaaaaaaaa", "Short clip", "2025-10-12T15:13:20-05:00"),
        ("ccccccccccc", None, "2025-10-08T00:06:40-05:00"),
    ]
    assert rows[1] == {
        "title": "Never Gonna Give You Up",
        "channel": "Rick Astley",
        "channelUrl": "https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw",
        "videoId": "dQw4w9WgXcQ",
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "watchedAt": "2025-10-14T09:00:00-05:00",
        "watchedAtPrecision": "exact",
        "durationSec": None,
        "isShort": False,
        "source": "myactivity",
    }
    assert (rows[3]["isShort"], rows[3]["url"], rows[3]["channel"], rows[3]["channelUrl"]) == (
        True,
        "https://www.youtube.com/shorts/aaaaaaaaaaa",
        "Some Creator",
        "https://www.youtube.com/@somecreator",
    )


def test_records_are_never_stitched_together():
    liked = ["a", 1760100000000000, ["Liked Me at the zoo"], ["https://www.youtube.com/watch?v=jNQXAC9IVRw"]]
    removed = ["b", 1760000000000000, ["Watched a video that has been removed"]]
    assert entries_from_payload([[liked, removed]]) == []


def test_ambiguous_candidates_are_skipped():
    two_videos = [1760000000000000, "Watched X", "https://youtu.be/aaaaaaaaaaa", "https://youtu.be/bbbbbbbbbbb"]
    assert entries_from_payload(two_videos) == []


def test_unrecognized_bodies_yield_nothing():
    assert parse_bodies(["", "<html></html>", ")]}'\n\nnot json", json.dumps({"a": 1})]) == []



def test_verification_gate_is_detected():
    assert verification_required(GATE)
    assert verification_required("To see your full history, verify it's you")
    assert not verification_required("YouTube History\nManage My Activity verification\nWatched Me at the zoo")
    assert not verification_required("Watched Verify it's you\nWatched Verify")  # video titles, not the gate


def test_rendered_dom_rows_combine_day_header_and_time():
    rows = parse_dom(DOM, date(2026, 10, 14), tz=CHICAGO)
    assert [(r["videoId"], r["watchedAt"], r["watchedAtPrecision"], r["channel"]) for r in rows] == [
        ("dQw4w9WgXcQ", "2026-10-14T10:41:00-05:00", "exact", "Rick Astley"),
        ("aaaaaaaaaaa", "2026-10-14T09:05:00-05:00", "exact", "Some Creator"),
        ("jNQXAC9IVRw", "2026-10-12T23:15:00-05:00", "exact", "jawed"),
        ("ccccccccccc", "2026-10-12T00:00:00-05:00", "day", None),  # no time shown: day precision
    ]
    assert rows[1] == {
        "title": "Short clip",
        "channel": "Some Creator",
        "channelUrl": "https://www.youtube.com/@somecreator",
        "videoId": "aaaaaaaaaaa",
        "url": "https://www.youtube.com/shorts/aaaaaaaaaaa",
        "watchedAt": "2026-10-14T09:05:00-05:00",
        "watchedAtPrecision": "exact",
        "durationSec": None,
        "isShort": True,
        "source": "myactivity",
    }


def test_rendered_dom_without_a_day_header_has_no_time():
    rows = parse_dom('<div>Watched <a href="https://youtu.be/dQw4w9WgXcQ">X</a></div><div>8:00 PM</div>', date(2026, 10, 14))
    assert [(r["videoId"], r["watchedAt"], r["watchedAtPrecision"]) for r in rows] == [("dQw4w9WgXcQ", None, None)]


def test_rendered_dom_time_split_across_elements():
    html = '<h2>Today</h2><div>Watched <a href="https://youtu.be/dQw4w9WgXcQ">X</a></div><div><span>10:41</span><span>PM</span></div>'
    assert parse_dom(html, date(2026, 10, 14), tz=CHICAGO)[0]["watchedAt"] == "2026-10-14T22:41:00-05:00"


def test_gated_page_has_no_dom_rows():
    assert parse_dom("<p>To see your full history, verify it’s you</p><button>Verify</button>", date(2026, 10, 14)) == []


class FakePage:
    """Just enough of a Playwright page for fetch.myactivity_history: a page with no network bodies."""

    url = "https://myactivity.google.com/product/youtube"

    def __init__(self, html: str, text: str):
        self.html, self.text = html, text
        self.context = self
        self.mouse = self

    def on(self, event, handler):
        pass

    def remove_listener(self, event, handler):
        pass

    async def goto(self, url, **kwargs):
        pass

    async def wait_for_timeout(self, ms):
        pass

    async def wheel(self, x, y):
        pass

    async def content(self):
        return self.html

    async def inner_text(self, selector):
        return self.text


def test_gated_page_raises_verification_required():
    with pytest.raises(VerificationRequired, match=r"Manage My Activity verification"):
        asyncio.run(fetch.myactivity_history(FakePage("<html></html>", GATE), limit=5, cutoff=None, notes=[]))


def test_dom_fallback_when_network_parse_is_empty():
    rows = asyncio.run(fetch.myactivity_history(FakePage(DOM, "Today"), limit=2, cutoff=None, notes=[]))
    assert [(r["videoId"], r["watchedAtPrecision"]) for r in rows] == [("dQw4w9WgXcQ", "exact"), ("aaaaaaaaaaa", "exact")]


def test_dom_wins_when_page_data_misses_entries():
    rows = asyncio.run(fetch.myactivity_history(FakePage(DOM + HTML, "Today"), limit=10, cutoff=None, notes=[]))
    assert len(myactivity.parse_bodies([DOM + HTML])) == 2
    assert [r["videoId"] for r in rows] == ["dQw4w9WgXcQ", "aaaaaaaaaaa", "jNQXAC9IVRw", "ccccccccccc"]


def test_no_limit_scrolls_until_the_list_stops_growing():
    rows = asyncio.run(fetch.myactivity_history(FakePage(DOM + HTML, "Today"), limit=None, cutoff=None, notes=[]))
    assert [r["videoId"] for r in rows] == ["dQw4w9WgXcQ", "aaaaaaaaaaa", "jNQXAC9IVRw", "ccccccccccc"]
