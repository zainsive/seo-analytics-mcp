"""Where the server's Google credentials come from.

Two modes, chosen by environment:

  service_account  a key supplied via the environment. Built for servers: no browser,
                   no refresh-token expiry, no coupling to a person's account. This is
                   what the hosted deployment uses.
  user             the OAuth token written by `seo-analytics-mcp auth`. What a local
                   stdio install uses.

Tools call for_scopes() and never learn which mode is active, which is what lets the
same thirteen handlers serve both deployments.
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

from . import config
from .auth import store

logger = logging.getLogger(__name__)

# Inline JSON, or the same JSON base64-encoded. Base64 exists because a service-account
# private_key contains literal \n sequences that most .env parsers mangle.
SERVICE_ACCOUNT_JSON = "SEO_MCP_SERVICE_ACCOUNT_JSON"
SERVICE_ACCOUNT_FILE = "SEO_MCP_SERVICE_ACCOUNT_FILE"

Mode = Literal["service_account", "user"]


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
    info = _service_account_info()
    if info is None:
        return store.load(config.active_profile(), required)
    return service_account.Credentials.from_service_account_info(info, scopes=sorted(required))


def describe() -> dict[str, Any]:
    """What auth_status and doctor report. Never includes key material."""
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
        "authorised": True,
        "note": (
            "Access comes from a service account, so it reads exactly the properties "
            f"that {info.get('client_email')} has been granted in Search Console "
            "(Settings > Users and permissions) and GA4 (Admin > Property access "
            "management). Granting is a separate step from deploying."
        ),
    }
