"""GA4 client: google-analytics-data for runReport, google-analytics-admin for discovery.

return_property_quota=True costs nothing and turns the most confusing GA4 failure -
silent quota exhaustion mid-session - into a note the model can act on.
"""

from __future__ import annotations

import asyncio
from typing import Any

from google.analytics.admin_v1beta import AnalyticsAdminServiceClient
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    FilterExpression,
    Metric,
    OrderBy,
    RunReportRequest,
)
from google.oauth2.credentials import Credentials
from mcp.server.mcpserver.exceptions import ToolError

from ..shaping import QueryResult

DATA_API = "analyticsdata.googleapis.com"
ADMIN_API = "analyticsadmin.googleapis.com"
API_ROW_MAX = 250_000
LOW_QUOTA_TOKENS = 2000


def qualify(property_id: str) -> str:
    text = str(property_id).strip()
    if text.startswith("properties/"):
        return text
    if not text.isdigit():
        raise ToolError(
            f"{property_id!r} is not a GA4 property ID. Use the numeric ID, for example "
            "'properties/123456789' or '123456789'. List what this account can see with "
            "ga4_list_properties."
        )
    return f"properties/{text}"


def _num(value: str) -> Any:
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return value
    return int(as_float) if as_float.is_integer() else round(as_float, 4)


async def run_report(
    creds: Credentials,
    property_id: str,
    *,
    dimensions: list[str],
    metrics: list[str],
    start_date: str,
    end_date: str,
    limit: int = 250,
    dimension_filter: dict[str, Any] | None = None,
    order_by: list[dict[str, Any]] | None = None,
) -> QueryResult:
    def work():
        client = BetaAnalyticsDataClient(credentials=creds)
        req = RunReportRequest(
            property=qualify(property_id),
            dimensions=[Dimension(name=d) for d in dimensions],
            metrics=[Metric(name=m) for m in metrics],
            date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
            limit=min(limit, API_ROW_MAX),  # API hard maximum
            return_property_quota=True,  # cheap, and prevents baffling failures
            dimension_filter=FilterExpression(dimension_filter) if dimension_filter else None,
            order_bys=[OrderBy(o) for o in order_by] if order_by else None,
        )
        return client.run_report(req)

    resp = await asyncio.to_thread(work)
    rows = [
        {
            **{
                d.name: v.value
                for d, v in zip(resp.dimension_headers, r.dimension_values, strict=False)
            },
            **{
                m.name: _num(v.value)
                for m, v in zip(resp.metric_headers, r.metric_values, strict=False)
            },
        }
        for r in resp.rows
    ]
    metadata = getattr(resp, "metadata", None)
    sampled = bool(metadata and metadata.sampling_metadatas)
    meta: dict[str, Any] = {
        "date_range": f"{start_date}..{end_date}",
        "property": qualify(property_id),
        "sampled": sampled,
    }
    notes: list[str] = []
    if metadata is not None and metadata.time_zone:
        # GA4 reports in the property's timezone, GSC in Pacific: dates are not
        # strictly comparable across the two, and the model should know it.
        meta["time_zone"] = metadata.time_zone
    quota = getattr(resp, "property_quota", None)
    if quota and quota.tokens_per_hour:
        remaining = quota.tokens_per_hour.remaining
        meta["quota_tokens_remaining"] = remaining
        if remaining < LOW_QUOTA_TOKENS:
            notes.append(
                f"Only {remaining} GA4 hourly tokens remain (40,000/hour on a standard "
                "property). Further large reports may fail."
            )
    if sampled:
        notes.append("GA4 returned sampled data; totals are estimates.")
    if metadata is not None and metadata.subject_to_thresholding:
        notes.append(
            "GA4 withheld some rows under its data-thresholding rules, so these rows do "
            "NOT sum to property totals. Do not compute shares from them."
        )
    if metadata is not None and metadata.empty_reason:
        notes.append(f"GA4 returned no rows: {metadata.empty_reason}")
    return QueryResult(
        rows=rows,
        dimension_keys=tuple(dimensions),
        metric_keys=tuple(metrics),
        source="ga4",
        total_rows=resp.row_count,
        meta=meta,
        notes=notes,
    )


async def list_properties(creds: Credentials) -> QueryResult:
    def work() -> list[dict[str, Any]]:
        client = AnalyticsAdminServiceClient(credentials=creds)
        out: list[dict[str, Any]] = []
        for summary in client.list_account_summaries():
            for prop in summary.property_summaries:
                out.append(
                    {
                        "property": prop.property,
                        "property_name": prop.display_name,
                        "account": summary.account,
                        "account_name": summary.display_name,
                        "property_type": prop.property_type.name
                        if hasattr(prop.property_type, "name")
                        else str(prop.property_type),
                    }
                )
        return out

    rows = await asyncio.to_thread(work)
    notes = []
    if not rows:
        notes.append(
            "This Google account can see no GA4 properties. Either the wrong account was "
            "authorised, or it has not been granted access under Admin > Property access "
            "management. Note that Universal Analytics properties are not visible here."
        )
    return QueryResult(
        rows=rows,
        dimension_keys=("property", "property_name", "account", "account_name", "property_type"),
        metric_keys=(),
        source="ga4",
        total_rows=len(rows),
        notes=notes,
    )
