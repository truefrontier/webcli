"""webcli gsc, offline: the guard (reads only, inspection never live, each write once and only in its own policy),
the parsers over synthetic Search Console answers, the fetch flow on a fake page, and the commands over a faked
daemon, including the --yes gate and the audit line. Synthetic data (example.com); nothing reaches Google."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from click.testing import CliRunner

from webcli.apps import webcli as webcli_app
from webcli.core import browser, daemon
from webcli.core.errors import AuthRequired, NotFound, UsageError
from webcli.sites import registry
from webcli.sites.gsc import fetch, guard, parse
from webcli.sites.gsc.endpoints import BATCH_PATH

PROP = "sc-domain:example.com"
BATCH = f"https://search.google.com{BATCH_PATH}"


def body(*entries) -> str:
    """A batchexecute form body for [(rpcid, payload), ...]."""
    freq = json.dumps([[[rid, json.dumps(p), None, "generic"] for rid, p in entries]])
    return urlencode({"f.req": freq, "at": "TOKEN"})


def post(*entries, rpcids=None, method="POST"):
    ids = rpcids if rpcids is not None else ",".join(rid for rid, _ in entries)
    return method, f"{BATCH}?rpcids={ids}&source-path=/search-console", body(*entries)


def answer(rpcid: str, payload) -> str:
    """A batchexecute answer carrying `payload` for rpcid (None: an error envelope, code 3)."""
    env = (["wrb.fr", rpcid, json.dumps(payload), None, None, None, "generic"] if payload is not None
           else ["wrb.fr", rpcid, None, None, None, [3], "generic"])
    line = json.dumps([env, ["di", 40]])
    return f")]}}'\n\n{len(line)}\n{line}\n"


# The guard

@pytest.mark.parametrize(("req", "ok"), [
    (post(("BWF8he", [])), True),
    (post(("vyvePb", [PROP, PROP])), True),
    (post(("RVtklb", ["https://example.com/", None, 4, 2, PROP])), True),
    (post(("RVtklb", ["https://example.com/", None, 4, 1, PROP])), False),  # a live test is never a read
    (post(("CyYwI", [PROP, "https://example.com/sitemap.xml"])), False),  # sitemap submit
    (post(("Fj3Owf", ["JOB", "ATTESTATION", None, PROP])), False),  # request indexing
    (post(("SM7Bqb", [[1, [[[PROP]]]]])), False),  # the page's own recent-properties write
    (post(("BWF8he", []), ("vyvePb", [PROP, PROP])), False),  # one rpcid per call
    (post(("BWF8he", []), rpcids="BWF8he,CyYwI"), False),  # label and body disagree
    (post(("CyYwI", [PROP, "x"]), rpcids="BWF8he"), False),
    (post(("BWF8he", []), method="GET"), False),
    (("GET", "https://search.google.com/search-console?resource_id=x", None), True),
    (("POST", "https://search.google.com/_/SearchConsoleAggReportUi/jserror", ""), False),
    (("POST", "https://evil.example.com" + BATCH_PATH + "?rpcids=BWF8he", body(("BWF8he", []))), False),
])
def test_read_guard(req, ok):
    assert guard.read_allowed(*req) is ok


def test_submit_policy_allows_exactly_that_sitemap_once():
    budget = guard.Budget({"CyYwI": 1})
    check = guard.submit_check(PROP, "https://example.com/sitemap.xml", budget)
    assert check(*post(("CyYwI", [PROP, "https://example.com/other.xml"]))) is False
    assert check(*post(("CyYwI", ["sc-domain:other.example", "https://example.com/sitemap.xml"]))) is False
    assert check(*post(("CyYwI", [PROP, "https://example.com/sitemap.xml"]))) is True
    assert check(*post(("CyYwI", [PROP, "https://example.com/sitemap.xml"]))) is False  # budget spent
    assert budget.sent == {"CyYwI": 1}
    assert check(*post(("vyvePb", [PROP, PROP]))) is True  # reads still pass
    assert check(*post(("Fj3Owf", ["JOB", "T", None, PROP]))) is False


def test_request_indexing_policy_allows_the_page_flow_and_one_request():
    budget = guard.Budget({"Fj3Owf": 1})
    check = guard.request_indexing_check(budget)
    job = "JOB"
    assert check(*post(("RVtklb", ["https://example.com/", None, 4, 1, PROP]))) is True  # the live test
    assert check(*post(("yol87d", [job, 2, None, PROP]), ("HZPzjb", [job, PROP]), ("C4lTm", [job, 4, PROP]))) is True
    assert check(*post(("RVtklb", ["https://example.com/", None, 4, 9, PROP]))) is False
    assert check(*post(("CyYwI", [PROP, "https://example.com/sitemap.xml"]))) is False
    assert check(*post(("SM7Bqb", [[1, []]]))) is False
    assert check(*post(("Fj3Owf", [job, "T", None, PROP]), ("HZPzjb", [job, PROP]))) is False  # not alone
    assert check(*post(("Fj3Owf", [job, "T", None, PROP]))) is True
    assert check(*post(("Fj3Owf", [job, "T", None, PROP]))) is False  # once
    assert budget.sent == {"Fj3Owf": 1}
    assert guard.request_indexing_check(guard.Budget({}))(*post(("Fj3Owf", [job, "T", None, PROP]))) is False


@pytest.mark.parametrize("rpcid", ["oGVhvf", "gydQ5d"])
def test_page_load_reads_only_in_the_request_indexing_policy(rpcid):
    check = guard.request_indexing_check(guard.Budget({"Fj3Owf": 1}))
    assert check(*post((rpcid, [PROP]))) is True
    assert check(*post((rpcid, ["https://www.example.org/"]))) is True  # the last-used property, on the first load
    assert check(*post(("oGVhvf", [PROP]), ("gydQ5d", [PROP]))) is True  # only each other
    assert guard.read_allowed(*post((rpcid, [PROP]))) is False
    assert guard.submit_check(PROP, "https://example.com/sitemap.xml", guard.Budget({"CyYwI": 1}))(
        *post((rpcid, [PROP]))) is False


@pytest.mark.parametrize("req", [
    post(("oGVhvf", [PROP]), ("HZPzjb", ["JOB", PROP])),  # mixed with another page read
    post(("oGVhvf", [PROP]), ("zzWrit", [PROP])),  # an unknown rpcid with the same [property] shape
    post(("gydQ5d", [PROP]), ("SM7Bqb", [[1, []]])),  # mixed with a write
    post(("oGVhvf", [PROP]), ("Fj3Owf", ["JOB", "T", None, PROP])),  # never rides along with Fj3Owf
    post(("oGVhvf", [PROP]), rpcids="gydQ5d"),  # label and body disagree
    post(("oGVhvf", [])), post(("oGVhvf", [PROP, PROP])), post(("gydQ5d", ["not a property"])),
    post(("gydQ5d", [7])), post(("gydQ5d", ["sc-domain:EXAMPLE.com"])),  # not as Search Console names it
    post(("oGVhvf", [PROP]), method="GET"),
    ("POST", "https://search.google.com/_/SearchConsoleAggReportUi/data/other?rpcids=oGVhvf",
     body(("oGVhvf", [PROP]))),
    ("POST", f"https://evil.example.com{BATCH_PATH}?rpcids=oGVhvf", body(("oGVhvf", [PROP]))),
])
def test_page_load_reads_refused_mixed_misshapen_or_elsewhere(req):
    budget = guard.Budget({"Fj3Owf": 1})
    assert guard.request_indexing_check(budget)(*req) is False
    assert budget.sent == {}


def test_a_closed_budget_lets_no_late_write_out():
    budget = guard.Budget({"Fj3Owf": 1})
    check = guard.request_indexing_check(budget)
    assert budget.close() == {}
    assert check(*post(("Fj3Owf", ["JOB", "T", None, PROP]))) is False and budget.sent == {}
    budget = guard.Budget({"Fj3Owf": 1})
    assert guard.request_indexing_check(budget)(*post(("Fj3Owf", ["JOB", "T", None, PROP]))) is True
    assert budget.close() == {"Fj3Owf": 1} and budget.left == {}


@pytest.mark.parametrize(("method", "url", "quiet"), [
    ("GET", "https://www.googletagmanager.com/gtag/js?id=G-X", True),
    ("GET", "https://www.google-analytics.com/analytics.js", True),
    ("POST", "https://play.google.com/log?format=json", True),
    ("POST", "https://search.google.com/_/SearchConsoleAggReportUi/jserror?script=1", True),
    ("POST", "https://search.google.com/_/SearchConsoleAggReportUi/log", True),
    ("POST", "https://search.google.com/_/SearchConsoleAggReportUi/browserinfo", True),
    ("POST", BATCH + "?rpcids=SM7Bqb", False),  # a refused write is still listed
    ("POST", "https://play.google.com/logs", False),
    ("POST", "https://evil.example.com/_/SearchConsoleAggReportUi/jserror", False),
    ("POST", "https://ogads-pa.clients6.google.com/$rpc/x/GetAsyncData", False),
])
def test_quiet_is_only_telemetry(method, url, quiet):
    assert guard.quiet(method, url) is quiet
    assert guard.READ.quiet is not None and guard.request_indexing_policy(guard.Budget({})).quiet is not None


def test_describe_names_rpcids_not_payloads():
    method, url, data = post(("Fj3Owf", ["JOB", "SECRET-ATTESTATION", None, PROP]))
    text = guard.describe(method, url, data)
    assert "rpcids=Fj3Owf" in text and "SECRET" not in text and "TOKEN" not in text and "source-path" not in text


# Parsers

PROPERTIES = [[[PROP, 5, 1, "https://www.google.com/s2/favicons?domain=example.com", "example.com", "example.com"],
               ["https://www.example.org/", 1, 2, "", "https://www.example.org/", "example.org", False]],
              [[1, []]]]


def test_properties():
    assert parse.properties(PROPERTIES) == [
        {"property": PROP, "type": "domain", "permission": "owner", "permissionCode": 1,
         "displayName": "example.com"},
        {"property": "https://www.example.org/", "type": "url-prefix", "permission": "full", "permissionCode": 2,
         "displayName": "https://www.example.org/"}]


@pytest.mark.parametrize(("value", "out"), [
    ("example.com", PROP), ("sc-domain:Example.com", PROP), ("https://www.example.org", "https://www.example.org/"),
    ("https://www.example.org/", "https://www.example.org/")])
def test_normalize_property(value, out):
    assert parse.normalize_property(value) == out


@pytest.mark.parametrize("value", ["", "example", "ftp://example.com/", "example.com/blog", "sc-domain:"])
def test_bad_property(value):
    with pytest.raises(ValueError):
        parse.normalize_property(value)


def test_property_for_prefers_the_longest_url_prefix():
    props = [PROP, "https://www.example.com/", "https://www.example.com/blog/"]
    assert parse.property_for("https://www.example.com/blog/post", props) == "https://www.example.com/blog/"
    assert parse.property_for("https://shop.example.com/", props) == PROP
    assert parse.property_for("https://example.net/", props) is None
    assert not parse.url_in_property("https://notexample.com/", PROP)


SERIES = [[PROP, 30, []], [[[1785542400000, ["NaN", "NaN", 0], None, None, None, False],
                            [1792627200000, [4, 2, 0], None, None, None, False],
                            [1792713600000, [5, 3, 0], None, None, None, False],
                            [1792800000000, ["NaN", "NaN", 0], None, None, None, False]], None, [[True, 2]]],
          1, [0], None, None, [[4, 1, 5], None, [["INFORMATIONAL", True, 4, 1], ["OK", True, 3, 2],
                                                 ["IMPRESSIONS", False]], True], True]


def test_index_counts_take_the_last_day_with_data():
    # 1792713600000 is 2026-10-23T00:00Z: Search Console's "Last update" shows it as 10/22 (Pacific).
    assert parse.index_counts(SERIES) == {"indexed": 3, "notIndexed": 5, "lastUpdated": "2026-10-22",
                                          "asOf": "2026-10-22"}
    assert parse.index_counts([None, [[]]]) == {"indexed": None, "notIndexed": None, "lastUpdated": None,
                                                "asOf": None}


@pytest.mark.parametrize(("today", "crawls", "stale", "why"), [
    ("2026-10-25", [], False, []),
    ("2026-10-25", ["2026-10-23T06:59:00Z"], False, []),  # 10-22 23:59 Pacific: not after the update
    ("2026-10-26", [], True, ["last updated 2026-10-22, 4 days ago"]),
    ("2026-10-24", ["2026-10-24T06:43:05Z", None, "2026-10-23T08:00:00Z"], True,
     ["2 example URL(s) crawled after it, up to 2026-10-23"]),
])
def test_staleness(today, crawls, stale, why):
    from datetime import date
    assert parse.staleness("2026-10-22", crawls, date.fromisoformat(today)) == (stale, why)
    assert parse.staleness(None, [], date.fromisoformat(today))[0] is True


def test_index_filters():
    from webcli.sites.gsc.endpoints import INDEX_FILTERS, index_filters
    assert INDEX_FILTERS == [[12, *([None] * 10), 3], [9, ""]]
    assert index_filters(2) == [[12, *([None] * 10), 2], [9, ""]]
    assert index_filters(1, "https://example.com/sitemap.xml")[1] == [9, "https://example.com/sitemap.xml"]


def reason(key, enum, anchor, pages):
    return [[["INFORMATIONAL", None, None, None, None, None, 4, None, 4], [key], [None],
             [None, None, False, None, None, None, 3, None, None, [], 3, None, None, [1, 3, True], None, None, None,
              f"https://support.google.com/webmasters/answer/7440203#{anchor}", None,
              [None, "INFORMATIONAL", enum, None, None, None, None, None, [3, None, 8, 2], None, None, 4]],
             [None, None, None, None, None, None, None, [[30], [[1792713600000, pages]]], 4], [None, pages]]]


REASONS = [[PROP, 35, []], [[reason("KEY1", "NO_INDEX_TAG", "blocked_by_noindex_tag", 3),
                             reason("KEY2", "DISCOVERED", "discovered__unclear_status", 11),
                             reason("KEY3", "SOFT_404_NEW", "soft_404", 0)]]]


def test_reasons():
    rows = parse.reasons(REASONS)
    assert [(r["reason"], r["source"], r["key"], r["pages"]) for r in rows] == [
        ("Excluded by 'noindex' tag", "Website", "KEY1", 3),
        ("Discovered - currently not indexed", "Google systems", "KEY2", 11),
        ("Soft 404", None, "KEY3", 0)]  # an enum webcli doesn't know: its help anchor, humanized


URLS = [[PROP, 33], [[[[["https://example.com/a", None, True], [None, 1792801906]]],
                      [[["https://example.com/b", None, True], [None, 0]]]]]]


def test_reason_urls():
    assert parse.reason_urls(URLS) == [{"url": "https://example.com/a", "lastCrawled": "2026-10-24T00:31:46Z"},
                                       {"url": "https://example.com/b", "lastCrawled": None}]


SITEMAPS = [[["https://example.com/sitemap.xml", 1, 1791492955664, 1793374470540, [[1, 15], [2, 1]], 0, 2, None, 1,
              True, None, False, False]], PROP]


def test_sitemaps():
    assert parse.sitemaps(SITEMAPS) == [{
        "sitemap": "https://example.com/sitemap.xml", "type": "Sitemap", "submitted": "2026-10-08T20:55:55Z",
        "lastRead": "2026-10-30T15:34:30Z", "status": "Success", "statusCode": 1, "discoveredPages": 15,
        "discoveredVideos": 1, "errors": 0, "warnings": 2}]


def test_check_sitemap_url():
    assert parse.check_sitemap_url(" https://example.com/sitemap.xml ", PROP) == "https://example.com/sitemap.xml"
    for bad in ("https://example.net/sitemap.xml", "sitemap.xml", "https://user@example.com/s.xml"):
        with pytest.raises(ValueError):
            parse.check_sitemap_url(bad, PROP)


INDEXED = [[1, "JOB", False], 1792581574000, ["https://example.com/sitemap.xml"], ["https://example.org/"], None,
           [None, None, 2], 1, 1, None, 1, "KEY", None, 1, 0,
           "https://support.google.com/webmasters/answer/7440203#submitted_and_indexed", ["https://example.com/", 1],
           2, None, None, None, None, 0, 0, 0]
ALTERNATE = [[5, "JOB", False], 1792801906000, None, None, None, [None, None, 2], 1, 1, ["https://example.com/", 1],
             19, "KEY", None, 2, 0,
             "https://support.google.com/webmasters/answer/7440203#duplicate_page_with_proper_canonical_tag",
             ["https://example.com/", 2], 2, None, None, None, None, 1, 0, 0]
UNKNOWN = [[5, "JOB", False], 0, None, None, None, [None, None, 0], 0, 0, None, 77, "KEY", None, 0, 0,
           "https://support.google.com/webmasters/answer/9012289#url_unknown", ["", 0], 0]


def test_inspection_indexed():
    out = parse.inspection(INDEXED, "https://example.com/", PROP)
    assert out["onGoogle"] is True and out["coverageState"] == "Submitted and indexed"
    assert out["lastCrawl"] == "2026-10-21T11:19:34Z" and out["crawledAs"] == "Googlebot smartphone"
    assert out["userCanonical"] == out["googleCanonical"] == "https://example.com/"
    assert out["inSitemap"] is True and out["referringUrls"] == ["https://example.org/"]
    assert (out["crawlAllowed"], out["pageFetch"]) == (True, "Successful")


def test_inspection_notes():
    assert parse.inspection_notes(parse.inspection(INDEXED, "https://example.com/", PROP)) == []
    no_sitemap = list(INDEXED)
    no_sitemap[2], no_sitemap[3] = None, ["https://example.com/sitemap.xml", "https://example.com/blog/"]
    info = parse.inspection(no_sitemap, "https://example.com/", PROP)
    notes = parse.inspection_notes(info)
    assert len(notes) == 2 and "inSitemap is false" in notes[0] and "Submitted and indexed" in notes[0]
    assert "referringUrls includes https://example.com/sitemap.xml:" in notes[1]
    assert all("passed through" in n for n in notes)
    assert "caught up" in parse.inspection_notes({**info, "inLiveSitemap": True})[-1]
    assert "don't list" in parse.inspection_notes({**parse.inspection(INDEXED, "https://example.com/", PROP),
                                                   "inLiveSitemap": False})[-1]
    assert parse.inspection_notes(parse.inspection(UNKNOWN, "https://example.com/new", PROP)) == []


def test_inspection_alternate_and_unknown():
    alt = parse.inspection(ALTERNATE, "https://www.example.com/", PROP)
    assert alt["onGoogle"] is False and alt["coverageState"] == "Alternate page with proper canonical tag"
    assert alt["userCanonical"] == alt["googleCanonical"] == "https://example.com/" and alt["inSitemap"] is False
    unknown = parse.inspection(UNKNOWN, "https://example.com/new", PROP)
    assert unknown["coverageState"] == "URL is unknown to Google"  # from the help anchor
    assert unknown["lastCrawl"] is None and unknown["userCanonical"] is None and unknown["googleCanonical"] is None


def metric(mid, value, *, missing=False):
    if mid in (5, 6):
        return [None, value, None, None, None, None, None, None, mid]
    entry = [None] * 45
    entry[8] = mid
    if missing:
        entry[2], entry[6] = True, -2147483648
    else:
        entry[44 if mid == 7 else 43] = value
    return entry


def perf_row(key, clicks, impr, pos, *, missing=False):
    return [[key, metric(5, clicks), metric(6, impr), metric(7, 0, missing=missing),
             metric(8, pos, missing=missing)]]


def day(ms):
    key = [None] * 46
    key[6], key[45] = ms, [[ms // 1000], 0]
    return key


def page_key(url):
    key = [None] * 41
    key[16], key[40] = True, url
    return key


def test_performance_query_page_and_date():
    q = [None, [[perf_row(["example query", *([None] * 15), True], 1, 8, 1.375),
                 perf_row(["other query"], 3, 4, 6)]]]
    assert parse.performance(q, "query") == [
        {"query": "other query", "clicks": 3, "impressions": 4, "ctr": 0.75, "position": 6},
        {"query": "example query", "clicks": 1, "impressions": 8, "ctr": 0.125, "position": 1.38}]
    p = [None, [[perf_row(page_key("https://example.com/"), 0, 19, 6.7368)]]]
    assert parse.performance(p, "page")[0]["page"] == "https://example.com/"
    d = [None, [[perf_row(day(1793188800000), 0, 2, 8), perf_row(day(1793102400000), 0, 0, None, missing=True)]]]
    assert parse.performance(d, "date") == [
        {"date": "2026-10-27", "clicks": 0, "impressions": 0, "ctr": 0.0, "position": None},
        {"date": "2026-10-28", "clicks": 0, "impressions": 2, "ctr": 0.0, "position": 8}]


def test_performance_request_shape():
    from datetime import date
    req = parse.performance_request(PROP, 2, date(2026, 9, 1), date(2026, 10, 28))
    assert req[:2] == [PROP, 32] and req[5][0] == [2] and req[5][2] == [["20260901", "20261028"]]


PERF_SERIES = [[PROP, 27], [[[1793102400000, [0, 30, 0, 7.1]], [1793188800000, [1, 20, 0.05, 8.0]]],
                           [None, [1, 50, 0.02, 7.4012]], None, 1793363530000], None, None, None, None,
               [None, None, [["CLICKS"], ["IMPRESSIONS"], ["CTR"], ["POSITION"]]]]


def test_performance_totals():
    assert parse.performance_totals(PERF_SERIES) == {
        "totals": {"clicks": 1, "impressions": 50, "ctr": 0.02, "position": 7.4},
        "lastUpdated": "2026-10-30T12:32:10Z"}
    empty = [None, [[], [None, [0, 0, 0, 0]], None, None]]
    assert parse.performance_totals(empty)["totals"] == {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": None}
    with pytest.raises(ValueError):
        parse.performance_totals([None, None])
    from datetime import date
    req = parse.performance_series_request(PROP, date(2026, 10, 1), date(2026, 10, 28))
    assert req[:2] == [PROP, 27] and req[5][2] == [["20261001", "20261028"]] and req[-1] == [None, 2]


def test_rpc_payload_errors():
    assert parse.rpc_payload(answer("BWF8he", [1]), "BWF8he") == [1]
    with pytest.raises(parse.RpcError) as exc:
        parse.rpc_payload(answer("nDAfwb", None), "nDAfwb")
    assert exc.value.code == 3


def test_rpc_error_summarizes_an_answer_without_its_body():
    assert parse.rpc_error(answer("Fj3Owf", []), "Fj3Owf") is None
    assert parse.rpc_error(answer("Fj3Owf", None), "Fj3Owf") == "code 3"
    assert parse.rpc_error(answer("HZPzjb", []), "Fj3Owf") == "no Fj3Owf result"
    assert parse.rpc_error("<html>Error 500</html>", "Fj3Owf") == "no Fj3Owf result"
    bad = ")]}'\n\n" + json.dumps([["wrb.fr", "Fj3Owf", "{not json", None, None, None, "generic"]])
    assert parse.rpc_error(bad, "Fj3Owf") == "unreadable"


@pytest.mark.parametrize(("shown", "where", "sent", "status", "error", "outcome"), [
    ("done", "dialog", 1, 200, None, "confirmed"),
    ("done", "dialog", 1, None, None, "confirmed"),  # answer unseen: Google shows the dialog only after it
    ("done", "page", 1, 200, None, "confirmed"),  # text outside a dialog needs the clean answer
    ("done", "page", 1, None, None, "unconfirmed"),
    ("done", "dialog", 1, 200, "code 3", "unconfirmed"),  # dialog and answer disagree
    ("done", "dialog", 1, 200, "unreadable", "unconfirmed"),
    ("done", "dialog", 1, 500, "no Fj3Owf result", "unconfirmed"),
    ("failed", "dialog", 1, 200, None, "unconfirmed"),
    ("failed", "dialog", 1, 200, "code 13", "unconfirmed"),
    (None, None, 1, None, None, "unconfirmed"),  # timeout after Fj3Owf went out
    (None, None, 0, None, None, "not_sent"),  # timeout before
    ("rejected", "dialog", 0, None, None, "not_sent"),  # the live test found issues
    ("failed", "dialog", 0, None, None, "not_sent"),
    ("done", "dialog", 0, None, None, "not_sent"),
    ("quota", "dialog", 1, 200, "code 8", "quota"),
    ("quota", "dialog", 0, None, None, "quota"),
    ("failed", "dialog", 1, 200, "code 8", "quota"),
])
def test_ri_outcome(shown, where, sent, status, error, outcome):
    assert parse.ri_outcome(shown, where, sent, status, error) == outcome


BASE = {"dialog": fetch._counts(""), "page": fetch._counts("")}


def test_ri_result_comes_from_the_dialog_only():
    side = "Performance\nSomething went wrong\nURL inspection"  # a side panel outside the dialog
    assert fetch.ri_shown("", side, BASE) is None
    done = "Indexing requested\nURL was added to a priority crawl queue. Learn more.\nDismiss"
    assert fetch.ri_shown(done, "", BASE)[0::2] == ("done", "dialog")
    failed = "Oops! Something went wrong\nWe had a problem submitting your indexing request.\nDismiss"
    assert fetch.ri_shown(failed, "", BASE) == ("failed", " ".join(failed.split()), "dialog")
    assert fetch.ri_shown("Quota Exceeded\nSorry--we couldn't process this request", "", BASE)[0] == "quota"
    rejected = "Indexing request rejected\nDuring live testing, indexing issues were detected with the URL"
    assert fetch.ri_shown(rejected, "", BASE)[0] == "rejected"
    assert fetch.ri_shown("Testing if live URL can be indexed\nThis might take a minute or two", side, BASE) is None
    assert fetch.ri_shown("", "Status: Indexing requested", BASE) == ("done", "Indexing requested", "page")
    before = {"dialog": BASE["dialog"], "page": fetch._counts("Indexing requested")}
    assert fetch.ri_shown("", "Indexing requested", before) is None  # already on the page before the click


DISCOVERED = [[5, "JOB", False], 0, ["https://example.com/sitemap.xml"], None, None, [None, None, 0], 0, 0, None, 5,
              "KEY", None, 0, 0, "https://support.google.com/webmasters/answer/7440203#discovered__unclear_status",
              ["", 0], 0]


def test_needs_recheck_and_notes():
    unknown = parse.inspection(UNKNOWN, "https://example.com/new", PROP)
    found = parse.inspection(DISCOVERED, "https://example.com/new", PROP)
    indexed = parse.inspection(INDEXED, "https://example.com/", PROP)
    alternate = parse.inspection(ALTERNATE, "https://www.example.com/", PROP)
    assert parse.needs_recheck(unknown) and parse.needs_recheck(alternate)
    assert not parse.needs_recheck(found) and not parse.needs_recheck(indexed)
    assert parse.pick_answer(unknown, found) is found and parse.pick_answer(found, unknown) is found
    live = {"inLiveSitemap": True, "liveSitemaps": ["https://example.com/sitemap.xml"]}
    note = parse.recheck_note(unknown, found, live)
    assert note == ("Google's stored data said this URL is unknown, but it is in your live sitemap "
                    "(https://example.com/sitemap.xml). Google's answers can lag or flip between calls; webcli asked "
                    "again and got: Discovered - currently not indexed.")
    assert "still says it is unknown. Run inspect again later." in parse.recheck_note(unknown, unknown, live)
    assert "set aside" in parse.recheck_note(alternate, unknown, live)
    assert "--check-live-sitemap" in parse.recheck_note(unknown, None, None)
    assert "don't list it" in parse.recheck_note(unknown, None, {"inLiveSitemap": False, "liveSitemaps": []})
    assert "liveSitemapErrors" in parse.recheck_note(unknown, None, {"inLiveSitemap": None, "liveSitemaps": []})
    assert parse.recheck_note(alternate, None, {"inLiveSitemap": False}) is None


# fetch, on a fake page

class FakePage:
    def __init__(self, answers, *, landed="https://search.google.com/search-console", status=200, challenge=False,
                 http=200):
        self.answers, self.landed, self.status, self.challenge, self.http = answers, landed, status, challenge, http
        self.calls, self.url = [], ""

    async def goto(self, url, **_):
        self.url = self.landed
        return SimpleNamespace(status=self.status)

    async def wait_for_function(self, *_a, **_k):
        return True

    async def evaluate(self, script, arg=None):
        if script == fetch.CHALLENGE_JS:
            return self.challenge
        if script == fetch._CALL:
            rid = arg["rpcid"]
            self.calls.append((rid, json.loads(json.loads(arg["freq"])[0][0][1])))
            value = self.answers[rid]
            if isinstance(value, list) and value[:1] == ["SEQ"]:  # answers in turn, the last one repeating
                payload = value.pop(1) if len(value) > 2 else value[1]
            else:
                payload = value
            return {"status": self.http, "body": answer(rid, payload)}
        return None


@pytest.fixture
def page(monkeypatch):
    holder = {}

    @asynccontextmanager
    async def site_page(policy, **_):
        holder["policy"] = policy
        yield holder["page"]

    monkeypatch.setattr(browser, "site_page", site_page)
    monkeypatch.setattr(fetch, "POLL_SECONDS", 0)

    def make(answers, **kwargs):
        holder["page"] = FakePage(answers, **kwargs)
        return holder["page"]

    make.holder = holder
    return make


def test_properties_and_sitemaps(page):
    p = page({"BWF8he": PROPERTIES, "vyvePb": SITEMAPS})
    assert asyncio.run(fetch.properties())[0]["property"] == PROP
    assert asyncio.run(fetch.sitemaps("example.com"))[0]["discoveredPages"] == 15
    assert p.calls[-1] == ("vyvePb", [PROP, PROP])
    with pytest.raises(NotFound):
        asyncio.run(fetch.sitemaps("example.net"))


def test_pages_fetches_examples_only_for_reasons_with_pages(page):
    p = page({"BWF8he": PROPERTIES, "OLiH4d": SERIES, "nDAfwb": ["SEQ", REASONS, URLS, URLS]})
    out = asyncio.run(fetch.pages(PROP, 1))
    assert (out["indexed"], out["notIndexed"]) == (3, 5)
    assert [len(r["examples"]) for r in out["reasons"]] == [1, 1, 0]
    assert [c[1][1] for c in p.calls if c[0] == "nDAfwb"] == [35, 33, 33]


@pytest.fixture
def today(monkeypatch):
    from datetime import date
    monkeypatch.setattr(parse, "report_today", lambda: date(2026, 10, 30))


def test_pages_reports_stale_and_truncated_examples(page, today):
    p = page({"BWF8he": PROPERTIES, "OLiH4d": SERIES, "nDAfwb": ["SEQ", REASONS, URLS, URLS, URLS]})
    out = asyncio.run(fetch.pages(PROP, 5, None, True))
    assert out["filter"] == {"pages": "all-known", "sitemap": None} and out["total"] == 8
    assert out["lastUpdated"] == out["asOf"] == "2026-10-22"
    assert out["stale"] is True and len(out["staleReasons"]) == 2 and "lag URL Inspection" in out["warning"]
    r = out["reasons"][0]
    assert (len(r["examples"]), r["examplesAvailable"], r["examplesTruncated"]) == (2, 2, True)  # 2 of 3
    assert out["reasons"][2]["examplesTruncated"] is False  # 0 pages
    assert (len(out["indexedExamples"]), out["indexedExamplesTruncated"]) == (2, True)  # 2 of 5
    indexed_call = [c[1] for c in p.calls if c[0] == "nDAfwb"][-1]
    assert indexed_call[2][0] == [4] and indexed_call[-1] == [None, None, [13, "valid"]]


def test_pages_sitemap_filters(page, today):
    p = page({"BWF8he": PROPERTIES, "OLiH4d": SERIES, "nDAfwb": ["SEQ", REASONS, URLS], "vyvePb": SITEMAPS})
    out = asyncio.run(fetch.pages(PROP, 0, "all"))
    assert out["filter"] == {"pages": "all-submitted", "sitemap": None}
    assert [c for c in p.calls if c[0] == "OLiH4d"][-1][1][2][0][-1] == 2
    sm = "https://example.com/sitemap.xml"
    out = asyncio.run(fetch.pages(PROP, 0, sm))
    assert out["filter"] == {"pages": "sitemap", "sitemap": sm}
    series = [c for c in p.calls if c[0] == "OLiH4d"][-1][1]
    assert series[2] == [[12, *([None] * 10), 1], [9, sm]]
    assert [c for c in p.calls if c[0] == "nDAfwb"][-1][1][2][:2] == series[2]
    with pytest.raises(UsageError) as exc:
        asyncio.run(fetch.pages(PROP, 0, "https://example.com/other.xml"))
    assert sm in str(exc.value)


def test_performance_returns_totals_and_rows(page):
    q = [None, [[perf_row(["q1"], 1, 8, 1.0), perf_row(["q2"], 0, 4, 6), perf_row(["q3"], 0, 1, 2)]]]
    p = page({"BWF8he": PROPERTIES, "OLiH4d": PERF_SERIES, "nDAfwb": q})
    out = asyncio.run(fetch.performance(PROP, "2026-10-01", "2026-10-28", "query", 2))
    assert {k: out[k] for k in ("property", "start", "end", "dimension", "searchType")} == {
        "property": PROP, "start": "2026-10-01", "end": "2026-10-28", "dimension": "query", "searchType": "web"}
    assert out["totals"]["impressions"] == 50 and len(out["rows"]) == 2 and out["rowsTruncated"] is True
    assert [c[1][1] for c in p.calls if c[0] == "OLiH4d"] == [27]


def test_inspect_checks_the_live_sitemap(page, monkeypatch):
    seen = []

    def check(url, sitemaps, prop):
        seen.append((url, sitemaps, prop))
        return {"inLiveSitemap": True, "liveSitemaps": sitemaps, "liveSitemapsRead": 1, "liveSitemapUrls": 3,
                "liveSitemapErrors": []}
    monkeypatch.setattr(fetch.livesitemap, "check", check)
    no_sitemap = list(INDEXED)
    no_sitemap[2] = None
    page({"BWF8he": PROPERTIES, "RVtklb": ["JOB", 1, [60]], "MrNfbc": ["JOB", 2, [], 2], "HZPzjb": no_sitemap,
          "vyvePb": SITEMAPS})
    out = asyncio.run(fetch.inspect("https://example.com/", None, True))
    assert seen == [("https://example.com/", ["https://example.com/sitemap.xml"], PROP)]
    assert out["inSitemap"] is False and out["inLiveSitemap"] is True
    assert len(out["notes"]) == 2 and "caught up" in out["notes"][1]
    plain = asyncio.run(fetch.inspect("https://example.com/"))
    assert "inLiveSitemap" not in plain and len(plain["notes"]) == 1 and len(seen) == 1


def test_inspect_polls_until_done_and_never_live_tests(page):
    p = page({"BWF8he": PROPERTIES, "RVtklb": ["JOB", 1, [60]],
              "MrNfbc": ["SEQ", ["JOB", 1, [], 2], ["JOB", 2, [], 2]], "HZPzjb": INDEXED})
    out = asyncio.run(fetch.inspect("https://example.com/"))
    assert out["property"] == PROP and out["onGoogle"] is True
    assert p.calls[1] == ("RVtklb", ["https://example.com/", None, 4, 2, PROP])
    assert [c[0] for c in p.calls].count("MrNfbc") == 2
    with pytest.raises(UsageError):
        asyncio.run(fetch.inspect("https://example.net/", "example.com"))


LIVE = "https://example.com/sitemap.xml"


@pytest.fixture
def live(monkeypatch):
    seen, answer_ = [], {"inLiveSitemap": True}

    def check(url, sitemaps, prop):
        seen.append(url)
        found = answer_["inLiveSitemap"]
        return {"inLiveSitemap": found, "liveSitemaps": [LIVE] if found else [], "liveSitemapsRead": 1,
                "liveSitemapUrls": 3, "liveSitemapErrors": []}
    monkeypatch.setattr(fetch.livesitemap, "check", check)
    return seen, answer_


def test_inspect_rechecks_an_unknown_url_in_the_live_sitemap(page, live):
    seen, _ = live
    p = page({"BWF8he": PROPERTIES, "RVtklb": ["JOB", 1, [60]], "MrNfbc": ["JOB", 2, [], 2],
              "HZPzjb": ["SEQ", UNKNOWN, DISCOVERED], "vyvePb": SITEMAPS})
    out = asyncio.run(fetch.inspect("https://example.com/new"))
    assert out["coverageState"] == "Discovered - currently not indexed" and out["inSitemap"] is True
    assert out["recheckedAfterUnknown"] is True and out["inLiveSitemap"] is True and seen == ["https://example.com/new"]
    assert [c[0] for c in p.calls].count("RVtklb") == 2
    assert out["notes"] == [parse.recheck_note(parse.inspection(UNKNOWN, "https://example.com/new", PROP),
                                               parse.inspection(DISCOVERED, "https://example.com/new", PROP),
                                               {"liveSitemaps": [LIVE]})]
    assert "webcli asked again and got: Discovered - currently not indexed." in out["notes"][0]


def test_inspect_still_unknown_or_not_in_the_live_sitemap(page, live):
    seen, answer_ = live
    p = page({"BWF8he": PROPERTIES, "RVtklb": ["JOB", 1, [60]], "MrNfbc": ["JOB", 2, [], 2], "HZPzjb": UNKNOWN,
              "vyvePb": SITEMAPS})
    out = asyncio.run(fetch.inspect("https://example.com/new"))
    assert out["coverageState"] == "URL is unknown to Google" and out["recheckedAfterUnknown"] is True
    assert "still says it is unknown" in out["notes"][0] and len(out["notes"]) == 1  # no "caught up" note too
    answer_["inLiveSitemap"] = False
    p.calls.clear()
    out = asyncio.run(fetch.inspect("https://example.com/new"))
    assert out["recheckedAfterUnknown"] is False and [c[0] for c in p.calls].count("RVtklb") == 1
    assert "don't list it" in out["notes"][0]
    p.calls.clear()
    out = asyncio.run(fetch.inspect("https://example.com/new", None, False, False))  # --no-live-sitemap
    assert "vyvePb" not in [c[0] for c in p.calls] and "inLiveSitemap" not in out and len(seen) == 2
    assert out["recheckedAfterUnknown"] is False and "--check-live-sitemap" in out["notes"][0]


def test_inspect_skips_the_live_sitemap_when_google_knows_the_url(page, live):
    seen, _ = live
    page({"BWF8he": PROPERTIES, "RVtklb": ["JOB", 1, [60]], "MrNfbc": ["JOB", 2, [], 2], "HZPzjb": DISCOVERED})
    out = asyncio.run(fetch.inspect("https://example.com/new"))
    assert seen == [] and out["recheckedAfterUnknown"] is False and out["notes"] == []


class RIPage(FakePage):
    """URL Inspection's page for REQUEST INDEXING. The click sends Fj3Owf through the real request-indexing policy
    (so the guard's budget counts it) when `sends`, answers it with `fj3owf` (status, payload; None payload: an error
    envelope), then shows `dialog` in a modal and `side` elsewhere on the page."""

    def __init__(self, *, policy, dialog="", side="", sends=True, fj3owf=(200, [])):
        super().__init__({"BWF8he": PROPERTIES})
        self.policy, self.dialog_after, self.side, self.sends, self.fj3owf = policy, dialog, side, sends, fj3owf
        self.dialog, self.handlers, self.clicked = "", [], 0
        self.text = "URL is not on Google\nREQUEST INDEXING\nPage indexing"

    def on(self, event, handler):
        assert event == "response"
        self.handlers.append(handler)

    def locator(self, _selector):
        async def nothing(_value):
            pass
        return SimpleNamespace(first=SimpleNamespace(fill=nothing, press=nothing))

    def get_by_text(self, _pattern):
        return SimpleNamespace(first=SimpleNamespace(click=self.click_request_indexing))

    async def click_request_indexing(self):
        self.clicked += 1
        method, url, data = post(("Fj3Owf", ["JOB", "ATTESTATION", None, PROP]))
        request = SimpleNamespace(method=method, url=url, post_data=data)
        if self.sends and self.policy()(request, None):
            status, payload = self.fj3owf

            async def text():
                return answer("Fj3Owf", payload)
            for handler in self.handlers:
                handler(SimpleNamespace(request=request, status=status, text=text))
        self.dialog, self.text = self.dialog_after, self.text + "\n" + self.side

    async def evaluate(self, script, arg=None):
        if script == fetch.DIALOG_JS:
            return self.dialog
        if script == fetch.TEXT_JS:
            return self.text
        return await super().evaluate(script, arg)


@pytest.fixture
def ri_page(page, monkeypatch):
    holder = page.holder

    def make(**kwargs):
        holder["page"] = RIPage(policy=lambda: holder["policy"].allowed, **kwargs)
        return holder["page"]
    monkeypatch.setattr(fetch, "REQUEST_INDEXING_TIMEOUT_S", 0.01)
    return make


def request_indexing():
    return asyncio.run(fetch.request_indexing("https://example.com/", PROP, confirm=True))


DONE = "Indexing requested\nURL was added to a priority crawl queue.\nDismiss"
FAILED = "Oops! Something went wrong\nWe had a problem submitting your indexing request. Please try again later."


def test_request_indexing_confirmed_by_the_dialog_despite_a_side_panel_error(ri_page):
    p = ri_page(dialog=DONE, side="Something went wrong")
    out = request_indexing()
    assert p.clicked == 1 and page_policy_is_request_indexing(p)
    assert {k: out[k] for k in ("requested", "outcome", "confirmed", "fj3owfSent", "fj3owfStatus", "fj3owfError")} \
        == {"requested": True, "outcome": "confirmed", "confirmed": "dialog", "fj3owfSent": 1, "fj3owfStatus": 200,
            "fj3owfError": None}
    assert out["message"].startswith("Indexing requested: the request left the browser (Fj3Owf sent once); Google "
                                     "answered HTTP 200; the dialog said \"Indexing requested")


def page_policy_is_request_indexing(p):
    method, url, data = post(("oGVhvf", [PROP]))
    return p.policy()(SimpleNamespace(method=method, url=url, post_data=data), None)


@pytest.mark.parametrize(("kwargs", "outcome", "error", "says"), [
    ({"dialog": FAILED}, "unconfirmed", None, 'Google answered HTTP 200; the dialog said "Oops! Something went wrong'),
    ({"dialog": DONE, "fj3owf": (200, None)}, "unconfirmed", "code 3", "Google answered HTTP 200, error code 3"),
    ({"dialog": FAILED, "fj3owf": (500, [])}, "unconfirmed", None, "Google answered HTTP 500;"),
    ({"dialog": ""}, "unconfirmed", None, "no result showed in 0.01s"),  # timeout after Fj3Owf went out
    ({"dialog": "Indexing request rejected\nDuring live testing", "sends": False}, "not_sent", None,
     "the request never left the browser (Fj3Owf not sent), so nothing was submitted"),
    ({"dialog": FAILED, "sends": False}, "not_sent", None, "never left the browser"),
    ({"dialog": "Quota Exceeded\nyou've exceeded your daily quota"}, "quota", None, "quota exceeded for today"),
])
def test_request_indexing_outcomes(ri_page, kwargs, outcome, error, says):
    ri_page(**kwargs)
    out = request_indexing()
    assert out["outcome"] == outcome and out["requested"] is False and out["confirmed"] is None
    assert out["fj3owfError"] == error and says in out["message"]
    if outcome == "unconfirmed":
        assert out["fj3owfSent"] == 1 and "Google may or may not have queued it; check URL Inspection in Search " \
                                          "Console before retrying." in out["message"]
    assert "request sent: True" not in out["message"]


def test_request_indexing_never_reads_a_body_into_its_result(ri_page):
    ri_page(dialog=DONE, fj3owf=(200, ["SECRET-ANSWER"]))
    out = request_indexing()
    assert out["outcome"] == "confirmed" and "SECRET" not in json.dumps(out) and "ATTESTATION" not in json.dumps(out)


@pytest.mark.parametrize(("kwargs", "error"), [
    ({"landed": "https://accounts.google.com/signin"}, AuthRequired),
    ({"landed": "https://search.google.com/search-console/about"}, AuthRequired),
    ({"status": 429}, fetch.Blocked), ({"challenge": True}, fetch.Blocked),
    ({"landed": "https://www.google.com/sorry/index"}, fetch.Blocked), ({"http": 403}, fetch.Blocked)])
def test_sign_in_and_challenges_stop(page, kwargs, error):
    page({"BWF8he": PROPERTIES}, **kwargs)
    with pytest.raises(error):
        asyncio.run(fetch.properties())


def test_writes_refuse_without_confirm_before_any_page(page):
    with pytest.raises(UsageError):
        asyncio.run(fetch.submit_sitemap(PROP, "https://example.com/sitemap.xml"))
    with pytest.raises(UsageError):
        asyncio.run(fetch.request_indexing("https://example.com/"))
    assert "page" not in page.holder


def test_submit_sitemap_reads_it_back(page):
    p = page({"BWF8he": PROPERTIES, "CyYwI": [], "vyvePb": SITEMAPS})
    out = asyncio.run(fetch.submit_sitemap("example.com", "https://example.com/sitemap.xml", confirm=True))
    assert out["submitted"] and out["listed"] and out["row"]["status"] == "Success"
    assert ("CyYwI", [PROP, "https://example.com/sitemap.xml"]) in p.calls
    assert page.holder["policy"].site == "gsc"


def test_registered_as_google():
    site = registry()["gsc"]
    assert site.provider == "google" and "request_indexing" in site.ops and site.login is None


# The commands, over a faked daemon

@pytest.fixture
def calls(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBCLI_HOME", str(tmp_path))
    seen, answers = [], {}

    async def call(ident, site, op, *args, **kwargs):
        seen.append((op, args, kwargs))
        return answers[op]

    monkeypatch.setattr(daemon, "call", call)
    return seen, answers, tmp_path


def run(*args):
    return CliRunner().invoke(webcli_app.build(), ["gsc", *args])


def audit_lines(home):
    path = home / "logs" / "gsc.log"
    return path.read_text().splitlines() if path.exists() else []


def test_read_commands(calls):
    seen, answers, _ = calls
    answers["properties"] = parse.properties(PROPERTIES)
    out = run("properties", "--json")
    assert out.exit_code == 0 and json.loads(out.stdout)[0]["property"] == PROP
    totals = {"clicks": 1, "impressions": 2, "ctr": 0.5, "position": 3}
    answers["performance"] = {"property": PROP, "searchType": "web", "start": "2026-09-01", "end": "2026-10-28",
                              "dimension": "query", "lastUpdated": None, "totals": totals,
                              "rows": [{"query": "q", **totals}], "rowsTruncated": False}
    out = run("performance", "example.com", "--start", "2026-09-01", "--end", "2026-10-28", "--dimension", "query",
              "--limit", "5", "--json")
    assert out.exit_code == 0 and seen[-1][:2] == ("performance", (PROP, "2026-09-01", "2026-10-28", "query", 5))
    got = json.loads(out.stdout)
    assert (got["property"], got["start"], got["end"], got["dimension"]) == (PROP, "2026-09-01", "2026-10-28",
                                                                             "query")
    assert got["totals"] == totals and got["rows"][0]["query"] == "q"
    out = run("performance", PROP, "--json", "--select", "query,clicks")
    assert json.loads(out.stdout)["rows"] == [{"query": "q", "clicks": 1}]
    out = run("performance", PROP, "--format", "table")
    assert out.exit_code == 0 and "totals" in out.stderr and "2 impressions" in out.stderr
    answers["performance"] = {**answers["performance"], "rows": []}
    out = run("performance", PROP, "--json")
    assert out.exit_code == 0 and "anonymized" in json.loads(out.stdout)["note"]
    answers["performance"] = {**answers["performance"], "totals": {**totals, "clicks": 0, "impressions": 0}}
    assert run("performance", PROP, "--json").exit_code == 3
    assert run("performance", PROP, "--start", "2026-10-28", "--end", "2026-09-01").exit_code == 2
    assert run("performance", PROP, "--start", "10/28/2026").exit_code == 2
    answers["sitemaps"] = parse.sitemaps(SITEMAPS)
    assert run("sitemaps", PROP, "--json").exit_code == 0 and seen[-1][:2] == ("sitemaps", (PROP,))
    answers["inspect"] = parse.inspection(INDEXED, "https://example.com/", PROP)
    out = run("inspect", "https://example.com/", "--json")
    assert out.exit_code == 0 and seen[-1][:2] == ("inspect", ("https://example.com/", None, False, True))
    run("inspect", "https://example.com/", "--check-live-sitemap", "--json")
    assert seen[-1][:2] == ("inspect", ("https://example.com/", None, True, True))
    run("inspect", "https://example.com/", "--no-live-sitemap", "--json")
    assert seen[-1][:2] == ("inspect", ("https://example.com/", None, False, False))
    n = len(seen)
    assert run("inspect", "https://example.com/", "--check-live-sitemap", "--request-indexing").exit_code == 2
    assert run("inspect", "https://example.com/", "--no-live-sitemap", "--request-indexing").exit_code == 2
    assert run("inspect", "https://example.com/", "--check-live-sitemap", "--no-live-sitemap").exit_code == 2
    assert len(seen) == n
    answers["inspect"] = {**answers["inspect"], "notes": ["a note"]}
    out = run("inspect", "https://example.com/", "--format", "table")
    assert out.exit_code == 0 and "note: a note" in out.stderr
    report = {"property": PROP, "filter": {"pages": "all-known", "sitemap": None}, "indexed": 1, "notIndexed": 0,
              "total": 1, "lastUpdated": "2026-10-22", "asOf": "2026-10-22", "stale": False, "staleReasons": [],
              "warning": None, "examplesLimit": 0, "reasons": []}
    answers["pages"] = report
    assert run("pages", "example.com", "--examples", "0", "--json").exit_code == 0
    assert seen[-1][:2] == ("pages", (PROP, 0, None, False))
    assert run("pages", PROP, "--sitemap", "all", "--indexed", "--json").exit_code == 0
    assert seen[-1][:2] == ("pages", (PROP, 5, "all", True))
    n = len(seen)
    assert run("pages", PROP, "--sitemap", "https://example.net/sitemap.xml").exit_code == 2 and len(seen) == n
    answers["pages"] = {**report, "stale": True, "warning": "stale!"}
    out = run("pages", PROP, "--format", "table")
    assert out.exit_code == 0 and "warning: stale!" in out.stderr and "stale!" not in out.stdout
    out = run("pages", PROP, "--json")
    assert json.loads(out.stdout)["stale"] is True and "warning: stale!" in out.stderr


def test_sitemaps_submit_refuses_without_yes(calls):
    seen, _, home = calls
    out = run("sitemaps", "submit", "example.com", "https://example.com/sitemap.xml")
    assert out.exit_code == 2 and seen == []
    plan = json.loads(out.stdout)
    assert plan["dryRun"] is True and plan["sitemap"] == "https://example.com/sitemap.xml" and plan["property"] == PROP
    assert "submit sitemap https://example.com/sitemap.xml" in plan["wouldDo"]
    (line,) = audit_lines(home)
    assert "write=sitemaps_submit outcome=refused" in line and "sitemap=https://example.com/sitemap.xml" in line


def test_sitemaps_submit_with_yes(calls):
    seen, answers, home = calls
    answers["submit_sitemap"] = {"submitted": True, "property": PROP, "sitemap": "https://example.com/sitemap.xml",
                                 "listed": True, "row": None}
    out = run("sitemaps", "submit", "example.com", "https://example.com/sitemap.xml", "--yes", "--json")
    assert out.exit_code == 0 and json.loads(out.stdout)["submitted"] is True
    assert seen == [("submit_sitemap", (PROP, "https://example.com/sitemap.xml"), {"headed": False,
                                                                                   "confirm": True})]
    assert [ln.split(" outcome=")[1].split()[0] for ln in audit_lines(home)] == ["attempt", "done"]
    answers["submit_sitemap"] = {**answers["submit_sitemap"], "listed": False}
    assert run("sitemaps", "submit", PROP, "https://example.com/sitemap.xml", "--yes").exit_code == 8


def test_sitemaps_submit_usage(calls):
    seen, _, home = calls
    assert run("sitemaps", "submit", PROP, "https://example.net/sitemap.xml", "--yes").exit_code == 2
    assert run("sitemaps", "submit", PROP).exit_code == 2
    assert run("sitemaps", PROP, "--yes").exit_code == 2
    assert seen == [] and audit_lines(home) == []


def test_request_indexing_refuses_without_yes(calls):
    seen, _, home = calls
    out = run("inspect", "https://example.com/page", "--request-indexing")
    assert out.exit_code == 2 and seen == []
    plan = json.loads(out.stdout)
    assert plan["dryRun"] is True and plan["write"] == "request_indexing" and plan["url"] == "https://example.com/page"
    (line,) = audit_lines(home)
    assert "write=request_indexing outcome=refused" in line and "url=https://example.com/page" in line
    assert run("inspect", "https://example.com/", "--yes").exit_code == 2  # --yes alone writes nothing


def ri_result(outcome, sent=1, status=200, error=None):
    return {"requested": outcome == "confirmed", "url": "https://example.com/", "property": PROP, "outcome": outcome,
            "confirmed": "dialog" if outcome == "confirmed" else None, "dialog": "a dialog", "fj3owfSent": sent,
            "fj3owfStatus": status, "fj3owfError": error, "message": f"the {outcome} message"}


def test_request_indexing_with_yes(calls):
    seen, answers, home = calls
    answers["request_indexing"] = ri_result("confirmed")
    out = run("inspect", "https://example.com/", "--property", "example.com", "--request-indexing", "--yes", "--json")
    got = json.loads(out.stdout)
    assert out.exit_code == 0 and got["requested"] is True and got["confirmed"] == "dialog"
    assert got["fj3owfStatus"] == 200
    assert seen == [("request_indexing", ("https://example.com/", PROP), {"headed": False, "confirm": True})]
    lines = audit_lines(home)
    assert [ln.split(" outcome=")[1].split()[0] for ln in lines] == ["attempt", "done"]
    assert "fj3owf=200" in lines[-1] and "exit=" not in lines[-1]


@pytest.mark.parametrize(("result", "code", "logged"), [
    (ri_result("unconfirmed", error="code 3"), 8, "exit=8 fj3owf=200 fj3owfError=code-3"),
    (ri_result("unconfirmed", status=None), 8, "exit=8 fj3owf=unseen"),
    (ri_result("not_sent", sent=0, status=None), 5, "exit=5 fj3owf=unsent"),
    (ri_result("quota"), 6, "exit=6 fj3owf=200"),
    ({"requested": True}, 5, "exit=5 fj3owf=unsent"),  # no outcome: never exit 0
])
def test_request_indexing_exit_codes(calls, result, code, logged):
    _, answers, home = calls
    answers["request_indexing"] = result
    out = run("inspect", "https://example.com/", "--request-indexing", "--yes", "--json")
    assert out.exit_code == code
    line = audit_lines(home)[-1]
    assert "outcome=failed" in line and logged in line
    err = json.loads(out.stderr.strip().splitlines()[-1])
    assert err["code"] == code
    if code == 8:
        assert err["error"] == "sent_unverified" and err["message"] == "the unconfirmed message"
        assert "before retrying" in err["hint"] and json.loads(out.stdout)["requested"] is False
    if code == 6:
        assert err["error"] == "rate_limited" and "Fj3Owf went out once" in err["hint"]


def test_a_failed_write_is_audited(calls, monkeypatch):
    _, _, home = calls

    async def boom(*a, **k):
        raise fetch.Blocked("Google answered with a challenge")

    monkeypatch.setattr(daemon, "call", boom)
    out = run("sitemaps", "submit", PROP, "https://example.com/sitemap.xml", "--yes")
    assert out.exit_code == 5
    assert "outcome=failed" in audit_lines(home)[-1] and "exit=5" in audit_lines(home)[-1]


# --check-live-sitemap, over a fake fetcher

def _urlset(*urls):
    locs = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>'.encode()


def _index(*maps):
    locs = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in maps)
    return (f'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}'
            "</sitemapindex>").encode()


def test_live_sitemap_follows_indexes_and_gzip():
    import gzip

    from webcli.sites.gsc import livesitemap
    files = {"https://example.com/sitemap.xml": _index("https://example.com/a.xml.gz", "https://example.net/x.xml",
                                                       "https://example.com/missing.xml"),
             "https://example.com/a.xml.gz": gzip.compress(_urlset("https://example.com/", "https://example.com/b"))}
    got = []

    def get(url, prop):
        got.append(url)
        if url not in files:
            raise OSError("HTTP Error 404: Not Found")
        return files[url]
    out = livesitemap.check("https://example.com/b", ["https://example.com/sitemap.xml"], PROP, get)
    assert out["inLiveSitemap"] is True and out["liveSitemaps"] == ["https://example.com/a.xml.gz"]
    assert out["liveSitemapsRead"] == 2 and out["liveSitemapUrls"] == 2
    assert "https://example.net/x.xml" not in got  # off the property: never fetched
    assert {e["sitemap"] for e in out["liveSitemapErrors"]} == {"https://example.net/x.xml",
                                                                "https://example.com/missing.xml"}
    out = livesitemap.check("https://example.com/c", ["https://example.com/sitemap.xml"], PROP, get)
    assert out["inLiveSitemap"] is False and out["liveSitemaps"] == []
    out = livesitemap.check("https://example.com/c", ["https://example.net/sitemap.xml"], PROP, get)
    assert out["inLiveSitemap"] is None and out["liveSitemapsRead"] == 0


def test_live_sitemap_rejects_non_sitemaps():
    from webcli.sites.gsc import livesitemap
    with pytest.raises(ValueError):
        livesitemap.parse_sitemap(b"<html><body>hi</body></html>")
    assert livesitemap.parse_sitemap(_urlset("https://example.com/")) == (["https://example.com/"], [])
