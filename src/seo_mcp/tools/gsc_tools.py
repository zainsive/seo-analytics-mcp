"""Search Console tools. Argument surface, docstrings, cache TTL choice, shape()."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import cache, config
from ..auth import scopes, store
from ..clients import gsc
from ..server import mcp
from ..shaping import DateRangeError, ShapedResponse, resolve_range, shape
from . import _base
from ._base import google_tool, locals_without

API = gsc.API


def _range(start_date: str, end_date: str) -> tuple[str, str, list[str]]:
    try:
        return resolve_range(start_date, end_date, source="gsc")
    except DateRangeError as exc:
        raise ToolError(str(exc)) from exc


def _site(site_url: str | None) -> str:
    return _base.require(
        site_url or config.active_profile().default_site,
        what="site_url",
        env_var="GSC_DEFAULT_SITE",
        lister="gsc_list_sites",
    )


@mcp.tool(annotations=_base.READ_ONLY)
@google_tool(api=API)
async def gsc_list_sites() -> ShapedResponse:
    """List the Search Console properties this Google account can read, with the
    permission level held on each. Call this first in any session: the exact string
    returned here is what every other Search Console tool needs as site_url.
    Domain properties look like 'sc-domain:example.com'; URL-prefix properties are
    full URLs with a trailing slash.
    """
    profile = config.active_profile()
    creds = store.load(profile, {scopes.GSC_READ})
    key = cache.key(profile, "gsc.sites.list", {})
    result = await cache.get_or_set(
        profile, key, ttl=cache.TTL_DISCOVERY, producer=lambda: gsc.list_sites(creds)
    )
    return shape(result, top_n=200)


@mcp.tool(annotations=_base.READ_ONLY)
@google_tool(api=API)
async def gsc_search_analytics(
    site_url: str | None = None,
    start_date: str = "28daysAgo",
    end_date: str = "yesterday",
    dimensions: list[str] | None = None,
    filters: list[str] | None = None,
    search_type: str = "web",
    row_limit: int = 250,
    data_state: str = "final",
) -> ShapedResponse:
    """Query Search Console performance data: clicks, impressions, CTR, position.

    Dates are YYYY-MM-DD, or 'today' / 'yesterday' / 'NdaysAgo'; the absolute range
    actually used is echoed in summary.date_range. dimensions may include query, page,
    country, device, date (default ["query"]). filters are "dimension operator value",
    e.g. "page contains /blog/". search_type is web, image, video, news or discover.
    data_state "final" excludes the most recent 2-3 incomplete days; "all" is fresher
    but unstable. Rows carrying the 'query' dimension are anonymised by Google and will
    NOT sum to property totals - never compute shares or percentages from them.
    """
    site_url = _site(site_url)
    start_date, end_date, range_notes = _range(start_date, end_date)
    dimensions = dimensions or ["query"]
    gsc.validate_filters(filters)  # before any credential is loaded
    unknown = [d for d in dimensions if d not in gsc.VALID_DIMENSIONS]
    if unknown:
        raise ToolError(
            f"Unknown Search Console dimension(s): {', '.join(unknown)}. "
            f"Use any of: {', '.join(gsc.VALID_DIMENSIONS)}."
        )
    profile = config.active_profile()
    creds = store.load(profile, {scopes.GSC_READ})
    key = cache.key(profile, "gsc.searchanalytics", locals_without())

    result = await cache.get_or_set(
        profile,
        key,
        ttl=cache.TTL_GSC_LIVE if data_state.lower() == "all" else cache.TTL_GSC,
        producer=lambda: gsc.search_analytics(
            creds,
            site_url,
            start_date,
            end_date,
            dimensions=dimensions,
            filters=filters,
            search_type=search_type,
            row_limit=row_limit,
            data_state=data_state,
        ),
    )
    out = shape(result, top_n=row_limit, export_dir=profile.export_dir)
    out["notes"].extend(range_notes)
    return out


@mcp.tool(annotations=_base.READ_ONLY)
@google_tool(api=API)
async def gsc_inspect_url(inspection_url: str, site_url: str | None = None) -> dict[str, Any]:
    """Inspect one URL's index status in Google: coverage verdict, last crawl time,
    Google's chosen canonical versus the declared one, robots.txt state, mobile
    usability and rich-result verdicts. inspection_url must be a full URL that belongs
    to site_url. This is the tightest Google quota in the set (2,000 calls per day per
    property), so results are cached for an hour.
    """
    site_url = _site(site_url)
    profile = config.active_profile()
    creds = store.load(profile, {scopes.GSC_READ})
    key = cache.key(profile, "gsc.urlInspection", locals_without())

    async def producer():
        from ..shaping import QueryResult

        data = await gsc.inspect_url(creds, site_url, inspection_url)
        return QueryResult(
            rows=[data], dimension_keys=tuple(data), metric_keys=(), source="gsc", total_rows=1
        )

    result = await cache.get_or_set(profile, key, ttl=cache.TTL_INSPECT, producer=producer)
    return _base.as_rows(result.rows[0]) if result.rows else {}


@mcp.tool(annotations=_base.READ_ONLY)
@google_tool(api=API)
async def gsc_list_sitemaps(site_url: str | None = None) -> ShapedResponse:
    """List the sitemaps submitted for a property, with when each was last downloaded
    by Google and how many warnings and errors it carries. A sitemap Google has never
    downloaded, or one with errors, is usually the answer to 'why isn't this indexed'.
    """
    site_url = _site(site_url)
    profile = config.active_profile()
    creds = store.load(profile, {scopes.GSC_READ})
    key = cache.key(profile, "gsc.sitemaps.list", locals_without())
    result = await cache.get_or_set(
        profile,
        key,
        ttl=cache.TTL_INSPECT,
        producer=lambda: gsc.list_sitemaps(creds, site_url),
    )
    return shape(result, top_n=100)


@mcp.tool(annotations=_base.DESTRUCTIVE)
@google_tool(api=API, scope=scopes.GSC_WRITE)
async def gsc_submit_sitemap(
    feedpath: str, site_url: str | None = None, confirm: bool = False
) -> dict[str, Any]:
    """Submit or re-ping a sitemap to Search Console. THIS WRITES TO THE USER'S LIVE
    PROPERTY. It needs the write scope, which is not granted by default, and it is a
    dry run unless confirm=true. Show the dry run to the user and get their explicit
    agreement before calling it again with confirm=true. feedpath must be the full
    sitemap URL, e.g. https://example.com/sitemap.xml.
    """
    site_url = _site(site_url)
    profile = config.active_profile()
    if not feedpath.startswith(("http://", "https://")):
        raise ToolError(
            f"feedpath must be a full sitemap URL, got {feedpath!r}. "
            "Example: https://example.com/sitemap.xml"
        )
    if not confirm:
        return {
            "dry_run": True,
            "would_submit": feedpath,
            "site_url": site_url,
            "next_step": (
                "Show this to the user. Call again with confirm=true only after they "
                "have agreed to submit this sitemap to their live property."
            ),
        }
    creds = store.load(profile, scopes.for_write())
    result = await gsc.submit_sitemap(creds, site_url, feedpath)
    cache.clear(profile)  # the sitemap list just changed
    return {"dry_run": False, **result}
