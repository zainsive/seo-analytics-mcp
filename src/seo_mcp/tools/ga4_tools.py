"""GA4 tools. The shaping and cache layers are reused wholesale."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import cache, config
from ..auth import scopes, store
from ..clients import ga4
from ..server import mcp
from ..shaping import DateRangeError, ShapedResponse, normalise_url, resolve_range, shape
from . import _base
from ._base import google_tool, locals_without

LANDING_PAGE_METRICS = (
    "sessions",
    "engagedSessions",
    "engagementRate",
    "conversions",
    "totalRevenue",
)


def _property(property_id: str | None) -> str:
    """Resolve and validate before any credential is loaded: a malformed property ID
    must be reported as that, not as an authorisation problem."""
    resolved = _base.require(
        property_id or config.active_profile().default_property,
        what="property_id",
        env_var="GA4_DEFAULT_PROPERTY",
        lister="ga4_list_properties",
    )
    return ga4.qualify(resolved)


def _range(start_date: str, end_date: str) -> tuple[str, str, list[str]]:
    try:
        return resolve_range(start_date, end_date, source="ga4")
    except DateRangeError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=_base.READ_ONLY)
@google_tool(api=ga4.ADMIN_API)
async def ga4_list_properties() -> ShapedResponse:
    """List the GA4 accounts and properties this Google account can read, so a property
    name can be resolved to the numeric ID every other GA4 tool needs. Universal
    Analytics properties are not returned - they no longer exist in this API.
    """
    profile = config.active_profile()
    creds = store.load(profile, {scopes.GA4_READ})
    key = cache.key(profile, "ga4.accountSummaries", {})
    result = await cache.get_or_set(
        profile, key, ttl=cache.TTL_DISCOVERY, producer=lambda: ga4.list_properties(creds)
    )
    return shape(result, top_n=200)


@mcp.tool(annotations=_base.READ_ONLY)
@google_tool(api=ga4.DATA_API)
async def ga4_run_report(
    metrics: list[str],
    dimensions: list[str] | None = None,
    property_id: str | None = None,
    start_date: str = "28daysAgo",
    end_date: str = "yesterday",
    limit: int = 250,
    dimension_filter: dict[str, Any] | None = None,
    order_by: list[dict[str, Any]] | None = None,
) -> ShapedResponse:
    """Run an arbitrary GA4 report (the runReport method).

    metrics and dimensions are GA4 API names, not UI labels: 'sessions',
    'activeUsers', 'screenPageViews', 'conversions', 'totalRevenue';
    'landingPage', 'pagePath', 'sessionDefaultChannelGroup', 'country', 'date'.
    Not every dimension is compatible with every metric - a 400 error means the pair is
    not queryable together, not that the names are wrong. dimension_filter and order_by
    take GA4 FilterExpression and OrderBy objects as plain dicts. Dates accept
    YYYY-MM-DD, 'today', 'yesterday' or 'NdaysAgo'. GA4 reports in the property's own
    timezone, so its dates do not line up exactly with Search Console's.
    """
    property_id = _property(property_id)
    start_date, end_date, _ = _range(start_date, end_date)
    dimensions = dimensions or []
    if not metrics:
        raise ToolError(
            "At least one metric is required, e.g. metrics=['sessions']. "
            "GA4 cannot return a report of dimensions alone."
        )
    profile = config.active_profile()
    creds = store.load(profile, {scopes.GA4_READ})
    key = cache.key(profile, "ga4.runReport", locals_without())
    result = await cache.get_or_set(
        profile,
        key,
        ttl=cache.TTL_GA4,
        producer=lambda: ga4.run_report(
            creds,
            property_id,
            dimensions=dimensions,
            metrics=metrics,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            dimension_filter=dimension_filter,
            order_by=order_by,
        ),
    )
    return shape(result, top_n=limit, export_dir=profile.export_dir)


@mcp.tool(annotations=_base.READ_ONLY)
@google_tool(api=ga4.DATA_API)
async def ga4_landing_pages(
    property_id: str | None = None,
    start_date: str = "28daysAgo",
    end_date: str = "yesterday",
    path_contains: str | None = None,
    top_n: int = 50,
) -> ShapedResponse:
    """Sessions, engagement rate, conversions and revenue by landing page - the preset
    that joins to Search Console. Every row gains a normalised 'path' key ('/pricing',
    no host, no trailing slash, no query string) which is directly comparable to the
    'path' key added by gsc_search_analytics rows grouped by page.
    """
    property_id = _property(property_id)
    start_date, end_date, _ = _range(start_date, end_date)
    profile = config.active_profile()
    creds = store.load(profile, {scopes.GA4_READ})
    key = cache.key(profile, "ga4.landingPages", locals_without())

    dimension_filter = None
    if path_contains:
        dimension_filter = {
            "filter": {
                "field_name": "landingPage",
                "string_filter": {"match_type": "CONTAINS", "value": path_contains},
            }
        }

    async def producer():
        result = await ga4.run_report(
            creds,
            property_id,
            dimensions=["landingPage"],
            metrics=list(LANDING_PAGE_METRICS),
            start_date=start_date,
            end_date=end_date,
            limit=max(top_n, 100),
            dimension_filter=dimension_filter,
            order_by=[{"metric": {"metric_name": "sessions"}, "desc": True}],
        )
        # Normalise here, once, or every composite tool quietly joins nothing.
        rows = [{**r, "path": normalise_url(str(r.get("landingPage") or "/"))} for r in result.rows]
        return QueryResult(
            rows=rows,
            dimension_keys=("landingPage", "path"),
            metric_keys=result.metric_keys,
            source="ga4",
            total_rows=result.total_rows,
            meta=result.meta,
            notes=result.notes,
        )

    from ..shaping import QueryResult  # local import keeps the producer closure tidy

    result = await cache.get_or_set(profile, key, ttl=cache.TTL_GA4, producer=producer)
    return shape(result, top_n=top_n, sort_by="sessions", export_dir=profile.export_dir)
