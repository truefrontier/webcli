"""LinkedIn Boost stats (limsg's boost.py, its parsing and record logic kept as it was): personal Campaign Manager
ad accounts, read with GET only from https://www.linkedin.com/robots.txt (same origin, no app JS). fetch.py makes
the calls; this module turns them into records. Never a POST, never an ?action= endpoint (the UI's
fetchCurrentRunningSpend / billingConfiguration).

  GET campaignManagerAccounts?q=memberBoostAccounts
  GET campaignManagerReportingCampaigns?accountId={acct}&q=accountAndCampaignGroups
  GET campaignManagerCampaigns/{id}
  GET campaignManagerReportingCreatives?accountId={acct}&q=cursorBasedCriteria&campaignIds=List({id})&count=10
  GET campaignManagerAdAnalytics?q=statistics&pivots=List(CAMPAIGN)&timeGranularity=ALL&...
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import unquote

# Every name here was validated live; an unknown field makes the server return 500.
ANALYTICS_FIELDS = (
    "baseMetrics:(costInLocalCurrency,costInUsd,impressions,clicks,landingPageClicks,"
    "clickThroughRate,costPerClick,totalEngagements,averageEngagementRate,costPerThousandImpressions),"
    "socialMetrics:(reactions,comments,shares,follows,otherEngagements,totalSocialActions),"
    "deliveryMetrics:(reach,averageFrequency),pivotValues,dateRange"
)

CONTENT_URN = re.compile(r"urn:li:(?:share|ugcPost|activity):\d+")
_TAG = re.compile(r"<[^>]+>")

# Reported as 0 when analytics succeeds with no row for the campaign.
_ZERO_ON_NO_ROWS = (
    "impressions",
    "clicks",
    "landingPageClicks",
    "reactions",
    "comments",
    "reposts",
    "totalEngagements",
    "spend",
)
_RAW_METRICS = (
    "spend",
    "spendUsd",
    "impressions",
    "reach",
    "clicks",
    "landingPageClicks",
    "reactions",
    "comments",
    "reposts",
    "follows",
    "totalEngagements",
)


# ---------------------------------------------------------------- parsing


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    n = _num(value)
    return None if n is None else int(n)


def _trailing_int(value: Any) -> int | None:
    """910747076, "910747076", or "urn:li:sponsoredCampaign:910747076" -> 910747076."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    m = re.search(r"(\d+)\D*$", str(value))
    return int(m.group(1)) if m else None


def _money(value: Any) -> float | None:
    return _num(value.get("amount")) if isinstance(value, dict) else None


def _ratio(a: float | None, b: float | None, scale: float, digits: int) -> float | None:
    if a is None or not b:
        return None
    return round(a / b * scale, digits)


def elements(data: Any) -> list[dict]:
    """Restli lists may sit at the top level or under data (normalized responses)."""
    if not isinstance(data, dict):
        return []
    rows = data.get("elements")
    if rows is None and isinstance(data.get("data"), dict):
        rows = data["data"].get("elements")
    return [row for row in rows or [] if isinstance(row, dict)]


def ms_to_dt(ms: Any) -> datetime | None:
    n = _num(ms)
    return None if n is None else datetime.fromtimestamp(n / 1000, tz=timezone.utc)


def campaign_start(campaign: dict) -> datetime | None:
    return ms_to_dt(campaign.get("startsAt") or (campaign.get("runSchedule") or {}).get("start"))


def campaign_end(campaign: dict) -> datetime | None:
    return ms_to_dt(campaign.get("endsAt") or (campaign.get("runSchedule") or {}).get("end"))


def serving_statuses(campaign: dict) -> list[str]:
    statuses = campaign.get("servingStatuses")
    if isinstance(statuses, list):
        return [str(s) for s in statuses]
    single = campaign.get("servingStatus")
    return [str(single)] if single else []


def content_urns(creatives: list[dict]) -> list[str]:
    """Post urns referenced by the creatives' preview URLs (url-decoded), in order."""
    found: list[str] = []
    for creative in creatives:
        for urn in CONTENT_URN.findall(unquote(str(creative.get("previewUrl") or ""))):
            if urn not in found:
                found.append(urn)
    return found


def _creative_statuses(creative: dict) -> set[str]:
    v2 = creative.get("statusV2") if isinstance(creative.get("statusV2"), dict) else {}
    return {str(s).upper() for s in (creative.get("status"), v2.get("status")) if s}


def status_detail(creatives: list[dict]) -> str | None:
    parts: list[str] = []
    for creative in creatives:
        v2 = creative.get("statusV2") if isinstance(creative.get("statusV2"), dict) else {}
        for raw in v2.get("statusDetails") or []:
            text = re.sub(r"\s+", " ", html.unescape(_TAG.sub("", str(raw)))).strip()
            if text and text not in parts:
                parts.append(text)
    return " ".join(parts) or None


def derive_status(campaign: dict, creatives: list[dict], now: datetime) -> str | None:
    """Precedence: COMPLETED/ARCHIVED/CANCELED > REJECTED > IN_REVIEW > PAUSED/DRAFT > ON_HOLD > SCHEDULED > ACTIVE."""
    raw = str(campaign.get("status") or "").upper() or None
    start, end = campaign_start(campaign), campaign_end(campaign)
    creative_states = set().union(*(_creative_statuses(c) for c in creatives))
    if raw == "COMPLETED" or (end is not None and now > end):
        return "COMPLETED"
    if raw in {"ARCHIVED", "CANCELED"}:
        return raw
    if any("REJECT" in s for s in creative_states):
        return "REJECTED"
    if creative_states & {"IN_REVIEW", "CREATIVE_IN_REVIEW"}:
        return "IN_REVIEW"
    if raw in {"PAUSED", "DRAFT"}:
        return raw
    if any("HOLD" in s.upper() for s in serving_statuses(campaign)):
        return "ON_HOLD"
    if start is not None and now < start:
        return "SCHEDULED"
    # Nothing above fired: an ACTIVE campaign inside its schedule stays ACTIVE; anything else is raw.
    return raw


def parse_target(target: str) -> tuple[str, Any]:
    """("campaign", 910747076) or ("urn", "urn:li:share:…"). Accepts feed URLs that contain a urn."""
    text = unquote((target or "").strip())
    m = re.fullmatch(r"(?:urn:li:sponsoredCampaign:)?(\d+)", text)
    if m:
        return "campaign", int(m.group(1))
    m = CONTENT_URN.search(text)
    if m:
        return "urn", m.group(0)
    raise ValueError(
        f"Unrecognized target {target!r}. Use urn:li:share:…, urn:li:activity:…, or a campaign id."
    )


# ---------------------------------------------------------------- dates


def analytics_window(
    campaign: dict,
    *,
    since: datetime | None,
    until: datetime | None,
    now: datetime,
) -> tuple[date, date] | None:
    """UTC dates to query: --since/--until intersected with [campaign start, min(campaign end, today)].

    None when the window is empty (for example a campaign that has not started).
    """
    start_dt, end_dt = campaign_start(campaign), campaign_end(campaign)
    starts = [d for d in (start_dt and start_dt.date(), since and since.date()) if d]
    ends = [d for d in (end_dt and end_dt.date(), until and until.date(), now.date()) if d]
    start = max(starts) if starts else now.date()
    end = min(ends)
    return (start, end) if start <= end else None


def in_list_window(campaign: dict, *, since: datetime | None, until: datetime | None) -> bool:
    """`boost list` drops campaigns that ended before --since or start after --until (inclusive day)."""
    start, end = campaign_start(campaign), campaign_end(campaign)
    if since is not None and end is not None and end.date() < since.date():
        return False
    if until is not None and start is not None and start.date() > until.date():
        return False
    return True


def _restli_date(d: date) -> str:
    return f"(day:{d.day},month:{d.month},year:{d.year})"


def analytics_path(campaign_ids: list[int], start: date, end: date) -> str:
    """Literal restli query (parens/commas/colons not percent-encoded)."""
    ids = ",".join(str(int(i)) for i in campaign_ids)
    return (
        "campaignManagerAdAnalytics?q=statistics&pivots=List(CAMPAIGN)&timeGranularity=ALL"
        f"&dateRange=(start:{_restli_date(start)},end:{_restli_date(end)})"
        f"&campaignIds=List({ids})&fields={ANALYTICS_FIELDS}"
    )


# ---------------------------------------------------------------- metrics


def _pivot(row: dict) -> Any:
    values = row.get("pivotValues")
    if isinstance(values, list) and values:
        return values[0]
    return row.get("pivotValue")


def _row_for(campaign_id: int, rows: list[dict]) -> dict | None:
    for row in rows:
        if _trailing_int(_pivot(row)) == campaign_id:
            return row
    # One campaign per query, so a lone row with no parsable pivot id still belongs to it.
    if len(rows) == 1 and _trailing_int(_pivot(rows[0])) is None:
        return rows[0]
    return None


def _metrics_from_row(row: dict) -> dict[str, Any]:
    base = row.get("baseMetrics") or {}
    social = row.get("socialMetrics") or {}
    delivery = row.get("deliveryMetrics") or {}
    out: dict[str, Any] = {
        "spend": _num(base.get("costInLocalCurrency")),
        "spendUsd": _num(base.get("costInUsd")),
        "impressions": _int(base.get("impressions")),
        "reach": _int(delivery.get("reach")),
        "clicks": _int(base.get("clicks")),
        "landingPageClicks": _int(base.get("landingPageClicks")),
        "reactions": _int(social.get("reactions")),
        "comments": _int(social.get("comments")),
        "reposts": _int(social.get("shares")),
        "follows": _int(social.get("follows")),
        "totalEngagements": _int(base.get("totalEngagements")),
    }
    ctr_api = _num(base.get("clickThroughRate"))
    if ctr_api is not None:
        out["ctrApi"] = ctr_api
    return out


def campaign_metrics(
    campaign_id: int, analytics: tuple[int, Any] | None
) -> tuple[dict[str, Any], str | None, list[str]]:
    """(raw metrics, metricsSource, warnings). `analytics` is (status, data), or None when skipped."""
    nulls: dict[str, Any] = {key: None for key in _RAW_METRICS}
    zeros = {**nulls, **{key: 0 for key in _ZERO_ON_NO_ROWS}, "spend": 0.0}
    if analytics is None:
        return zeros, "adAnalytics:no-delivery-rows", []
    status, data = analytics
    if status != 200 or not isinstance(data, dict):
        detail = " (non-JSON)" if status == 200 else ""
        return nulls, None, [f"adAnalytics HTTP {status}{detail}"]
    row = _row_for(campaign_id, elements(data))
    if row is None:
        return zeros, "adAnalytics:no-delivery-rows", []
    return _metrics_from_row(row), "adAnalytics", []


# ---------------------------------------------------------------- record


def boost_record(
    campaign: dict,
    creatives: list[dict],
    analytics: tuple[int, Any] | None,
    *,
    account: dict | None,
    date_range: tuple[date, date] | None,
    now: datetime,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    account = account or {}
    campaign_id = _trailing_int(campaign.get("id"))
    metrics, source, metric_warnings = campaign_metrics(campaign_id, analytics)
    budget = campaign.get("totalBudget") if isinstance(campaign.get("totalBudget"), dict) else {}
    total_budget = _money(budget)
    spend = metrics["spend"]
    impressions = metrics["impressions"]
    clicks = metrics["clicks"]
    lpc = metrics["landingPageClicks"]
    first = creatives[0] if creatives else {}
    start, end = campaign_start(campaign), campaign_end(campaign)
    urns = content_urns(creatives)
    out: dict[str, Any] = {
        "contentUrn": urns[0] if urns else None,
        "campaignId": campaign_id,
        "campaignName": campaign.get("name"),
        "adAccountId": _trailing_int(campaign.get("accountId")) or _trailing_int(account.get("id")),
        "campaignGroupId": _trailing_int(campaign.get("campaignGroupId")),
        "status": derive_status(campaign, creatives, now),
        "statusDetail": status_detail(creatives),
        "campaignStatus": campaign.get("status"),
        "servingStatuses": serving_statuses(campaign),
        "creativeStatus": first.get("status"),
        "objective": campaign.get("objectiveType"),
        "currency": budget.get("currencyCode") or account.get("currencyCode"),
        "totalBudget": total_budget,
        "dailyBudget": _money(campaign.get("dailyBudget")),
        "spend": spend,
        "spendUsd": metrics["spendUsd"],
        "budgetRemaining": None if total_budget is None or spend is None else round(total_budget - spend, 2),
        "spendPct": _ratio(spend, total_budget, 100, 1),
        "impressions": impressions,
        "reach": metrics["reach"],
        "clicks": clicks,
        "landingPageClicks": lpc,
        "ctr": _ratio(clicks, impressions, 100, 2),
    }
    if "ctrApi" in metrics:
        out["ctrApi"] = metrics["ctrApi"]
    out.update(
        {
            "cpc": _ratio(spend, clicks, 1, 2),
            "costPerLandingPageClick": _ratio(spend, lpc, 1, 2),
            "cpm": _ratio(spend, impressions, 1000, 2),
            "reactions": metrics["reactions"],
            "comments": metrics["comments"],
            "reposts": metrics["reposts"],
            "follows": metrics["follows"],
            "totalEngagements": metrics["totalEngagements"],
            "startAt": _iso(start) if start else None,
            "endAt": _iso(end) if end else None,
            "dateRange": (
                {"start": date_range[0].isoformat(), "end": date_range[1].isoformat()} if date_range else None
            ),
            "metricsSource": source,
            "warnings": list(warnings or []) + metric_warnings,
            "destinationUrl": first.get("destinationUrl"),
            "previewUrl": first.get("previewUrl"),
            "fetchedAt": _iso(now),
        }
    )
    return out


def read_path(path: str) -> bool:
    """A Campaign Manager path a read may GET: relative (under CM_BASE), no "..", and no Rest.li action."""
    return "://" not in path and ".." not in path and "action=" not in unquote(path).lower()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def latest_first(campaign: dict) -> tuple[float, int]:
    start = campaign_start(campaign)
    return (start.timestamp() if start else 0.0, _trailing_int(campaign.get("id")) or 0)
