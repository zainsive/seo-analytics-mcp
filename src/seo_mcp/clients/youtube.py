"""YouTube, which is two APIs with two different credentials.

Data API v3 answers public questions - subscriber count, view count, a video's likes -
and takes a plain API key. Analytics API v2 answers the questions worth asking - watch
time, retention, where the views came from, how often a thumbnail was clicked - and
takes OAuth from the channel owner. Google does not allow a service account anywhere
near the second one.

Both halves are read-only. Nothing here uploads, edits, rates or deletes.

Rates are recomputed from counts rather than taken from the API, for the fifth time in
this codebase: `averageViewPercentage` and click-through rate are both already divided
when they arrive, so neither survives being combined across rows.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from mcp.server.mcpserver.exceptions import ToolError

from ..shaping import QueryResult

DATA_API = "youtube"
DATA_VERSION = "v3"
ANALYTICS_API = "youtubeAnalytics"
ANALYTICS_VERSION = "v2"

# The Analytics API answers for one channel at a time, addressed this way.
CHANNEL = "channel=={}"

VIDEO_METRICS = ("views", "likes", "comments")
# Ratios last: shaping rebuilds them from the components beside them.
REPORT_METRICS = (
    "views",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "subscribersGained",
    "subscribersLost",
)


@dataclass(frozen=True)
class Channel:
    """A channel the key can see. Holds no credential."""

    channel_id: str
    title: str
    subscribers: int | None
    views: int | None
    videos: int | None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def channel_id(value: str) -> str:
    """Accept an id, a /channel/ URL, or an @handle; reject anything else clearly."""
    raw = (value or "").strip().rstrip("/")
    if not raw:
        raise ToolError(
            "No YouTube channel was given and YOUTUBE_CHANNEL_ID is not set. "
            "Run youtube_channel_stats once with the id, or set the default."
        )
    if "/channel/" in raw:
        raw = raw.split("/channel/", 1)[1].split("/")[0]
    if raw.startswith("UC") and len(raw) == 24:
        return raw
    if raw.startswith("@"):
        raise ToolError(
            f"{raw!r} is a channel handle, not an id. The Analytics API needs the id, "
            "which starts 'UC' and is 24 characters. It is in the channel URL under "
            "/channel/, or in YouTube Studio under Settings > Channel > Advanced."
        )
    raise ToolError(
        f"{value!r} is not a YouTube channel id. An id starts with 'UC' and is 24 "
        "characters long, for example 'UCVsEgKMBL1DREac01aQf5zA'."
    )


def _fail(exc: HttpError, what: str) -> ToolError:
    status = getattr(exc.resp, "status", None)
    detail = ""
    try:
        detail = str(exc.error_details or exc.reason or "")
    except Exception:  # pragma: no cover - error_details is not always populated
        detail = str(exc)[:200]

    if status == 400 and "quotaExceeded" not in detail:
        return ToolError(f"YouTube rejected the {what} request: {detail or 'bad request'}.")
    if status in (401, 403):
        if "quota" in detail.lower():
            return ToolError(
                "The YouTube API quota is exhausted for today. The Data API allows "
                "10,000 units a day by default and a video search costs 100 of them; "
                "it resets at midnight Pacific time."
            )
        return ToolError(
            f"YouTube refused the {what} request: {detail or 'forbidden'}. For Analytics "
            "this means the signed-in account does not own or manage the channel - "
            "Google allows no service account here, so it must be the owner's own "
            "sign-in. For public statistics it usually means the API key is restricted "
            "to other APIs or referrers."
        )
    if status == 404:
        return ToolError(f"YouTube has no such {what}: {detail or 'not found'}.")
    if status and status >= 500:
        return ToolError(f"YouTube returned a {status} server error. This is transient - retry.")
    return ToolError(f"YouTube rejected the {what} request: {detail or exc}")


# --- public statistics, by API key --------------------------------------------


async def channel_stats(api_key: str, channel: str) -> Channel:
    ident = channel_id(channel)

    def work():
        service = build(DATA_API, DATA_VERSION, developerKey=api_key, cache_discovery=False)
        return service.channels().list(part="snippet,statistics", id=ident).execute()

    try:
        payload = await asyncio.to_thread(work)
    except HttpError as exc:
        raise _fail(exc, "channel") from exc

    items = payload.get("items") or []
    if not items:
        raise ToolError(
            f"YouTube returned no channel for id {ident!r}. Check it is the channel id "
            "rather than a video or playlist id - it starts 'UC' and is 24 characters."
        )
    item = items[0]
    stats = item.get("statistics") or {}
    return Channel(
        channel_id=ident,
        title=str((item.get("snippet") or {}).get("title") or ident),
        subscribers=_int(stats.get("subscriberCount")),
        views=_int(stats.get("viewCount")),
        videos=_int(stats.get("videoCount")),
    )


async def recent_videos(api_key: str, channel: str, *, limit: int = 25) -> QueryResult:
    """The channel's most recent uploads with their public counts."""
    ident = channel_id(channel)
    limit = max(1, min(limit, 50))

    def work():
        service = build(DATA_API, DATA_VERSION, developerKey=api_key, cache_discovery=False)
        # The uploads playlist, rather than search.list: it is exact, ordered, and
        # costs 1 quota unit instead of 100.
        channels = service.channels().list(part="contentDetails", id=ident).execute()
        items = channels.get("items") or []
        if not items:
            return [], []
        uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        playlist = (
            service.playlistItems()
            .list(part="contentDetails", playlistId=uploads, maxResults=limit)
            .execute()
        )
        ids = [
            i["contentDetails"]["videoId"]
            for i in playlist.get("items", [])
            if i.get("contentDetails", {}).get("videoId")
        ]
        if not ids:
            return [], []
        videos = service.videos().list(part="snippet,statistics", id=",".join(ids)).execute()
        return ids, videos.get("items", [])

    try:
        _, items = await asyncio.to_thread(work)
    except HttpError as exc:
        raise _fail(exc, "video list") from exc

    rows = []
    for item in items:
        snippet, stats = item.get("snippet") or {}, item.get("statistics") or {}
        rows.append(
            {
                "video_id": item.get("id"),
                "title": snippet.get("title") or "",
                "published": (snippet.get("publishedAt") or "")[:10],
                "views": _int(stats.get("viewCount")) or 0,
                "likes": _int(stats.get("likeCount")) or 0,
                "comments": _int(stats.get("commentCount")) or 0,
            }
        )
    return QueryResult(
        rows=rows,
        dimension_keys=("video_id", "title", "published"),
        metric_keys=VIDEO_METRICS,
        source="youtube",
        total_rows=len(rows),
        meta={"account": "youtube", "channel": ident},
        notes=[
            "These are public counts, the same numbers any viewer sees. Watch time, "
            "retention and traffic sources come from youtube_channel_report instead.",
            "Like and comment counts are missing where the channel has hidden them.",
        ],
    )


# --- owner analytics, by OAuth ------------------------------------------------


async def _query(creds: Any, **params: Any) -> dict[str, Any]:
    def work():
        service = build(ANALYTICS_API, ANALYTICS_VERSION, credentials=creds, cache_discovery=False)
        return service.reports().query(**params).execute()

    try:
        return await asyncio.to_thread(work)
    except HttpError as exc:
        raise _fail(exc, "analytics") from exc


def _rows(payload: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    headers = [str(h.get("name")) for h in payload.get("columnHeaders", [])]
    return headers, payload.get("rows") or []


def _shape_report(
    payload: dict[str, Any], dimension: str | None, channel: str, notes: list[str]
) -> QueryResult:
    headers, raw = _rows(payload)
    rows: list[dict[str, Any]] = []
    for values in raw:
        row = dict(zip(headers, values, strict=False))
        gained = _int(row.get("subscribersGained")) or 0
        lost = _int(row.get("subscribersLost")) or 0
        shaped: dict[str, Any] = {}
        if dimension:
            shaped[dimension] = row.get(dimension)
        shaped.update(
            {
                "views": _int(row.get("views")) or 0,
                "minutes_watched": _int(row.get("estimatedMinutesWatched")) or 0,
                "average_view_duration": _int(row.get("averageViewDuration")) or 0,
                "subscribers_gained": gained,
                "subscribers_lost": lost,
                "subscribers_net": gained - lost,
            }
        )
        rows.append(shaped)
    return QueryResult(
        rows=rows,
        dimension_keys=(dimension,) if dimension else (),
        metric_keys=(
            "views",
            "minutes_watched",
            "average_view_duration",
            "subscribers_gained",
            "subscribers_lost",
            "subscribers_net",
        ),
        source="youtube",
        total_rows=len(rows),
        meta={"account": "youtube", "channel": channel},
        notes=notes,
    )


async def channel_report(
    creds: Any,
    channel: str,
    start: str,
    end: str,
    *,
    dimension: str | None = None,
) -> QueryResult:
    """Watch time and subscriber movement, optionally broken down by day or video."""
    ident = channel_id(channel)
    params: dict[str, Any] = {
        "ids": CHANNEL.format(ident),
        "startDate": start,
        "endDate": end,
        "metrics": ",".join(REPORT_METRICS),
    }
    notes = [
        "'average_view_duration' is in seconds, and 'minutes_watched' is total watch "
        "time, not an average.",
        "YouTube Analytics lags about 2 days; the most recent days may read low.",
    ]
    if dimension:
        params["dimensions"] = dimension
        params["sort"] = "-views" if dimension == "video" else dimension
        if dimension == "video":
            params["maxResults"] = 50
            notes.append(
                "Rows are video ids. Titles come from youtube_recent_videos, which "
                "shares the same id."
            )
    return _shape_report(await _query(creds, **params), dimension, ident, notes)


async def traffic_sources(creds: Any, channel: str, start: str, end: str) -> QueryResult:
    """Where the views came from: search, suggested, external, and so on."""
    ident = channel_id(channel)
    payload = await _query(
        creds,
        ids=CHANNEL.format(ident),
        startDate=start,
        endDate=end,
        metrics="views,estimatedMinutesWatched",
        dimensions="insightTrafficSourceType",
        sort="-views",
    )
    headers, raw = _rows(payload)
    rows = []
    for values in raw:
        row = dict(zip(headers, values, strict=False))
        rows.append(
            {
                "source": _readable(str(row.get("insightTrafficSourceType") or "")),
                "views": _int(row.get("views")) or 0,
                "minutes_watched": _int(row.get("estimatedMinutesWatched")) or 0,
            }
        )
    return QueryResult(
        rows=rows,
        dimension_keys=("source",),
        metric_keys=("views", "minutes_watched"),
        source="youtube",
        total_rows=len(rows),
        meta={"account": "youtube", "channel": ident},
        notes=[
            "'external' means a link from another site. Which site is not broken out "
            "here; the website side of that traffic is in Analytics.",
            "YouTube Analytics lags about 2 days; the most recent days may read low.",
        ],
    )


_READABLE = {
    "ADVERTISING": "advertising",
    "ANNOTATION": "annotation",
    "CAMPAIGN_CARD": "campaign card",
    "END_SCREEN": "end screen",
    "EXT_URL": "external link",
    "NO_LINK_EMBEDDED": "embedded elsewhere",
    "NO_LINK_OTHER": "other",
    "NOTIFICATION": "notification",
    "PLAYLIST": "playlist",
    "PROMOTED": "promoted",
    "RELATED_VIDEO": "suggested video",
    "SHORTS": "shorts feed",
    "SOUND_PAGE": "sound page",
    "SUBSCRIBER": "subscriptions feed",
    "YT_CHANNEL": "channel page",
    "YT_OTHER_PAGE": "other YouTube page",
    "YT_SEARCH": "YouTube search",
    "HASHTAGS": "hashtags",
}


def _readable(code: str) -> str:
    return _READABLE.get(code, code.replace("_", " ").lower() or "unknown")
