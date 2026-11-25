"""Where the server's Google credentials come from.

Resolution order, and the order is the whole design:

  1. the caller's own grant   when a signed-in person has connected their Google
                              account, every query runs as them. They see the
                              properties they are granted and nothing else.
  2. service account          a key from the environment. Used for callers who are
                              not people - a hand-issued static token, a cron job.
  3. local user token         what `seo-analytics-mcp auth` writes, for a stdio install.

A signed-in person never silently falls through to 2. If they have not granted the
scope a tool needs, they are told to grant it. Falling back would hand them somebody
else's view of the data and look like it worked, which is the exact failure this
arrangement exists to prevent.

Tools call for_scopes() and learn none of this, which is what lets the same handlers
serve a personal install and a shared deployment.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from pathlib import Path
from typing import Any, Literal

from google.oauth2 import service_account
from mcp.server.mcpserver.exceptions import ToolError

from . import config, identity
from .auth import store
from .sso import connections

logger = logging.getLogger(__name__)

# Inline JSON, or the same JSON base64-encoded. Base64 exists because a service-account
# private_key contains literal \n sequences that most .env parsers mangle.
SERVICE_ACCOUNT_JSON = "SEO_MCP_SERVICE_ACCOUNT_JSON"
SERVICE_ACCOUNT_FILE = "SEO_MCP_SERVICE_ACCOUNT_FILE"

Mode = Literal["service_account", "user"]

GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Plain names for the consent a person is missing. "webmasters.readonly" means nothing
# to somebody being told to sign in again.
scope_names = {
    "https://www.googleapis.com/auth/webmasters.readonly": "Search Console",
    "https://www.googleapis.com/auth/analytics.readonly": "Analytics",
    "https://www.googleapis.com/auth/yt-analytics.readonly": "YouTube Analytics",
    "https://www.googleapis.com/auth/youtube.readonly": "YouTube",
}


def _decode(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if not text.startswith("{"):
        try:
            text = base64.b64decode(text, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise ToolError(
                f"{SERVICE_ACCOUNT_JSON} is neither JSON nor valid base64 ({exc}). Set it "
                "to the contents of the service-account key file, or to that file "
                "base64-encoded."
            ) from exc
    try:
        info = json.loads(text)
    except ValueError as exc:
        raise ToolError(f"{SERVICE_ACCOUNT_JSON} could not be parsed as JSON: {exc}") from exc
    if info.get("type") != "service_account":
        raise ToolError(
            f"{SERVICE_ACCOUNT_JSON} does not hold a service-account key "
            f"(its type is {info.get('type')!r}). Download the key from Google Cloud "
            "Console > IAM & Admin > Service Accounts > Keys."
        )
    for field in ("client_email", "private_key", "token_uri"):
        if not info.get(field):
            raise ToolError(
                f"{SERVICE_ACCOUNT_JSON} is missing {field!r}. It must be the complete "
                "key file, unmodified."
            )
    return info


def _service_account_info() -> dict[str, Any] | None:
    if raw := os.environ.get(SERVICE_ACCOUNT_JSON):
        return _decode(raw)
    if path := os.environ.get(SERVICE_ACCOUNT_FILE):
        resolved = Path(path).expanduser()
        if not resolved.is_file():
            raise ToolError(f"{SERVICE_ACCOUNT_FILE} points at {resolved}, which does not exist.")
        return _decode(resolved.read_text(encoding="utf-8"))
    return None


def mode() -> Mode:
    return "service_account" if _service_account_info() is not None else "user"


def service_account_email() -> str | None:
    info = _service_account_info()
    return info.get("client_email") if info else None


def for_scopes(required: set[str]):
    """The credential every tool uses. Scopes are requested per call so a read tool
    never holds more authority than it needs."""
    if subject := identity.caller():
        return as_person(subject, required)
    info = _service_account_info()
    if info is None:
        return store.load(config.active_profile(), required)
    return service_account.Credentials.from_service_account_info(info, scopes=sorted(required))


def names_for(required) -> str:
    """Plain product names, listed the way a person would say them.

    "YouTube, YouTube Analytics" is what joining with commas produces, and it reads
    like a truncated list rather than a sentence; these strings only ever appear in
    front of somebody whose query just failed.
    """
    names = sorted({scope_names.get(s, s) for s in required})
    if len(names) <= 1:
        return names[0] if names else "anything"
    return f"{', '.join(names[:-1])} and {names[-1]}"


def as_person(subject: str, required: set[str]):
    """Build a live credential for one named person, or explain what is missing.

    Public, and takes the subject rather than reading it from the ambient request,
    because YouTube resolves its credential from an async path where passing the name
    explicitly is clearer than relying on the context travelling into a worker thread.
    """
    link = connections.recall(subject)
    if link is None:
        # Name what was actually asked for. A YouTube question answered with "nothing
        # to read Search Console with" reads as the server having lost track of the
        # question, which is a worse impression than the real problem.
        wanted = names_for(required)
        raise ToolError(
            f"{subject} has not connected a Google account to this server yet, so there "
            f"is nothing to read {wanted} with. Sign out of the connector and sign in "
            "again to grant access."
        )
    if not link.covers(required):
        wanted = names_for(required - set(link.scopes))
        raise ToolError(
            f"{subject} signed in before this server asked for {wanted}, so that part "
            "is not granted yet. Sign out of the connector and sign in again - Google "
            "will show the extra permission once, and nothing else changes."
        )

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    profile = config.active_profile()
    creds = Credentials(
        token=None,
        refresh_token=link.refresh_token,
        client_id=profile.google_sso_client_id,
        client_secret=profile.google_sso_client_secret,
        token_uri=GOOGLE_TOKEN_URI,
        scopes=sorted(required),
    )
    try:
        creds.refresh(Request())
    except Exception as exc:
        raise ToolError(_explain_refresh(subject, exc)) from exc
    connections.touch(subject)
    return creds


def _explain_refresh(subject: str, exc: Exception) -> str:
    text = str(exc)
    if "invalid_grant" in text:
        return (
            f"Google will no longer act for {subject} (invalid_grant). Usually the "
            "access was withdrawn at myaccount.google.com/permissions, or the password "
            "changed. Signing in to the connector again fixes it."
        )
    return f"Could not use {subject}'s Google access: {text[:200]}"


def describe() -> dict[str, Any]:
    """What auth_status and doctor report. Never includes key material.

    Two questions are asked here, and they stopped having the same answer when
    queries began running as the person asking: how this server is configured, and
    what the caller can actually read. Reporting only the first would tell a
    signed-in person "authorised: true" while every one of their queries refuses,
    which is backwards for the one tool whose job is explaining what went wrong.

    `grantee` is the answer to "who needs to be added to the property", which is a
    different identity depending on who is calling. Getting it wrong sends somebody
    to grant access to an account that is not the one being used.
    """
    subject = identity.caller()
    if subject is None:
        return _server_credential()
    return {
        "mode": "per_person",
        "authorised": connections.recall(subject) is not None,
        "grantee": subject,
        "note": (
            f"Queries run as {subject}, so what can be read is whatever that Google "
            "account is granted - not what this server, or anybody else using it, can "
            "see. The server's own credential is reported separately and is used only "
            "for callers that are not people, such as a hand-issued token."
        ),
        "server_credential": _server_credential(),
    }


def _server_credential() -> dict[str, Any]:
    """The credential the server itself holds, for callers who are not people."""
    info = _service_account_info()
    if info is None:
        profile = config.active_profile()
        return {
            "mode": "user",
            "profile": profile.name,
            "authorised": store.has_token(profile),
            "scopes_held": store.scopes_held(profile),
        }
    return {
        "mode": "service_account",
        "service_account_email": info.get("client_email"),
        "project_id": info.get("project_id"),
        "grantee": info.get("client_email"),
        "authorised": True,
        "note": (
            "Access comes from a service account, so it reads exactly the properties "
            f"that {info.get('client_email')} has been granted in Search Console "
            "(Settings > Users and permissions) and GA4 (Admin > Property access "
            "management). Granting is a separate step from deploying."
        ),
    }
