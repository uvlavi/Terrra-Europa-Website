"""
GA4 Data API wrapper for the in-site /admin/analytics page.

Auth: service account JSON key at GA4_KEY_FILE.
Property: GA4_PROPERTY_ID env var.

If either is missing, all functions return a placeholder dict so the page
can render an empty state without crashing.
"""
import os
from datetime import date, timedelta

PROPERTY_ID = os.environ.get("GA4_PROPERTY_ID", "")
KEY_FILE    = os.environ.get("GA4_KEY_FILE", "")

_client = None
_client_err = None


def _get_client():
    """Lazy-import + lazy-init the BetaAnalyticsDataClient."""
    global _client, _client_err
    if _client is not None or _client_err is not None:
        return _client
    if not PROPERTY_ID or not KEY_FILE or not os.path.exists(KEY_FILE):
        _client_err = "GA4 not configured (set GA4_PROPERTY_ID and GA4_KEY_FILE)"
        return None
    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        _client = BetaAnalyticsDataClient.from_service_account_file(KEY_FILE)
        return _client
    except Exception as e:
        _client_err = f"Failed to init GA4 client: {e}"
        return None


def is_configured() -> bool:
    return _get_client() is not None


def status() -> str:
    _get_client()
    return _client_err or "OK"


def _run_report(date_ranges, metrics, dimensions=None, limit=None):
    """Single GA4 runReport call, returns rows as list of dicts."""
    client = _get_client()
    if client is None:
        return []
    from google.analytics.data_v1beta.types import (
        RunReportRequest, DateRange, Metric, Dimension,
    )
    req = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        date_ranges=[DateRange(start_date=s, end_date=e) for s, e in date_ranges],
        metrics=[Metric(name=m) for m in metrics],
        dimensions=[Dimension(name=d) for d in (dimensions or [])],
        limit=limit or 10000,
    )
    resp = client.run_report(req)
    rows = []
    for r in resp.rows:
        row = {}
        for i, d in enumerate(dimensions or []):
            row[d] = r.dimension_values[i].value
        for i, m in enumerate(metrics):
            v = r.metric_values[i].value
            try:
                row[m] = int(v)
            except (ValueError, TypeError):
                try:
                    row[m] = float(v)
                except (ValueError, TypeError):
                    row[m] = v
        rows.append(row)
    return rows


def _ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def summary() -> dict:
    """Return headline numbers and breakdowns for the dashboard."""
    if not is_configured():
        return {"configured": False, "error": status()}

    today = date.today().isoformat()

    # Three windows: today, 7d, 30d (totals)
    totals = _run_report(
        date_ranges=[
            (today,    today),
            (_ago(6),  today),
            (_ago(29), today),
        ],
        metrics=["activeUsers", "screenPageViews", "sessions"],
    )

    # GA4 returns one row per date_range (with no dimensions), in order
    def safe(idx, key):
        return totals[idx][key] if idx < len(totals) else 0
    headline = {
        "today":   {"users": safe(0, "activeUsers"), "views": safe(0, "screenPageViews"), "sessions": safe(0, "sessions")},
        "last_7":  {"users": safe(1, "activeUsers"), "views": safe(1, "screenPageViews"), "sessions": safe(1, "sessions")},
        "last_30": {"users": safe(2, "activeUsers"), "views": safe(2, "screenPageViews"), "sessions": safe(2, "sessions")},
    }

    # Daily users for last 30 days (line chart)
    daily = _run_report(
        date_ranges=[(_ago(29), today)],
        metrics=["activeUsers"],
        dimensions=["date"],
    )
    daily.sort(key=lambda r: r["date"])

    # Country breakdown (last 30 days, top 10)
    countries = _run_report(
        date_ranges=[(_ago(29), today)],
        metrics=["activeUsers"],
        dimensions=["country"],
        limit=10,
    )
    countries.sort(key=lambda r: r["activeUsers"], reverse=True)

    # Top sources (last 30 days, top 10)
    sources = _run_report(
        date_ranges=[(_ago(29), today)],
        metrics=["activeUsers"],
        dimensions=["sessionSource"],
        limit=10,
    )
    sources.sort(key=lambda r: r["activeUsers"], reverse=True)

    # Top pages (last 30 days, top 10)
    pages = _run_report(
        date_ranges=[(_ago(29), today)],
        metrics=["screenPageViews", "activeUsers"],
        dimensions=["pagePath"],
        limit=10,
    )
    pages.sort(key=lambda r: r["screenPageViews"], reverse=True)

    return {
        "configured": True,
        "headline":   headline,
        "daily":      daily,
        "countries":  countries,
        "sources":    sources,
        "pages":      pages,
    }
