"""LinkedIn tools: how the company Page is performing. Statistics only.

Every tool here is a read. There is no tool that posts, edits or deletes anything on
LinkedIn, and the client beneath them issues GET requests exclusively.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import cache, config
from ..clients import linkedin
from ..server import mcp
from ..shaping import DateRangeError, ShapedResponse, resolve_date, shape
from . import _base

TTL = 3 * 3600  # LinkedIn recomputes statistics slowly; a stale hour costs nothing


def _token() -> str:
    token = config.active_profile().linkedin_token
    if not token:
        raise ToolError(
            "No LinkedIn access token is configured. Create an app at "
            "developer.linkedin.com, request the Community Management API product, then "
            "authorise a member who administers the Page and set LINKEDIN_ACCESS_TOKEN "
            "in the server's environment. The token is never passed as a tool argument, "
            "because tool arguments are visible in the transcript. Note that LinkedIn "
            "tokens expire after about 60 days and must be replaced by hand."
        )
    return token


def _version() -> str:
    return config.active_profile().linkedin_version or linkedin.DEFAULT_VERSION


def _organization(value: str | None) -> str:
    chosen = value or config.active_profile().linkedin_organization
    if not chosen:
        raise ToolError(
            "No LinkedIn organization was given and LINKEDIN_ORGANIZATION_ID is not set. "
            "Run linkedin_list_organizations to see the Pages this token administers."
        )
    return linkedin.organization_urn(chosen)  # reject a bad id here, not as "no access"


def _window(start: str | None, end: str | None) -> tuple[str | None, str | None]:
    """Both ends or neither. One alone would silently become a lifetime total."""
    if not start and not end:
        return None, None
    if bool(start) != bool(end):
        raise ToolError(
            "Give both start_date and end_date, or neither. One on its own would be "
            "answered with lifetime totals, which is not what was asked."
        )
    try:
        first, last = resolve_date(start or ""), resolve_date(end or "")
    except DateRangeError as exc:
        raise ToolError(str(exc)) from exc
    if first > last:
        raise ToolError(f"start_date {first} is after end_date {last}. Swap them.")
    return first, last


@mcp.tool(annotations=_base.READ_ONLY)
async def linkedin_list_organizations() -> dict[str, Any]:
    """LinkedIn Pages this server can read, with each one's follower total.

    Run this first: the other LinkedIn tools need the numeric organization id, and this
    is what proves the token actually administers a Page rather than merely being valid.
    """
    profile = config.active_profile()
    token, version = _token(), _version()
    ck = cache.key(profile, "linkedin.orgs", {"version": version})

    async def producer():
        from ..shaping import QueryResult

        orgs = await linkedin.list_organizations(token, version)
        return QueryResult(
            rows=[
                {
                    "organization_id": o.organization_id,
                    "name": o.name,
                    "vanity_name": o.vanity_name,
                    "followers": o.followers,
                }
                for o in orgs
            ],
            dimension_keys=("organization_id", "name", "vanity_name"),
            metric_keys=("followers",),
            source="linkedin",
            total_rows=len(orgs),
            meta={"account": "linkedin"},
        )

    result = await cache.get_or_set(profile, ck, ttl=TTL, producer=producer)
    notes = []
    if not result.rows:
        notes.append(
            "The token is valid but administers no LinkedIn Page. Statistics come back "
            "only for organizations where the signing member has an ADMINISTRATOR role, "
            "so granting that role is a separate step from creating the token."
        )
    return {
        "organizations": result.rows,
        "count": len(result.rows),
        "notes": notes
        + [
            "'followers' is the current running total. Followers gained over a period "
            "come from linkedin_follower_stats instead.",
        ],
    }


@mcp.tool(annotations=_base.READ_ONLY)
async def linkedin_share_stats(
    organization_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> ShapedResponse:
    """How the Page's posts performed: impressions, clicks, reactions and engagement.

    With no dates these are lifetime totals. With dates, one row per day. Engagement
    rate is recomputed from the counts, so it stays right however rows are combined.
    """
    profile = config.active_profile()
    # Arguments first. Checking the credential before them reports a malformed date
    # range as "no token configured", which sends the reader after the wrong problem.
    org = _organization(organization_id)
    start, end = _window(start_date, end_date)
    token, version = _token(), _version()
    ck = cache.key(
        profile, "linkedin.shares", {"org": org, "start": start, "end": end, "version": version}
    )
    result = await cache.get_or_set(
        profile,
        ck,
        ttl=TTL,
        producer=lambda: linkedin.share_stats(token, org, version=version, start=start, end=end),
    )
    return shape(result, top_n=400, export_dir=_base.export_dir(profile))


@mcp.tool(annotations=_base.READ_ONLY)
async def linkedin_follower_stats(
    organization_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> ShapedResponse:
    """Followers the Page gained, split into organic and paid.

    These are gains, not the running total - linkedin_list_organizations reports that.
    """
    profile = config.active_profile()
    # Arguments first. Checking the credential before them reports a malformed date
    # range as "no token configured", which sends the reader after the wrong problem.
    org = _organization(organization_id)
    start, end = _window(start_date, end_date)
    token, version = _token(), _version()
    ck = cache.key(
        profile, "linkedin.followers", {"org": org, "start": start, "end": end, "version": version}
    )
    result = await cache.get_or_set(
        profile,
        ck,
        ttl=TTL,
        producer=lambda: linkedin.follower_stats(token, org, version=version, start=start, end=end),
    )
    return shape(result, top_n=400, sort_by=None, export_dir=_base.export_dir(profile))


@mcp.tool(annotations=_base.READ_ONLY)
async def linkedin_page_stats(
    organization_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> ShapedResponse:
    """Views of the LinkedIn Page itself, broken down by which part was viewed.

    This is traffic to the Page on LinkedIn. Visits it sent to the website are in
    Analytics, under the linkedin.com referral.
    """
    profile = config.active_profile()
    # Arguments first. Checking the credential before them reports a malformed date
    # range as "no token configured", which sends the reader after the wrong problem.
    org = _organization(organization_id)
    start, end = _window(start_date, end_date)
    token, version = _token(), _version()
    ck = cache.key(
        profile, "linkedin.page", {"org": org, "start": start, "end": end, "version": version}
    )
    result = await cache.get_or_set(
        profile,
        ck,
        ttl=TTL,
        producer=lambda: linkedin.page_stats(token, org, version=version, start=start, end=end),
    )
    return shape(result, top_n=100, sort_by="views", export_dir=_base.export_dir(profile))
