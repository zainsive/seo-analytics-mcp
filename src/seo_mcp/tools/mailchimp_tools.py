"""Mailchimp tools: what we sent, how it landed, and which pages it drove traffic to."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from mcp.server.mcpserver.exceptions import ToolError

from .. import cache, config
from ..clients import mailchimp
from ..identity import caller
from ..server import mcp
from ..shaping import DateRangeError, ShapedResponse, resolve_date, shape
from ..sso import connections
from . import _base

TTL = 3 * 3600  # campaign stats keep moving for a day or two after a send


def _access() -> mailchimp.Access:
    """Whose Mailchimp this reads, and the one place that decides.

    The person's own connection comes first. Unlike Google, where everybody who signs
    in necessarily has an account to read with, somebody may have no Mailchimp login
    at all - seats are limited and most people never get one - so the shared key stays
    as a fallback rather than removing access the team already has. Which one was used
    is reported with every answer, because a figure read through a shared credential
    and one read as yourself can differ, and nothing else on the page would say so.
    """
    subject = caller()
    if subject:
        if link := connections.recall(subject, connections.MAILCHIMP):
            connections.touch(subject, connections.MAILCHIMP)
            return mailchimp.Access.from_oauth(
                link.refresh_token, str(link.meta.get("data_center") or "")
            )
    if key := config.active_profile().mailchimp_key:
        return mailchimp.Access.from_api_key(key)
    raise ToolError(
        f"No Mailchimp account is connected for {subject}, and this server holds no "
        "shared key either. Call connect_account with 'mailchimp' to connect your own."
        if subject
        else "No Mailchimp API key is configured. Create one in Mailchimp under "
        "Account & billing > Extras > API keys, then set MAILCHIMP_API_KEY in the "
        "server's environment. The key is never passed as a tool argument, because "
        "tool arguments are visible in the transcript."
    )


def _notes() -> list[str]:
    """Said only when it would otherwise mislead.

    A person reading their own Mailchimp needs no announcement - that is what they
    expect. A person reading through the shared key does, because the figures can
    differ from what their own login would show and nothing else in the answer says
    so. A caller that is not a person has nobody to mislead, so it stays quiet.
    """
    subject = caller()
    if subject is None or connections.recall(subject, connections.MAILCHIMP):
        return []
    return [
        "Read through this server's shared Mailchimp key rather than your own login, "
        "so it shows everything that key can reach. Call connect_account with "
        "'mailchimp' to read as yourself instead."
    ]


def _tag(access: mailchimp.Access) -> str:
    """Folded into the cache key, so connecting your own account part-way through a
    conversation cannot be answered from the shared key's cached rows."""
    from ..sso import store

    return store.digest(access.token)[:12]


def _site_host() -> str | None:
    """The site whose links count as on-site. Without it a link to the user's own
    /pricing cannot be told from a LinkedIn link, and neither can be joined safely."""
    profile = config.active_profile()
    site = profile.default_site
    if site:
        if site.startswith("sc-domain:"):
            return site.removeprefix("sc-domain:").removeprefix("www.") or None
        host = urlsplit(site).hostname
        if host:
            return host.removeprefix("www.")
    return (profile.indexnow_host or "").removeprefix("www.") or None


def _automation_note(report: dict[str, Any], links: list[dict[str, Any]]) -> list[str]:
    """Flag a campaign whose clicks cannot all be human.

    Nobody clicks a link they never opened, so link clicks running well past unique
    opens means something other than recipients is doing the clicking - corporate mail
    scanners and link prefetchers fetch every URL in a message. Left unflagged, the
    per-link rows read as three times the real audience.
    """
    clicks = sum(int(r.get("clicks") or 0) for r in links)
    opens, people = int(report.get("unique_opens") or 0), int(report.get("unique_clicks") or 0)
    if not (opens and clicks > 1.5 * opens):
        return []
    return [
        f"This campaign records {clicks:,} link clicks against only {opens:,} unique "
        f"opens. Clicks exceeding opens is the signature of security scanners and link "
        f"prefetchers following every link, so the per-link counts are inflated - "
        f"most heavily on footer and social links. {people:,} distinct subscribers "
        f"clicked something, and that is the figure to quote."
    ]


def _day(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return resolve_date(value)
    except DateRangeError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=_base.READ_ONLY)
async def mailchimp_list_audiences() -> ShapedResponse:
    """List the Mailchimp audiences (mailing lists) on this account, with how many
    people are on each and their lifetime open and click rates. Call this first to get
    an audience name or id before asking about a particular list.
    """
    profile = config.active_profile()
    access = _access()
    ck = cache.key(profile, "mailchimp.lists", {"cred": _tag(access)})
    result = await cache.get_or_set(
        profile, ck, ttl=cache.TTL_DISCOVERY, producer=lambda: mailchimp.list_audiences(access)
    )
    return shape(result, top_n=100, sort_by="members", notes=_notes())


@mcp.tool(annotations=_base.READ_ONLY)
async def mailchimp_list_campaigns(
    since: str | None = None, before: str | None = None, limit: int = 50
) -> ShapedResponse:
    """List sent email campaigns, newest first, with how many were delivered, opened
    and clicked.

    since and before are dates (YYYY-MM-DD, 'today', 'yesterday' or 'NdaysAgo') and
    filter by send time. Only finished sends are included - drafts and scheduled
    campaigns are not. Open and click rates are per campaign; the summary recomputes
    them across the whole set rather than averaging the per-campaign figures.
    """
    since, before = _day(since), _day(before)
    if since and before and since > before:
        raise ToolError(f"since {since} is after before {before}. Swap them.")
    profile = config.active_profile()
    access = _access()
    ck = cache.key(
        profile,
        "mailchimp.campaigns",
        {"since": since, "before": before, "limit": limit, "cred": _tag(access)},
    )
    result = await cache.get_or_set(
        profile,
        ck,
        ttl=TTL,
        producer=lambda: mailchimp.list_campaigns(access, since=since, before=before, limit=limit),
    )
    out = shape(
        result,
        top_n=limit,
        sort_by="sent",
        export_dir=_base.export_dir(profile),
        notes=_notes(),
    )
    if since or before:
        out["summary"]["date_range"] = f"{since or 'any'}..{before or 'any'}"
    return out


@mcp.tool(annotations=_base.READ_ONLY)
async def mailchimp_campaign_report(campaign_id: str) -> dict[str, Any]:
    """Full results for one campaign: delivered, opens, clicks, unsubscribes, bounces
    and abuse reports, with every rate recomputed from its own components.

    Get a campaign_id from mailchimp_list_campaigns. Also returns the links that were
    clicked, each with a normalised 'path' that can be compared directly against
    Search Console page rows and GA4 landing pages for the same page.
    """
    profile = config.active_profile()
    access = _access()
    host = _site_host()
    ck = cache.key(
        profile,
        "mailchimp.report",
        {"campaign_id": campaign_id, "host": host, "cred": _tag(access)},
    )

    async def producer():
        from ..shaping import QueryResult

        report = await mailchimp.campaign_report(access, campaign_id)
        links = await mailchimp.campaign_links(access, campaign_id, limit=50, site_host=host)
        return QueryResult(
            rows=[report],
            dimension_keys=tuple(report),
            metric_keys=(),
            source="mailchimp",
            total_rows=1,
            meta={"links": links.rows, "link_notes": links.notes},
        )

    result = await cache.get_or_set(profile, ck, ttl=TTL, producer=producer)
    report = _base.as_rows(result.rows[0]) if result.rows else {}
    links = result.meta.get("links") or []
    return {
        "campaign": report,
        "links_clicked": links[:25],
        "links_total": len(links),
        "notes": _notes()
        + list(result.meta.get("link_notes") or [])
        + _automation_note(report, links)
        + [
            "Opens are measured by a tracking pixel, so recipients who block images are "
            "not counted. Treat the open rate as a floor, not a precise figure.",
        ],
    }


@mcp.tool(annotations=_base.READ_ONLY)
async def mailchimp_campaign_links(campaign_id: str, limit: int = 50) -> ShapedResponse:
    """Which links a campaign's recipients clicked, most-clicked first.

    Rows for links on the configured site carry a normalised 'path' alongside the full
    URL, so they line up with the 'path' key on GA4 landing pages and Search Console
    page rows - use it to compare a campaign's traffic to a page with that page's
    organic traffic. Off-site links (social footers and the like) carry their 'host'
    and an empty 'path'.
    """
    profile = config.active_profile()
    access = _access()
    host = _site_host()
    ck = cache.key(
        profile,
        "mailchimp.clicks",
        {"campaign_id": campaign_id, "limit": limit, "host": host, "cred": _tag(access)},
    )
    result = await cache.get_or_set(
        profile,
        ck,
        ttl=TTL,
        producer=lambda: mailchimp.campaign_links(access, campaign_id, limit=limit, site_host=host),
    )
    return shape(
        result,
        top_n=limit,
        sort_by="clicks",
        export_dir=_base.export_dir(profile),
        notes=_notes(),
    )
