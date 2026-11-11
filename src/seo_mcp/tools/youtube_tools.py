"""YouTube tools. Statistics only: nothing here uploads, edits, rates or deletes.

Two credentials, because Google requires two. The public counts need only an API key;
watch time, retention and traffic sources need the channel owner's own OAuth, which a
service account cannot stand in for. Each tool names the one it needs when it is absent,
so a half-configured install says which half.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import cache, config, youtube_auth
from ..clients import youtube
from ..server import mcp
from ..shaping import DateRangeError, ShapedResponse, resolve_range, shape
from . import _base

TTL = 3 * 3600  # YouTube Analytics is recomputed slowly; an hour stale costs nothing

DIMENSIONS = ("day", "video", "country")


def _api_key() -> str:
    key = config.active_profile().youtube_api_key
    if not key:
        raise ToolError(
            "No YouTube API key is configured. Create one in the Google Cloud console "
            "under APIs & Services > Credentials, enable the YouTube Data API v3 for "
            "that project, then set YOUTUBE_API_KEY in the server's environment. The "
            "key is never passed as a tool argument, because tool arguments are visible "
            "in the transcript."
        )
    return key


def _channel(value: str | None) -> str:
    chosen = value or config.active_profile().youtube_channel
    if not chosen:
        raise ToolError(
            "No YouTube channel was given and YOUTUBE_CHANNEL_ID is not set. The id "
            "starts 'UC' and is 24 characters; it is in YouTube Studio under "
            "Settings > Channel > Advanced."
        )
    return youtube.channel_id(chosen)  # reject a bad id here, not as "no access"


def _window(start_date: str, end_date: str) -> tuple[str, str, list[str]]:
    try:
        return resolve_range(start_date, end_date, source="youtube")
    except DateRangeError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=_base.READ_ONLY)
async def youtube_channel_stats(channel_id: str | None = None) -> dict[str, Any]:
    """Headline numbers for a channel: subscribers, total views, how many videos.

    These are the public counts, so this works with only an API key. Watch time and
    traffic sources need youtube_channel_report, which needs the owner's sign-in.
    """
    profile = config.active_profile()
    ident = _channel(channel_id)
    key = _api_key()
    ck = cache.key(profile, "youtube.channel", {"channel": ident})

    async def producer():
        from ..shaping import QueryResult

        channel = await youtube.channel_stats(key, ident)
        return QueryResult(
            rows=[
                {
                    "channel_id": channel.channel_id,
                    "title": channel.title,
                    "subscribers": channel.subscribers,
                    "views": channel.views,
                    "videos": channel.videos,
                }
            ],
            dimension_keys=("channel_id", "title"),
            metric_keys=("subscribers", "views", "videos"),
            source="youtube",
            total_rows=1,
            meta={"account": "youtube"},
        )

    result = await cache.get_or_set(profile, ck, ttl=TTL, producer=producer)
    return {
        "channel": _base.as_rows(result.rows[0]) if result.rows else {},
        "notes": [
            "'views' is the channel's lifetime total, not a figure for any period.",
            "Subscriber counts are rounded by YouTube above 1,000, so small changes "
            "will not show here.",
        ],
    }


@mcp.tool(annotations=_base.READ_ONLY)
async def youtube_recent_videos(channel_id: str | None = None, limit: int = 25) -> ShapedResponse:
    """The channel's most recent uploads with their public view, like and comment counts.

    Use the video_id from these rows to read the same videos in youtube_channel_report.
    """
    profile = config.active_profile()
    ident = _channel(channel_id)
    key = _api_key()
    ck = cache.key(profile, "youtube.videos", {"channel": ident, "limit": limit})
    result = await cache.get_or_set(
        profile,
        ck,
        ttl=TTL,
        producer=lambda: youtube.recent_videos(key, ident, limit=limit),
    )
    return shape(result, top_n=limit, sort_by=None, export_dir=_base.export_dir(profile))


@mcp.tool(annotations=_base.READ_ONLY)
async def youtube_channel_report(
    start_date: str = "28daysAgo",
    end_date: str = "today",
    dimension: str | None = None,
    channel_id: str | None = None,
) -> ShapedResponse:
    """Watch time, average view duration and subscriber movement over a period.

    `dimension` may be 'day', 'video' or 'country'; omit it for a single total. This is
    owner data, so it needs the channel owner's Google sign-in - a service account
    cannot read it, which is Google's rule rather than this server's.
    """
    if dimension is not None and dimension not in DIMENSIONS:
        raise ToolError(
            f"{dimension!r} is not a dimension this tool supports. Use one of: "
            f"{', '.join(DIMENSIONS)} - or omit it for the period total."
        )
    profile = config.active_profile()
    ident = _channel(channel_id)
    start, end, notes = _window(start_date, end_date)
    creds = await youtube_auth.credentials()
    ck = cache.key(
        profile,
        "youtube.report",
        {"channel": ident, "start": start, "end": end, "dimension": dimension},
    )
    result = await cache.get_or_set(
        profile,
        ck,
        ttl=TTL,
        producer=lambda: youtube.channel_report(creds, ident, start, end, dimension=dimension),
    )
    out = shape(
        result,
        top_n=200,
        sort_by="views" if dimension else None,
        export_dir=_base.export_dir(profile),
    )
    out["summary"]["date_range"] = f"{start}..{end}"
    out["notes"] = notes + out["notes"]
    return out


@mcp.tool(annotations=_base.READ_ONLY)
async def youtube_traffic_sources(
    start_date: str = "28daysAgo",
    end_date: str = "today",
    channel_id: str | None = None,
) -> ShapedResponse:
    """Where the channel's views came from: YouTube search, suggested, external links.

    The 'external link' row is the one that joins to the rest of this server - those
    are views that arrived from a website, which Analytics sees from the other side.
    """
    profile = config.active_profile()
    ident = _channel(channel_id)
    start, end, notes = _window(start_date, end_date)
    creds = await youtube_auth.credentials()
    ck = cache.key(profile, "youtube.traffic", {"channel": ident, "start": start, "end": end})
    result = await cache.get_or_set(
        profile,
        ck,
        ttl=TTL,
        producer=lambda: youtube.traffic_sources(creds, ident, start, end),
    )
    out = shape(result, top_n=50, sort_by="views", export_dir=_base.export_dir(profile))
    out["summary"]["date_range"] = f"{start}..{end}"
    out["notes"] = notes + out["notes"]
    return out
