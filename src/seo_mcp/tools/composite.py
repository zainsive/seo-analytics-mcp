"""The tools that justify a new server: they join sources the model cannot join itself.

Both fan out with asyncio.gather and join on normalise_url. Every sub-request is
individually recoverable: a GA4 failure must not lose the Search Console answer.
"""

from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import cache, config
from ..auth import scopes, store
from ..clients import ga4, gsc
from ..server import mcp
from ..shaping import (
    DateRangeError,
    QueryResult,
    aggregate_rows,
    normalise_url,
    resolve_range,
    shape,
)
from . import _base
from ._base import google_tool

DAY = 86400


def _soft(label: str, exc: Exception) -> str:
    return f"{label} could not be included: {type(exc).__name__}: {exc}"[:300]


@mcp.tool(annotations=_base.READ_ONLY)
@google_tool(api=gsc.API)
async def gsc_compare_periods(
    site_url: str | None = None,
    dimension: str = "page",
    period_days: int = 28,
    offset_days: int = 0,
    metric: str = "clicks",
    top_n: int = 20,
) -> dict[str, Any]:
    """Compare two consecutive Search Console periods and return the biggest movers in
    both directions - the 'what changed' question in one call.

    period_days is the length of each window; the current window ends offset_days before
    the freshest available data, and the comparison window is the period_days
    immediately before it. metric is clicks, impressions, ctr or position. For position,
    a DECREASE is an improvement, and the response says so. dimension is usually page or
    query; query rows are anonymised by Google and will not sum to totals.
    """
    from datetime import date, timedelta

    site_url = _base.require(
        site_url or config.active_profile().default_site,
        what="site_url",
        env_var="GSC_DEFAULT_SITE",
        lister="gsc_list_sites",
    )
    if dimension not in gsc.VALID_DIMENSIONS:
        raise ToolError(
            f"dimension {dimension!r} is not a Search Console dimension. "
            f"Use one of: {', '.join(gsc.VALID_DIMENSIONS)}."
        )
    if metric not in gsc.METRICS:
        raise ToolError(f"metric must be one of {', '.join(gsc.METRICS)}; got {metric!r}.")
    if period_days < 1 or period_days > 480:
        raise ToolError("period_days must be between 1 and 480.")

    # Anchor on the freshest final data, not on today, or the recent window is empty.
    anchor = date.today() - timedelta(days=3 + max(0, offset_days))
    cur_end = anchor
    cur_start = anchor - timedelta(days=period_days - 1)
    prev_end = cur_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=period_days - 1)

    try:
        for start, end in ((cur_start, cur_end), (prev_start, prev_end)):
            resolve_range(start.isoformat(), end.isoformat(), source="gsc")
    except DateRangeError as exc:
        raise ToolError(str(exc)) from exc

    profile = config.active_profile()
    creds = store.load(profile, {scopes.GSC_READ})

    async def window(start: Any, end: Any) -> QueryResult:
        key = cache.key(
            profile,
            "gsc.searchanalytics",
            {
                "site_url": site_url,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "dimensions": [dimension],
                "row_limit": 1000,
                "data_state": "final",
                "search_type": "web",
            },
        )
        return await cache.get_or_set(
            profile,
            key,
            ttl=cache.TTL_GSC,
            producer=lambda: gsc.search_analytics(
                creds,
                site_url,
                start.isoformat(),
                end.isoformat(),
                dimensions=[dimension],
                filters=None,
                search_type="web",
                row_limit=1000,
                data_state="final",
            ),
        )

    current, previous = await asyncio.gather(
        window(cur_start, cur_end), window(prev_start, prev_end)
    )

    def index(result: QueryResult) -> dict[str, dict[str, Any]]:
        # keep_host=True: both sides are Search Console rows, and a domain property
        # spans subdomains, so the host is part of the page's identity here.
        key_fn = None
        if dimension == "page":

            def key_fn(row):  # noqa: E306
                return normalise_url(str(row.get("page") or ""), keep_host=True)

        return aggregate_rows(
            result.rows, key=dimension, metric_keys=result.metric_keys, key_fn=key_fn
        )

    now, before = index(current), index(previous)
    merged = sum(r.get("_merged_from", 1) - 1 for side in (now, before) for r in side.values())
    lower_is_better = metric == "position"
    movers = []
    for key_value in set(now) | set(before):
        a = float(before.get(key_value, {}).get(metric) or 0)
        b = float(now.get(key_value, {}).get(metric) or 0)
        if not a and not b:
            continue
        delta = round(b - a, 4)
        movers.append(
            {
                dimension: key_value,
                f"{metric}_current": round(b, 4),
                f"{metric}_previous": round(a, 4),
                "delta": delta,
                "pct_change": round((delta / a) * 100, 1) if a else None,
                "status": "new" if not a else ("lost" if not b else "changed"),
            }
        )

    # Split by DIRECTION, not by sort order: re-sorting one list two ways puts a single
    # mover in both, and a page that did not move at all in both as well.
    moved = [m for m in movers if m["delta"] != 0]
    better = (lambda d: d < 0) if lower_is_better else (lambda d: d > 0)
    improved = sorted(
        (m for m in moved if better(m["delta"])),
        key=lambda m: m["delta"],
        reverse=not lower_is_better,
    )[:top_n]
    declined = sorted(
        (m for m in moved if not better(m["delta"])),
        key=lambda m: m["delta"],
        reverse=lower_is_better,
    )[:top_n]

    notes = [
        f"Windows are final data only: current {cur_start}..{cur_end}, "
        f"previous {prev_start}..{prev_end}."
    ]
    if lower_is_better:
        notes.append(
            "For position, a NEGATIVE delta is an improvement (the page moved up). "
            "'gained' below means moved up the results."
        )
    if dimension == "query":
        notes.append(
            "Google anonymises rare queries, so these rows do not sum to property totals "
            "and a query can appear 'new' or 'lost' purely by crossing that threshold."
        )
    if dimension == "page":
        notes.append(
            "Page URLs are normalised (trailing slash and query string removed, host "
            "kept) so the two windows join reliably. A domain property covers every "
            "subdomain, so the host is part of each page's identity."
        )
    if merged:
        notes.append(
            f"{merged} row(s) normalised onto an existing key and were summed into it, "
            "not discarded."
        )

    return {
        "summary": {
            "site_url": site_url,
            "dimension": dimension,
            "metric": metric,
            "current_range": f"{cur_start}..{cur_end}",
            "previous_range": f"{prev_start}..{prev_end}",
            "current_total": current and shape(current, top_n=1)["summary"]["totals"].get(metric),
            "previous_total": previous
            and shape(previous, top_n=1)["summary"]["totals"].get(metric),
            "rows_compared": len(movers),
            "rows_that_moved": len(moved),
        },
        "gained": improved,
        "declined": declined,
        "notes": notes,
    }


@mcp.tool(annotations=_base.READ_ONLY)
@google_tool(api=gsc.API)
async def page_report(
    url: str,
    site_url: str | None = None,
    property_id: str | None = None,
    days: int = 28,
) -> dict[str, Any]:
    """Everything known about one page in a single call: its Search Console clicks and
    impressions trend, its top queries, its GA4 engagement, and its current index
    status. Use this for 'how is /pricing doing?'.

    GA4 and index status are included on a best-effort basis - if GA4 is not authorised
    or the property is unknown, the Search Console half is still returned, with a note
    saying what was left out.
    """
    profile = config.active_profile()
    site_url = _base.require(
        site_url or profile.default_site,
        what="site_url",
        env_var="GSC_DEFAULT_SITE",
        lister="gsc_list_sites",
    )
    property_id = property_id or profile.default_property
    try:
        start, end, range_notes = resolve_range(f"{days}daysAgo", "yesterday", source="gsc")
    except DateRangeError as exc:
        raise ToolError(str(exc)) from exc

    path = normalise_url(url)
    creds = store.load(profile, {scopes.GSC_READ})
    notes: list[str] = list(range_notes)

    async def gsc_trend() -> QueryResult:
        return await gsc.search_analytics(
            creds,
            site_url,
            start,
            end,
            dimensions=["date"],
            filters=[f"page contains {path}"],
            search_type="web",
            row_limit=400,
            data_state="final",
        )

    async def gsc_queries() -> QueryResult:
        return await gsc.search_analytics(
            creds,
            site_url,
            start,
            end,
            dimensions=["query"],
            filters=[f"page contains {path}"],
            search_type="web",
            row_limit=25,
            data_state="final",
        )

    async def index_status() -> dict[str, Any]:
        return await gsc.inspect_url(creds, site_url, url)

    async def ga4_engagement() -> QueryResult | None:
        if not property_id:
            return None
        ga_creds = store.load(profile, {scopes.GA4_READ})
        return await ga4.run_report(
            ga_creds,
            property_id,
            dimensions=["landingPage"],
            metrics=["sessions", "engagedSessions", "engagementRate", "conversions"],
            start_date=start,
            end_date=end,
            limit=25,
            dimension_filter={
                "filter": {
                    "field_name": "landingPage",
                    "string_filter": {"match_type": "CONTAINS", "value": path},
                }
            },
        )

    trend, queries, index, engagement = await asyncio.gather(
        gsc_trend(),
        gsc_queries(),
        index_status(),
        ga4_engagement(),
        return_exceptions=True,
    )

    out: dict[str, Any] = {
        "url": url,
        "path": path,
        "site_url": site_url,
        "date_range": f"{start}..{end}",
    }

    if isinstance(trend, QueryResult):
        shaped = shape(trend, top_n=days + 5, sort_by="date")
        out["search_console"] = {
            "totals": shaped["summary"]["totals"],
            "days_with_data": len(shaped["rows"]),
            "daily": shaped["rows"],
        }
        notes.extend(shaped["notes"])
    elif isinstance(trend, Exception):
        notes.append(_soft("Search Console trend", trend))

    if isinstance(queries, QueryResult):
        shaped = shape(queries, top_n=10)
        out["top_queries"] = shaped["rows"]
        notes.extend(shaped["notes"])
    elif isinstance(queries, Exception):
        notes.append(_soft("Top queries", queries))

    if isinstance(index, dict):
        out["index_status"] = _base.as_rows(index)
    elif isinstance(index, Exception):
        notes.append(_soft("Index status", index))

    if isinstance(engagement, QueryResult):
        rows = [
            {**r, "path": normalise_url(str(r.get("landingPage") or "/"))} for r in engagement.rows
        ]
        exact = [r for r in rows if r["path"] == path]
        out["ga4"] = {
            "property": engagement.meta.get("property"),
            "matched_landing_pages": len(rows),
            "exact_match": exact[0] if exact else None,
            "all_matches": rows[:5],
        }
        notes.extend(engagement.notes)
        if rows and not exact:
            notes.append(
                f"No GA4 landing page normalised exactly to {path}; the closest matches "
                "are listed under all_matches. GA4 landing pages can carry query strings."
            )
    elif isinstance(engagement, Exception):
        notes.append(_soft("GA4 engagement", engagement))
    elif engagement is None:
        notes.append(
            "GA4 was not included: no property_id was given and GA4_DEFAULT_PROPERTY is "
            "not set. Call ga4_list_properties to find the numeric ID."
        )

    # The fan-out means several sub-calls contribute the same caveat. Say each once,
    # in the order first seen.
    out["notes"] = list(dict.fromkeys(notes))
    return out
