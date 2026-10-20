"""Upstream failure -> a message that names the fix.

In mcp 2.1.0 a tool that raises ToolError produces is_error=True with this full
text as content for the model to read. A tool that raises anything else produces
the bare string "Error executing tool <name>" and every message below is thrown
away - which is why tools/_base.py exists.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from mcp.server.mcpserver.exceptions import ToolError

REAUTH, DOCTOR = "seo-analytics-mcp auth --reauth", "seo-analytics-mcp doctor"

CONSOLE_APIS = "https://console.cloud.google.com/apis/library"


class SeoMcpError(Exception):
    """Base for the project's own, already-actionable failures."""


class AuthMissing(SeoMcpError):
    def __init__(self, profile: str) -> None:
        self.profile = profile
        super().__init__(profile)


class ScopeMissing(SeoMcpError):
    def __init__(self, missing: list[str], profile: str = "default") -> None:
        self.missing = missing
        self.profile = profile
        super().__init__(", ".join(missing))


class TokenCorrupt(SeoMcpError):
    def __init__(self, path: str, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(detail)


class ClientSecretMissing(SeoMcpError):
    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(path)


class WrongClientType(SeoMcpError):
    def __init__(self, found: list[str]) -> None:
        self.found = found
        super().__init__(", ".join(found))


@dataclass(frozen=True)
class Ctx:
    """What the caller knows that the exception does not."""

    profile: str = "default"
    site_url: str | None = None
    property_id: str | None = None
    scope_needed: str | None = None
    api: str | None = None
    # Set in service-account deployments. "Re-authorise" is useless advice there -
    # the fix is granting this email access to the property.
    service_account_email: str | None = None


def _status(exc: Exception) -> int | None:
    """googleapiclient puts it on resp.status; google.api_core on .code. Check both."""
    for holder in (getattr(exc, "resp", None), exc):
        if holder is None:
            continue
        for attr in ("status", "status_code", "code"):
            v = getattr(holder, attr, None)
            if isinstance(v, int):
                return v
            v = getattr(v, "value", None)  # enum-valued api_core codes
            if isinstance(v, int):
                return v
    return None


def _reason(exc: Exception) -> str | None:
    """Google's machine-readable reason, which distinguishes 'API not enabled' from
    'no access to this property' - both of which arrive as a 403."""
    for attr in ("content", "_content"):
        raw = getattr(exc, attr, None)
        if not raw:
            continue
        try:
            payload = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except (ValueError, AttributeError, UnicodeDecodeError):
            continue
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, str):
            return error
        if isinstance(error, dict):
            errors = error.get("errors") or []
            if errors and isinstance(errors[0], dict) and errors[0].get("reason"):
                return str(errors[0]["reason"])
            if error.get("status"):
                return str(error["status"])
    text = str(exc)
    for token in ("accessNotConfigured", "SERVICE_DISABLED", "rateLimitExceeded", "invalid_grant"):
        if token in text:
            return token
    return None


def _activation_url(exc: Exception) -> str | None:
    """Google's SERVICE_DISABLED errors carry a project-specific enable link. Prefer it
    over the generic API library page - it is one click instead of a search."""
    for source in (getattr(exc, "details", None), str(exc)):
        text = str(source or "")
        marker = "https://console.developers.google.com/apis/api/"
        start = text.find(marker)
        if start == -1:
            continue
        end = len(text)
        for stop in (" ", '"', "\n", ")", ","):
            found = text.find(stop, start)
            if found != -1:
                end = min(end, found)
        return text[start:end]
    return None


def _auth_missing(exc: AuthMissing) -> ToolError:
    return ToolError(
        f"No Google authorisation is stored for profile '{exc.profile}'. This server "
        "cannot open a browser from inside a tool call, so authorise it once from a "
        "terminal: `seo-analytics-mcp auth --client-secret ./client_secret.json`. If you have "
        f"not created an OAuth client yet, run `{DOCTOR}` - it prints the exact Google "
        "Cloud Console steps in order. Restart this MCP client afterwards."
    )


def _scope_missing(exc: ScopeMissing) -> ToolError:
    needed = ", ".join(exc.missing)
    writes = any(s.endswith("/webmasters") for s in exc.missing)
    fix = f"{REAUTH} --enable-writes" if writes else REAUTH
    return ToolError(
        f"The stored token for profile '{exc.profile}' lacks the {needed} scope. "
        f"Grant it with `{fix}`. Read-only scopes are the default on purpose, so "
        "write access has to be asked for explicitly."
    )


def translate(exc: Exception, ctx: Ctx) -> ToolError:
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, AuthMissing):
        return _auth_missing(exc)
    if isinstance(exc, ScopeMissing):
        return _scope_missing(exc)
    if isinstance(exc, TokenCorrupt):
        return ToolError(
            f"The stored token at {exc.path} could not be read ({exc.detail}). Delete the "
            "profile directory and authorise again: "
            "`seo-analytics-mcp profiles rm <profile>` then "
            "`seo-analytics-mcp auth --client-secret <path>`."
        )
    if isinstance(exc, ClientSecretMissing):
        return ToolError(
            f"No OAuth client secret found at {exc.path}. Google names that download "
            "'client_secret_<id>.apps.googleusercontent.com.json' - check the path ends "
            "in '.json', since tab-completion often stops at the '.com'. If you have not "
            "created the client yet, run `seo-analytics-mcp doctor` for the Google Cloud Console "
            "steps, then `seo-analytics-mcp auth --client-secret <path>`."
        )
    if isinstance(exc, WrongClientType):
        return ToolError(
            "That client_secret.json is not a Desktop-app client (it contains "
            f"{exc.found}, not 'installed'). In Google Cloud Console create an OAuth "
            "client of type 'Desktop app' and download that one instead - a Web client "
            "cannot use the loopback flow this server needs."
        )

    status, reason = _status(exc), _reason(exc)
    target = ctx.site_url or ctx.property_id or "this resource"

    if "invalid_grant" in str(exc) or reason == "invalid_grant":
        return ToolError(
            f"The stored Google authorisation for profile '{ctx.profile}' was rejected. "
            "The usual cause is an OAuth app still in 'Testing' publishing status, whose "
            "refresh tokens expire after 7 days. Fix it permanently: in Google Cloud Console "
            "open the OAuth consent screen and press 'Publish app', then re-authorise with "
            f"`{REAUTH}`. Run `{DOCTOR}` to confirm."
        )

    if status == 401:
        return ToolError(
            f"Google rejected the credentials for profile '{ctx.profile}'. "
            f"Re-authorise with `{REAUTH}`."
        )

    if status == 400:
        # GA4's most common failure by a wide margin: an incompatible dimension and
        # metric pair, or a name that does not exist. Google's own text is the useful
        # part here, so keep it and add the lookup that resolves it.
        return ToolError(
            f"Google rejected the request as invalid: {exc}. For GA4 this is almost "
            "always an unknown field name or an incompatible dimension/metric pair - "
            "check the names against Google's GA4 API dimensions and metrics reference, "
            "and note that not every dimension can be queried with every metric. For "
            "Search Console, check the date format (YYYY-MM-DD) and that dimensions are "
            "drawn from query, page, country, device and date."
        )

    if status == 403:
        if reason in {"accessNotConfigured", "SERVICE_DISABLED"}:
            where = _activation_url(exc) or CONSOLE_APIS
            return ToolError(
                f"{ctx.api or 'The required API'} is not enabled on the Google Cloud project "
                f"that issued your OAuth client. Enable it here: {where} - then wait a minute "
                f"and retry, because activation takes a moment to propagate. `{DOCTOR}` lists "
                "which APIs answer."
            )
        if ctx.scope_needed:
            return ToolError(
                f"The stored token lacks the '{ctx.scope_needed}' scope. Re-authorise with "
                f"`{REAUTH} --enable-writes` to grant it. Read-only scopes are the default "
                "on purpose."
            )
        if ctx.service_account_email:
            return ToolError(
                f"The service account {ctx.service_account_email} has no access to {target}. "
                "Granting access is a separate step from deploying: add that email as a user "
                "in Search Console under Settings > Users and permissions (Full, if URL "
                "inspection is needed), and in GA4 under Admin > Property access management "
                "(Viewer). Access applies within a minute; no redeploy is required."
            )
        return ToolError(
            f"The authorised Google account has no access to {target}. Confirm the account "
            "you authorised is the one with access - Search Console grants are per-property "
            "under Settings > Users and permissions, GA4 grants are per-property under "
            f"Admin > Property access management. If access was added recently, run `{REAUTH}`."
        )

    if status == 404:
        return ToolError(
            f"{target} was not found. A Search Console domain property must be written as "
            "'sc-domain:example.com'; a URL-prefix property must include the scheme and a "
            "trailing slash. List what this account can see with gsc_list_sites or "
            "ga4_list_properties."
        )

    if status == 429 or reason == "rateLimitExceeded":
        return ToolError(
            "Google rate-limited this request. Search Console also enforces load limits in "
            "10-minute windows, where queries grouped by both page and query over long date "
            "ranges cost most. Wait 15 minutes, or narrow the date range and reduce row_limit."
        )

    if status is not None and 500 <= status < 600:
        return ToolError(
            f"Google returned a {status} server error. This is transient - retry shortly."
        )

    return ToolError(
        f"Unhandled failure calling {ctx.api or 'the Google API'}: "
        f"{type(exc).__name__}: {exc}. Run `{DOCTOR}` and include its output."
    )
