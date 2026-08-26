"""Search Console client.

ONE discovery document covers everything: searchanalytics, sites, sitemaps, urlInspection.
The legacy "webmasters" v3 API is not needed - do not build against it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from mcp.server.mcpserver.exceptions import ToolError

from ..shaping import QueryResult

API = "searchconsole.googleapis.com"
METRICS = ("clicks", "impressions", "ctr", "position")
API_ROW_MAX = 25_000
VALID_DIMENSIONS = ("query", "page", "country", "device", "date", "searchAppearance")
OPERATORS = (
    "equals",
    "notEquals",
    "contains",
    "notContains",
    "includingRegex",
    "excludingRegex",
)


def _service(creds: Credentials):
    # cache_discovery=False: googleapiclient otherwise tries to write a discovery
    # cache and emits warnings on a read-only filesystem.
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def _parse_filter(raw: str) -> dict[str, str]:
    """'page contains /blog/' -> a dimensionFilter. The value may contain spaces."""
    parts = raw.strip().split(None, 2)
    if len(parts) < 3:
        raise ToolError(
            f"Filter {raw!r} is not usable. Use 'dimension operator value', for example "
            "'page contains /blog/' or 'country equals usa'."
        )
    dimension, operator, expression = parts
    if dimension not in VALID_DIMENSIONS:
        raise ToolError(
            f"Filter dimension {dimension!r} is not a Search Console dimension. "
            f"Use one of: {', '.join(VALID_DIMENSIONS)}."
        )
    if operator not in OPERATORS:
        raise ToolError(
            f"Filter operator {operator!r} is not supported. Use one of: {', '.join(OPERATORS)}."
        )
    return {"dimension": dimension, "operator": operator, "expression": expression}


def validate_filters(filters: list[str] | None) -> None:
    """Parse-check without building a request, so a tool can reject a bad filter before
    spending a credential load or a quota unit on it."""
    for raw in filters or []:
        _parse_filter(raw)


def _flatten(row: dict[str, Any], dimensions: list[str]) -> dict[str, Any]:
    # strict=False on purpose: if Google ever returns fewer keys than dimensions,
    # truncating is better than raising inside a tool call.
    out: dict[str, Any] = dict(zip(dimensions, row.get("keys") or [], strict=False))
    for metric in METRICS:
        value = row.get(metric)
        if value is None:
            continue
        out[metric] = round(value, 4) if metric in ("ctr", "position") else value
    return out


async def search_analytics(
    creds: Credentials,
    site_url: str,
    start_date: str,
    end_date: str,
    *,
    dimensions: list[str],
    filters: list[str] | None,
    search_type: str,
    row_limit: int,
    data_state: str,
) -> QueryResult:
    """googleapiclient is synchronous. Calling it directly on the event loop stalls
    the stdio transport and the client shows a hung server, so every call in this
    package goes through asyncio.to_thread."""
    page_size = max(1, min(row_limit, API_ROW_MAX))  # API hard maximum
    body: dict[str, Any] = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": dimensions,
        "type": search_type,
        "dataState": data_state.upper(),
        "rowLimit": page_size,
        "startRow": 0,
    }
    if filters:
        body["dimensionFilterGroups"] = [{"filters": [_parse_filter(f) for f in filters]}]

    def work() -> list[dict[str, Any]]:
        svc = _service(creds)
        out: list[dict[str, Any]] = []
        start = 0
        while True:  # paginate until a short page or the caller's cap
            request_body = {**body, "startRow": start}
            resp = svc.searchanalytics().query(siteUrl=site_url, body=request_body).execute()
            page = resp.get("rows", [])
            out.extend(page)
            if len(page) < page_size or len(out) >= row_limit:
                return out[:row_limit]
            start += len(page)

    raw = await asyncio.to_thread(work)
    rows = [_flatten(r, dimensions) for r in raw]
    notes: list[str] = []
    if "query" in dimensions:
        notes.append(
            "Google anonymises rare queries: these rows do NOT sum to property "
            "totals. Do not compute percentages or shares from them."
        )
    if data_state.lower() == "all":
        notes.append("dataState=all includes incomplete recent days; these numbers will change.")
    else:
        notes.append("dataState=final excludes the most recent 2-3 days.")
    return QueryResult(
        rows=rows,
        dimension_keys=tuple(dimensions),
        metric_keys=METRICS,
        source="gsc",
        total_rows=len(rows),
        meta={
            "date_range": f"{start_date}..{end_date}",
            "data_state": data_state,
            "search_type": search_type,
            "site_url": site_url,
        },
        notes=notes,
    )


async def list_sites(creds: Credentials) -> QueryResult:
    def work() -> dict[str, Any]:
        return _service(creds).sites().list().execute()

    resp = await asyncio.to_thread(work)
    rows = [
        {
            "site_url": entry.get("siteUrl"),
            "permission_level": entry.get("permissionLevel"),
            "property_type": "domain"
            if str(entry.get("siteUrl", "")).startswith("sc-domain:")
            else "url-prefix",
        }
        for entry in resp.get("siteEntry", [])
    ]
    notes = []
    if not rows:
        notes.append(
            "This Google account can see no Search Console properties. Either the wrong "
            "account was authorised, or it has not been granted access under "
            "Settings > Users and permissions on the property."
        )
    return QueryResult(
        rows=rows,
        dimension_keys=("site_url", "permission_level", "property_type"),
        metric_keys=(),
        source="gsc",
        total_rows=len(rows),
        notes=notes,
    )


async def inspect_url(creds: Credentials, site_url: str, inspection_url: str) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        body = {
            "inspectionUrl": inspection_url,
            "siteUrl": site_url,
            "languageCode": "en-US",
        }
        return _service(creds).urlInspection().index().inspect(body=body).execute()

    resp = await asyncio.to_thread(work)
    result = resp.get("inspectionResult", {})
    index = result.get("indexStatusResult", {})
    return {
        "inspection_url": inspection_url,
        "verdict": index.get("verdict"),
        "coverage_state": index.get("coverageState"),
        "robots_txt_state": index.get("robotsTxtState"),
        "indexing_state": index.get("indexingState"),
        "last_crawl_time": index.get("lastCrawlTime"),
        "page_fetch_state": index.get("pageFetchState"),
        "google_canonical": index.get("googleCanonical"),
        "user_canonical": index.get("userCanonical"),
        "crawled_as": index.get("crawledAs"),
        "sitemaps": index.get("sitemap") or [],
        "referring_urls": (index.get("referringUrls") or [])[:5],
        "mobile_usability": (result.get("mobileUsabilityResult") or {}).get("verdict"),
        "rich_results": (result.get("richResultsResult") or {}).get("verdict"),
        "inspection_link": result.get("inspectionResultLink"),
    }


async def list_sitemaps(creds: Credentials, site_url: str) -> QueryResult:
    def work() -> dict[str, Any]:
        return _service(creds).sitemaps().list(siteUrl=site_url).execute()

    resp = await asyncio.to_thread(work)
    rows = []
    for entry in resp.get("sitemap", []):
        counts = entry.get("contents") or []
        rows.append(
            {
                "path": entry.get("path"),
                "type": entry.get("type"),
                "last_submitted": entry.get("lastSubmitted"),
                "last_downloaded": entry.get("lastDownloaded"),
                "is_pending": entry.get("isPending"),
                "is_sitemaps_index": entry.get("isSitemapsIndex"),
                "warnings": int(entry.get("warnings") or 0),
                "errors": int(entry.get("errors") or 0),
                "submitted_urls": sum(int(c.get("submitted") or 0) for c in counts),
            }
        )
    return QueryResult(
        rows=rows,
        dimension_keys=(
            "path",
            "type",
            "last_submitted",
            "last_downloaded",
            "is_pending",
            "is_sitemaps_index",
        ),
        metric_keys=("warnings", "errors", "submitted_urls"),
        source="gsc",
        total_rows=len(rows),
        meta={"site_url": site_url},
    )


async def submit_sitemap(creds: Credentials, site_url: str, feedpath: str) -> dict[str, Any]:
    """The only Search Console write in this server. A 204 with no body is success."""

    def work() -> None:
        _service(creds).sitemaps().submit(siteUrl=site_url, feedpath=feedpath).execute()

    await asyncio.to_thread(work)
    return {"submitted": feedpath, "site_url": site_url, "status": "accepted"}
