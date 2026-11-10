"""Mailchimp Marketing API v3.

The key carries its own data centre as a suffix - a key ending `-us3` must be sent to
us3.api.mailchimp.com, and the bare host fails - so the base URL is derived from the
key rather than configured separately.

Read-only by construction: this module only ever issues GET requests.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from ..shaping import QueryResult, aggregate_rows, normalise_url

API = "mailchimp.com"
VERSION = "3.0"
PAGE_MAX = 1000  # API maximum for `count`
# Mailchimp allows 10 simultaneous connections per account; stay well under it so a
# burst of tool calls cannot starve anything else using the same key.
_GATE = asyncio.Semaphore(4)

CAMPAIGN_METRICS = (
    "emails_sent",
    "unique_opens",
    "unique_clicks",
    "open_rate",
    "click_rate",
    "unsubscribes",
    "bounces",
)


@dataclass(frozen=True)
class Account:
    """Where a key points, and who it belongs to. Never holds the key itself."""

    account_name: str
    email: str
    total_subscribers: int | None
    data_center: str


def data_center(key: str) -> str:
    """The suffix after the final '-'. Keys without one are not usable."""
    _, _, dc = (key or "").rpartition("-")
    if not dc or not dc.replace("us", "").isdigit():
        raise ToolError(
            "The Mailchimp API key does not end in a data-centre suffix such as '-us3'. "
            "Copy it again from Account & billing > Extras > API keys; the suffix is "
            "part of the key and the request cannot be routed without it."
        )
    return dc


def _base(key: str) -> str:
    return f"https://{data_center(key)}.api.{API}/{VERSION}"


def _fail(response: httpx.Response) -> ToolError:
    """Mailchimp returns RFC 7807 problem documents; use its own words where it has any."""
    detail = ""
    try:
        body = response.json()
        detail = str(body.get("detail") or body.get("title") or "")
    except ValueError:
        detail = response.text[:200]

    status = response.status_code
    if status == 401:
        return ToolError(
            "Mailchimp rejected the API key. Generate a fresh one under Account & billing "
            "> Extras > API keys and set MAILCHIMP_API_KEY. A key also stops working if "
            "the user who created it is removed from the account."
        )
    if status == 403:
        return ToolError(
            f"Mailchimp refused this request: {detail or 'forbidden'}. The key is scoped to "
            "one account, so this usually means the audience or campaign belongs to a "
            "different Mailchimp account."
        )
    if status == 404:
        return ToolError(
            f"Mailchimp has no such record: {detail or 'not found'}. List what exists with "
            "mailchimp_list_audiences or mailchimp_list_campaigns - IDs are per account."
        )
    if status == 429:
        return ToolError(
            "Mailchimp rate-limited this request. It allows 10 simultaneous connections "
            "per account; wait a few seconds and ask for less at once."
        )
    if status >= 500:
        return ToolError(f"Mailchimp returned a {status} server error. This is transient - retry.")
    return ToolError(f"Mailchimp rejected the request ({status}): {detail or 'no detail given'}")


async def _get(key: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    async with _GATE:
        async with httpx.AsyncClient(timeout=25) as client:
            try:
                r = await client.get(
                    f"{_base(key)}{path}",
                    params=params,
                    auth=("seo-analytics-mcp", key),  # Basic: any username, key as password
                )
            except httpx.HTTPError as exc:
                raise ToolError(f"Could not reach Mailchimp at {_base(key)}{path}: {exc}") from exc
    if r.status_code >= 400:
        raise _fail(r)
    try:
        return r.json()
    except ValueError as exc:  # pragma: no cover - Mailchimp always returns JSON
        raise ToolError(f"Mailchimp returned a non-JSON response: {r.text[:160]}") from exc


async def ping(key: str) -> Account:
    """Cheapest possible credential check, and the only call auth_status needs."""
    await _get(key, "/ping")
    root = await _get(key, "/", {"fields": "account_name,email,total_subscribers"})
    return Account(
        account_name=root.get("account_name") or "unknown",
        email=root.get("email") or "unknown",
        total_subscribers=root.get("total_subscribers"),
        data_center=data_center(key),
    )


async def list_audiences(key: str) -> QueryResult:
    payload = await _get(
        key,
        "/lists",
        {
            "count": PAGE_MAX,
            "fields": "lists.id,lists.name,lists.stats,lists.date_created,total_items",
        },
    )
    rows = []
    for item in payload.get("lists", []):
        stats = item.get("stats") or {}
        rows.append(
            {
                "audience_id": item.get("id"),
                "name": item.get("name"),
                "created": (item.get("date_created") or "")[:10],
                "members": int(stats.get("member_count") or 0),
                "unsubscribed": int(stats.get("unsubscribe_count") or 0),
                "open_rate": round(float(stats.get("open_rate") or 0) / 100, 4),
                "click_rate": round(float(stats.get("click_rate") or 0) / 100, 4),
            }
        )
    notes = []
    if not rows:
        notes.append(
            "This Mailchimp account has no audiences. Either the key belongs to a "
            "different account, or no list has been created yet."
        )
    else:
        notes.append(
            "open_rate and click_rate here are Mailchimp's lifetime averages for the "
            "audience, not figures for any particular period."
        )
    return QueryResult(
        rows=rows,
        dimension_keys=("audience_id", "name", "created"),
        metric_keys=("members", "unsubscribed", "open_rate", "click_rate"),
        source="mailchimp",
        total_rows=payload.get("total_items", len(rows)),
        meta={"account": "mailchimp"},
        notes=notes,
    )


async def list_campaigns(
    key: str, *, since: str | None = None, before: str | None = None, limit: int = 50
) -> QueryResult:
    params: dict[str, Any] = {
        "count": max(1, min(limit, PAGE_MAX)),
        "status": "sent",
        "sort_field": "send_time",
        "sort_dir": "DESC",
        "fields": (
            "campaigns.id,campaigns.send_time,campaigns.settings.subject_line,"
            "campaigns.settings.title,campaigns.recipients.list_name,"
            "campaigns.emails_sent,campaigns.report_summary,total_items"
        ),
    }
    if since:
        params["since_send_time"] = f"{since}T00:00:00+00:00"
    if before:
        params["before_send_time"] = f"{before}T23:59:59+00:00"

    payload = await _get(key, "/campaigns", params)
    rows = []
    for item in payload.get("campaigns", []):
        summary = item.get("report_summary") or {}
        settings = item.get("settings") or {}
        rows.append(
            {
                "campaign_id": item.get("id"),
                "sent": (item.get("send_time") or "")[:10],
                "subject": settings.get("subject_line") or settings.get("title") or "(untitled)",
                "audience": (item.get("recipients") or {}).get("list_name") or "",
                "emails_sent": int(item.get("emails_sent") or 0),
                "unique_opens": int(summary.get("unique_opens") or 0),
                "unique_clicks": int(summary.get("subscriber_clicks") or 0),
                "open_rate": round(float(summary.get("open_rate") or 0), 4),
                "click_rate": round(float(summary.get("click_rate") or 0), 4),
            }
        )
    notes = [
        "Only campaigns Mailchimp has finished sending are included; drafts and "
        "scheduled campaigns are omitted."
    ]
    if not rows:
        notes.append("No sent campaigns fall in this range.")
    return QueryResult(
        rows=rows,
        dimension_keys=("campaign_id", "sent", "subject", "audience"),
        metric_keys=("emails_sent", "unique_opens", "unique_clicks", "open_rate", "click_rate"),
        source="mailchimp",
        total_rows=payload.get("total_items", len(rows)),
        meta={"account": "mailchimp"},
        notes=notes,
    )


async def campaign_report(key: str, campaign_id: str) -> dict[str, Any]:
    report = await _get(key, f"/reports/{campaign_id}")
    opens = report.get("opens") or {}
    clicks = report.get("clicks") or {}
    bounces = report.get("bounces") or {}
    sent = int(report.get("emails_sent") or 0)

    def rate(n: int) -> float | None:
        return round(n / sent, 4) if sent else None

    unsubs = int(report.get("unsubscribed") or 0)
    hard, soft = int(bounces.get("hard_bounces") or 0), int(bounces.get("soft_bounces") or 0)
    return {
        "campaign_id": campaign_id,
        "subject": (report.get("subject_line") or report.get("campaign_title") or ""),
        "sent": (report.get("send_time") or "")[:10],
        "audience": (report.get("list_name") or ""),
        "emails_sent": sent,
        "unique_opens": int(opens.get("unique_opens") or 0),
        "open_rate": round(float(opens.get("open_rate") or 0), 4),
        "unique_clicks": int(clicks.get("unique_subscriber_clicks") or 0),
        "click_rate": round(float(clicks.get("click_rate") or 0), 4),
        "unsubscribes": unsubs,
        "unsubscribe_rate": rate(unsubs),
        "bounces": hard + soft,
        "bounce_rate": rate(hard + soft),
        "abuse_reports": int(report.get("abuse_reports") or 0),
    }


def _same_site(host: str, site_host: str) -> bool:
    """The site and its subdomains count; a host that merely ends in the same letters
    does not, so 'notexample.com' is not matched by 'example.com'."""
    return host == site_host or host.endswith("." + site_host)


async def campaign_links(
    key: str, campaign_id: str, *, limit: int = 100, site_host: str | None = None
) -> QueryResult:
    """Which links recipients clicked, with on-site links carrying a joinable path.

    `path` is the key Search Console pages and GA4 landing pages reduce to, so email
    clicks line up against organic traffic for the same page. It is set ONLY for links
    on `site_host`: most campaigns link to LinkedIn, Facebook and YouTube in the footer,
    and those reduce to paths like '/company/acme' - or '/' - which would join against
    unrelated pages of the site, and against its homepage.
    """
    payload = await _get(
        key,
        f"/reports/{campaign_id}/click-details",
        {"count": max(1, min(limit, PAGE_MAX)), "sort_field": "total_clicks", "sort_dir": "DESC"},
    )
    placements = []
    for item in payload.get("urls_clicked", []):
        url = item.get("url") or ""
        host = (urlsplit(url).hostname or "").removeprefix("www.")
        on_site = bool(site_host) and _same_site(host, site_host)
        placements.append(
            {
                "url": url,
                "host": host,
                "path": normalise_url(url) if (url and on_site) else "",
                "clicks": int(item.get("total_clicks") or 0),
                "unique_clicks": int(item.get("unique_clicks") or 0),
            }
        )

    # One URL placed twice in an email gets two tracked link ids, each with its own real
    # clicks - the campaign's clicks_total counts both. So they are summed, not dropped:
    # the question is "which links were clicked", and two identical rows answer nothing.
    first: dict[str, dict[str, Any]] = {}
    for row in placements:
        first.setdefault(row["url"], row)
    merged = aggregate_rows(placements, key="url", metric_keys=("clicks", "unique_clicks"))
    rows = [
        {
            "url": url,
            "host": first[url]["host"],
            "path": first[url]["path"],
            "clicks": group["clicks"],
            "unique_clicks": group["unique_clicks"],
        }
        for url, group in merged.items()
    ]
    rows.sort(key=lambda r: r["clicks"], reverse=True)
    collapsed = sum(1 for g in merged.values() if g.get("_merged_from", 1) > 1)

    upstream = int(payload.get("total_items") or len(placements))
    notes = []
    if site_host:
        notes.append(
            f"'path' is set only for links on {site_host} and is directly comparable "
            "with the 'path' key on GA4 landing pages and Search Console page rows. "
            "Off-site links carry 'host' and an empty 'path' - do not join on those."
        )
    else:
        notes.append(
            "'path' is empty on every row because no site is configured, so on-site "
            "links cannot be told from links to social networks. Set GSC_DEFAULT_SITE "
            "to have paths that join against GA4 and Search Console."
        )
    if collapsed:
        notes.append(
            f"{collapsed} URL(s) appeared more than once in the email; each placement is "
            "tracked separately by Mailchimp and their clicks are summed here. Summed "
            "'unique_clicks' can therefore exceed the number of distinct people, since "
            "someone who clicked two placements of one link counts in each."
        )
    notes.extend(
        [
            # Both directions are real, and the overcount is the larger error by far:
            # scanners click every link, which is why footer and social links collect
            # implausible totals. Compare with the campaign report's 'unique_clicks'.
            "'unique_clicks' here counts one per link per subscriber, so these rows sum "
            "to far more than the number of people who clicked. Security scanners and "
            "link prefetchers also open and click every link in a message. For distinct "
            "people, use 'unique_clicks' from mailchimp_campaign_report instead.",
            "Click counts also miss recipients whose mail client blocked tracking "
            "entirely, so an individual link can undercount too.",
        ]
    )
    return QueryResult(
        rows=rows,
        dimension_keys=("url", "host", "path"),
        metric_keys=("clicks", "unique_clicks"),
        source="mailchimp",
        # After grouping, the matched count is the number of distinct URLs - but only
        # when every placement was fetched. Otherwise upstream really does hold more.
        total_rows=len(rows) if len(placements) >= upstream else upstream,
        meta={"account": "mailchimp", "campaign_id": campaign_id},
        notes=notes,
    )
