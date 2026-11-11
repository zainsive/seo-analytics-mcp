"""The YouTube credential, which is separate from every other one here on purpose.

`credentials.mode()` is a single switch: when a service account is present, everything
uses it. That cannot work for YouTube. Google's own documentation states the Analytics
API does not support the service account flow, because a service account cannot be
linked to a YouTube channel - requests authorised that way simply error. So YouTube
carries its own refresh token, read from the environment and exchanged for an access
token on demand, independently of whatever the rest of the server is authenticated as.

The refresh itself is delegated to google-auth rather than written here: it owns the
token endpoint, the clock skew margin and the error shapes, and this module has no
business issuing its own POST to an OAuth endpoint.
"""

from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import config
from .auth import scopes

TOKEN_URI = "https://oauth2.googleapis.com/token"

SETUP = (
    "YouTube Analytics needs its own Google sign-in from the channel owner - a service "
    "account cannot read a YouTube channel, which is Google's restriction and not one "
    "this server imposes. Run `seo-analytics-mcp youtube-auth` on a machine with a "
    "browser, then copy the three values it prints into the server's environment as "
    "YOUTUBE_REFRESH_TOKEN, YOUTUBE_OAUTH_CLIENT_ID and YOUTUBE_OAUTH_CLIENT_SECRET."
)


def configured() -> bool:
    profile = config.active_profile()
    return bool(
        profile.youtube_refresh_token
        and profile.youtube_client_id
        and profile.youtube_client_secret
    )


def _missing() -> list[str]:
    profile = config.active_profile()
    pairs = (
        ("YOUTUBE_REFRESH_TOKEN", profile.youtube_refresh_token),
        ("YOUTUBE_OAUTH_CLIENT_ID", profile.youtube_client_id),
        ("YOUTUBE_OAUTH_CLIENT_SECRET", profile.youtube_client_secret),
    )
    return [name for name, value in pairs if not value]


async def credentials() -> Any:
    """A live, refreshed credential for the YouTube APIs.

    Raises a ToolError naming the missing piece rather than letting google-auth raise
    something that reads as an internal fault.
    """
    if missing := _missing():
        raise ToolError(
            f"YouTube is not configured: {', '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} not set. {SETUP}"
        )

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    profile = config.active_profile()
    creds = Credentials(
        token=None,
        refresh_token=profile.youtube_refresh_token,
        client_id=profile.youtube_client_id,
        client_secret=profile.youtube_client_secret,
        token_uri=TOKEN_URI,
        scopes=sorted(scopes.for_youtube()),
    )
    try:
        # Blocking HTTP inside google-auth, so it does not belong on the event loop.
        await asyncio.to_thread(creds.refresh, Request())
    except Exception as exc:
        raise ToolError(_explain(exc)) from exc
    return creds


def _explain(exc: Exception) -> str:
    """Turn google-auth's refresh failures into something actionable.

    'invalid_grant' is the one that matters: it is what a revoked consent and an
    expired-because-unpublished app both look like, and the distinction decides
    whether re-authorising will hold or fail again a week later.
    """
    text = str(exc)
    if "invalid_grant" in text:
        return (
            "Google rejected the YouTube refresh token (invalid_grant). The usual causes, "
            "in order of likelihood: the OAuth app is still in Testing, where refresh "
            "tokens expire after 7 days - publish it so they persist; the channel owner "
            "revoked access; or the password changed. Re-authorise with "
            "`seo-analytics-mcp youtube-auth` and set the new refresh token."
        )
    if "invalid_client" in text:
        return (
            "Google rejected the YouTube OAuth client (invalid_client). "
            "YOUTUBE_OAUTH_CLIENT_ID and YOUTUBE_OAUTH_CLIENT_SECRET must come from the "
            "same OAuth client that issued the refresh token."
        )
    return f"Could not refresh the YouTube credential: {text[:200]}"
