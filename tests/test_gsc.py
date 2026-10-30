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
    assert out.exit_code == 0 and seen[-1][:2] == ("inspect", ("https://example.com/", None, False))
    run("inspect", "https://example.com/", "--check-live-sitemap", "--json")
    assert seen[-1][:2] == ("inspect", ("https://example.com/", None, True))
    assert run("inspect", "https://example.com/", "--check-live-sitemap", "--request-indexing").exit_code == 2
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


def test_request_indexing_with_yes(calls):
    seen, answers, home = calls
    answers["request_indexing"] = {"requested": True, "url": "https://example.com/", "property": PROP,
                                   "message": "Indexing requested", "requestsSent": 1}
    out = run("inspect", "https://example.com/", "--property", "example.com", "--request-indexing", "--yes", "--json")
    assert out.exit_code == 0 and json.loads(out.stdout)["requested"] is True
    assert seen == [("request_indexing", ("https://example.com/", PROP), {"headed": False, "confirm": True})]
    assert [ln.split(" outcome=")[1].split()[0] for ln in audit_lines(home)] == ["attempt", "done"]


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
