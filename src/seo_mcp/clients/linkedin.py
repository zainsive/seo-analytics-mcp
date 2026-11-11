"""LinkedIn Community Management API, statistics only.

Read-only by construction: this module only ever issues GET requests.

Three things make LinkedIn unlike the other sources here.

The credential is a *member* token, not an application key. LinkedIn has no service
account: statistics come back only for organizations the signing member administers, so
the token carries one person's access and expires after about 60 days. That is the same
shape as the personal OAuth token this project already abandoned once, so the error
messages name the expiry explicitly rather than leaving a dead deployment looking broken.

Every versioned call must carry a `Linkedin-Version: YYYYMM` header and LinkedIn
retires versions on a rolling basis. There is no sensible default that stays correct,
so the version is configuration with a current value as its fallback.

Statistics arrive as counts plus LinkedIn's own precomputed rates. The rates are
dropped and recomputed from the counts, because a rate that is averaged or summed
across rows is wrong - the fourth time that has come up in this codebase.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from ..shaping import QueryResult

BASE = "https://api.linkedin.com/rest"
DEFAULT_VERSION = "202601"
# LinkedIn throttles per application and per member daily. Keep concurrency low: a
# burst that trips the limit penalises every other caller sharing the token.
_GATE = asyncio.Semaphore(3)

ORG_URN = "urn:li:organization:{}"

# What LinkedIn sends back, and what each means in plain terms.
SHARE_METRICS = (
    "impressions",
    "clicks",
    "likes",
    "comments",
    "shares",
    "engagements",
    "engagement_rate",
)
FOLLOWER_METRICS = ("organic_gain", "paid_gain", "total_gain")


@dataclass(frozen=True)
class Organization:
    """A Page the token can read. Never holds the token itself."""

    organization_id: str
    name: str
    vanity_name: str | None
    followers: int | None


def organization_urn(value: str) -> str:
    """Accept a bare id, a vanity path, or a full URN; emit a URN.

    Models produce all three, and a malformed URN comes back as an opaque 400 from
    LinkedIn rather than anything a reader could act on.
    """
    raw = (value or "").strip()
    if not raw:
        raise ToolError(
            "No LinkedIn organization was given. Run linkedin_list_organizations to see "
            "the Pages this token administers, or set LINKEDIN_ORGANIZATION_ID."
        )
    if raw.startswith("urn:li:organization:"):
        tail = raw.rsplit(":", 1)[1]
    else:
        tail = raw.rstrip("/").rsplit("/", 1)[-1]
    if not tail.isdigit():
        raise ToolError(
            f"{value!r} is not a LinkedIn organization id. It is the numeric id, not the "
            "page name - 'urn:li:organization:1234567' or just '1234567'. "
            "linkedin_list_organizations lists the ids this token can read."
        )
    return ORG_URN.format(tail)


def _headers(token: str, version: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        # Mandatory on every versioned call; LinkedIn does not default it.
        "Linkedin-Version": version,
        "X-Restli-Protocol-Version": "2.0.0",
    }


def _fail(response: httpx.Response) -> ToolError:
    detail = ""
    try:
        body = response.json()
        detail = str(body.get("message") or body.get("error_description") or "")
    except ValueError:
        detail = response.text[:200]
    status = response.status_code

    if status == 401:
        return ToolError(
            "LinkedIn rejected the access token. These expire after about 60 days and "
            "cannot be renewed unattended, so this is usually expiry rather than a bad "
            "value. Generate a new one for a member who administers the Page and set "
            f"LINKEDIN_ACCESS_TOKEN. LinkedIn said: {detail or 'unauthorized'}"
        )
    if status == 403:
        return ToolError(
            "LinkedIn refused this request. Statistics need two things that are easy to "
            "miss: the signing member must have an ADMINISTRATOR role on the Page, and "
            "the app must have the Community Management API product approved with the "
            "r_organization_social and rw_organization_admin scopes granted. A token "
            f"missing either returns exactly this. LinkedIn said: {detail or 'forbidden'}"
        )
    if status == 404:
        return ToolError(
            f"LinkedIn has no such organization or share: {detail or 'not found'}. "
            "linkedin_list_organizations lists what this token can actually read."
        )
    if status == 426:
        return ToolError(
            "LinkedIn rejected the API version. Versions are retired on a rolling basis; "
            "set LINKEDIN_API_VERSION to a supported YYYYMM value - the current one is "
            "named in LinkedIn's API changelog."
        )
    if status == 429:
        return ToolError(
            "LinkedIn rate-limited this request. Its limits are per application and per "
            "member per day, so a burst of queries can exhaust the day's allowance for "
            "everyone sharing the token. Wait, then ask for a narrower range."
        )
    if status >= 500:
        return ToolError(f"LinkedIn returned a {status} server error. This is transient - retry.")
    return ToolError(f"LinkedIn rejected the request ({status}): {detail or 'no detail given'}")


async def _get(
    token: str, version: str, path: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    async with _GATE:
        async with httpx.AsyncClient(timeout=25) as client:
            try:
                r = await client.get(
                    f"{BASE}{path}", params=params, headers=_headers(token, version)
                )
            except httpx.HTTPError as exc:
                raise ToolError(f"Could not reach LinkedIn at {BASE}{path}: {exc}") from exc
    if r.status_code >= 400:
        raise _fail(r)
    try:
        return r.json()
    except ValueError as exc:
        raise ToolError(f"LinkedIn returned a non-JSON response: {r.text[:160]}") from exc


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# --- organizations ------------------------------------------------------------


async def list_organizations(token: str, version: str = DEFAULT_VERSION) -> list[Organization]:
    """The Pages this token administers.

    Probes what the credential *exposes* rather than merely whether it answers. A token
    that authenticates but administers nothing returns an empty list, and reporting that
    as success is how an earlier version of auth_status misled on Search Console.
    """
    acls = await _get(
        token,
        version,
        "/organizationAcls",
        {"q": "roleAssignee", "role": "ADMINISTRATOR", "state": "APPROVED"},
    )
    urns = [str(e.get("organization") or "") for e in acls.get("elements", [])]
    urns = [u for u in urns if u.startswith("urn:li:organization:")]
    if not urns:
        return []

    out: list[Organization] = []
    for urn in urns:
        ident = urn.rsplit(":", 1)[1]
        try:
            org = await _get(token, version, f"/organizations/{ident}")
        except ToolError:
            # An ACL can outlive readability of the Page itself; a name we cannot
            # fetch should not hide the ones we can.
            org = {}
        out.append(
            Organization(
                organization_id=ident,
                name=str(org.get("localizedName") or org.get("name") or f"organization {ident}"),
                vanity_name=org.get("vanityName"),
                followers=await _follower_count(token, version, urn),
            )
        )
    return out


async def _follower_count(token: str, version: str, urn: str) -> int | None:
    """Total followers, which the statistics endpoints do not report."""
    try:
        data = await _get(
            token,
            version,
            f"/networkSizes/{urn}",
            {"edgeType": "COMPANY_FOLLOWED_BY_MEMBER"},
        )
    except ToolError:
        return None
    return _int(data.get("firstDegreeSize")) or None


# --- statistics ---------------------------------------------------------------


def _share_row(stats: dict[str, Any]) -> dict[str, Any]:
    """Counts only. LinkedIn's own engagement figure is dropped deliberately.

    It arrives already divided, so it cannot be summed across rows or weighted
    correctly afterwards; shaping recomputes it from the components instead.
    """
    impressions = _int(stats.get("impressionCount"))
    clicks = _int(stats.get("clickCount"))
    likes = _int(stats.get("likeCount"))
    comments = _int(stats.get("commentCount"))
    shares = _int(stats.get("shareCount"))
    engagements = clicks + likes + comments + shares
    return {
        "impressions": impressions,
        "clicks": clicks,
        "likes": likes,
        "comments": comments,
        "shares": shares,
        # The numerator is carried explicitly so the rate can be rebuilt after rows are
        # combined. Without it, totals would have to average rates, which is wrong.
        "engagements": engagements,
        "engagement_rate": round(engagements / impressions, 4) if impressions else None,
    }


async def share_stats(
    token: str,
    organization: str,
    *,
    version: str = DEFAULT_VERSION,
    start: str | None = None,
    end: str | None = None,
) -> QueryResult:
    """How the Page's posts performed, in total over the window."""
    urn = organization_urn(organization)
    params: dict[str, Any] = {"q": "organizationalEntity", "organizationalEntity": urn}
    notes = [
        "Counts cover posts published by the Page itself. Employee posts that mention "
        "the company are not included.",
        "'engagement_rate' is recomputed from the counts rather than taken from "
        "LinkedIn's own field, so it stays correct when rows are combined.",
    ]
    if start and end:
        params["timeIntervals.timeRange.start"] = _epoch_ms(start)
        params["timeIntervals.timeRange.end"] = _epoch_ms(end)
        params["timeIntervals.timeGranularityType"] = "DAY"
        notes.append(
            "LinkedIn keeps about 12 months of share statistics and the window must end "
            "at least 2 days ago; a range outside that returns nothing."
        )
    else:
        notes.append("No date range was given, so these are lifetime totals for the Page.")

    payload = await _get(token, version, "/organizationalEntityShareStatistics", params)
    rows = []
    for element in payload.get("elements", []):
        row = _share_row(element.get("totalShareStatistics") or {})
        if (window := element.get("timeRange")) and window.get("start"):
            row = {"date": _iso_day(window["start"]), **row}
        rows.append(row)

    return QueryResult(
        rows=rows,
        dimension_keys=("date",) if rows and "date" in rows[0] else (),
        metric_keys=SHARE_METRICS,
        source="linkedin",
        total_rows=len(rows),
        meta={"account": "linkedin", "organization": urn},
        notes=notes,
    )


async def follower_stats(
    token: str,
    organization: str,
    *,
    version: str = DEFAULT_VERSION,
    start: str | None = None,
    end: str | None = None,
) -> QueryResult:
    """Followers gained, split by whether they were paid for."""
    urn = organization_urn(organization)
    params: dict[str, Any] = {"q": "organizationalEntity", "organizationalEntity": urn}
    notes = [
        "These are followers GAINED in the window, not the running total. "
        "linkedin_list_organizations reports the current total.",
    ]
    if start and end:
        params["timeIntervals.timeRange.start"] = _epoch_ms(start)
        params["timeIntervals.timeRange.end"] = _epoch_ms(end)
        params["timeIntervals.timeGranularityType"] = "DAY"
        notes.append(
            "LinkedIn keeps about 12 months of follower statistics and the window must "
            "end at least 2 days ago; a range outside that returns nothing."
        )
    else:
        notes.append("No date range was given, so these are lifetime totals for the Page.")

    payload = await _get(token, version, "/organizationalEntityFollowerStatistics", params)
    rows = []
    for element in payload.get("elements", []):
        counts = element.get("followerGains") or {}
        organic = _int(counts.get("organicFollowerGain"))
        paid = _int(counts.get("paidFollowerGain"))
        row: dict[str, Any] = {
            "organic_gain": organic,
            "paid_gain": paid,
            "total_gain": organic + paid,
        }
        if (window := element.get("timeRange")) and window.get("start"):
            row = {"date": _iso_day(window["start"]), **row}
        rows.append(row)

    return QueryResult(
        rows=rows,
        dimension_keys=("date",) if rows and "date" in rows[0] else (),
        metric_keys=FOLLOWER_METRICS,
        source="linkedin",
        total_rows=len(rows),
        meta={"account": "linkedin", "organization": urn},
        notes=notes,
    )


async def page_stats(
    token: str,
    organization: str,
    *,
    version: str = DEFAULT_VERSION,
    start: str | None = None,
    end: str | None = None,
) -> QueryResult:
    """Views of the Page itself, broken down by which tab was viewed."""
    urn = organization_urn(organization)
    params: dict[str, Any] = {"q": "organization", "organization": urn}
    notes = [
        "These are views of the LinkedIn Page, not of the website. Website traffic from "
        "LinkedIn shows up in Analytics instead.",
    ]
    if start and end:
        params["timeIntervals.timeRange.start"] = _epoch_ms(start)
        params["timeIntervals.timeRange.end"] = _epoch_ms(end)
        params["timeIntervals.timeGranularityType"] = "DAY"
    else:
        notes.append("No date range was given, so these are lifetime totals for the Page.")

    payload = await _get(token, version, "/organizationPageStatistics", params)
    rows = []
    for element in payload.get("elements", []):
        views = element.get("totalPageStatistics", {}).get("views") or {}
        for section, counts in sorted(views.items()):
            if not isinstance(counts, dict):
                continue
            rows.append(
                {
                    "section": _readable(section),
                    "views": _int(counts.get("pageViews")),
                    "unique_views": _int(counts.get("uniquePageViews")),
                }
            )
    return QueryResult(
        rows=rows,
        dimension_keys=("section",),
        metric_keys=("views", "unique_views"),
        source="linkedin",
        total_rows=len(rows),
        meta={"account": "linkedin", "organization": urn},
        notes=notes,
    )


# --- small helpers ------------------------------------------------------------


def _readable(key: str) -> str:
    """'allDesktopPageViews' -> 'all desktop'. LinkedIn's keys are not for reading."""
    out = []
    for ch in key:
        out.append(" " + ch.lower() if ch.isupper() else ch)
    return "".join(out).replace(" page views", "").replace("page views", "").strip() or key


def _epoch_ms(day: str) -> int:
    """LinkedIn takes milliseconds since the epoch, in UTC."""
    return int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp() * 1000)


def _iso_day(ms: Any) -> str:
    return datetime.fromtimestamp(_int(ms) / 1000, tz=UTC).date().isoformat()
