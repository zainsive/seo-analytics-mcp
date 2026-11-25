"""Mailchimp and LinkedIn, attached by each person to their own account.

These are not Google, and the difference drives everything here. A person signs in to
this server with Google, so a Google credential is always obtainable for them. Nobody
signs in with Mailchimp, so attaching one is a separate, deliberate act: the person is
already authenticated, and this is them handing over a second account.

Two written-out classes rather than a table of endpoints, because the platforms differ
in ways a table would have to grow a hook for anyway. Mailchimp has no scopes and no
expiry but hides its data centre behind a second call; LinkedIn has scopes, a hard
sixty-day expiry, and refuses to say anything useful without a version header.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

from .. import config
from . import connections

TIMEOUT = 20.0


class LinkRefused(Exception):
    """The platform would not complete the hand-over. Shown to a human in a browser."""


@dataclass(frozen=True)
class Linked:
    """What came back: the credential, plus the parts worth storing in the clear."""

    token: str
    scopes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


# --- Mailchimp -----------------------------------------------------------------


class Mailchimp:
    platform = connections.MAILCHIMP
    label = "Mailchimp"

    # Mailchimp's OAuth has no scopes at all. What a token can see is decided by the
    # role the person holds in the Mailchimp account, which is set in Mailchimp and
    # not here. That is the whole reason this is worth doing: today everybody reads
    # through one API key with whatever rights it was minted with.
    scopes: tuple[str, ...] = ()

    AUTHORIZE = "https://login.mailchimp.com/oauth2/authorize"
    TOKEN = "https://login.mailchimp.com/oauth2/token"
    METADATA = "https://login.mailchimp.com/oauth2/metadata"

    def configured(self) -> bool:
        profile = config.active_profile()
        return bool(profile.mailchimp_client_id and profile.mailchimp_client_secret)

    def setup_hint(self) -> str:
        return (
            "Mailchimp sign-in is not configured on this server. In Mailchimp go to "
            "Account & billing > Extras > Registered apps > Register an app, set the "
            "redirect URI to the one this server prints at startup, and set "
            "MAILCHIMP_CLIENT_ID and MAILCHIMP_CLIENT_SECRET in its environment."
        )

    def authorization_url(self, state: str, redirect_uri: str) -> str:
        profile = config.active_profile()
        return f"{self.AUTHORIZE}?" + urlencode(
            {
                "response_type": "code",
                "client_id": profile.mailchimp_client_id,
                "redirect_uri": redirect_uri,
                "state": state,
            }
        )

    async def exchange(self, code: str, redirect_uri: str) -> Linked:
        profile = config.active_profile()
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(
                self.TOKEN,
                data={
                    "grant_type": "authorization_code",
                    "client_id": profile.mailchimp_client_id,
                    "client_secret": profile.mailchimp_client_secret,
                    "redirect_uri": redirect_uri,
                    "code": code,
                },
                headers={"User-Agent": "seo-analytics-mcp"},
            )
            if response.status_code >= 400:
                raise LinkRefused(
                    "Mailchimp would not complete the connection. The usual cause is a "
                    f"redirect URI that does not match the one registered: this server "
                    f"uses {redirect_uri}, which must appear verbatim in the registered "
                    "app."
                )
            token = str((response.json() or {}).get("access_token") or "")
            if not token:
                raise LinkRefused("Mailchimp returned no access token, so there is nothing to use.")

            # The data centre is not in the token. Every later API call needs it in the
            # hostname, so it is fetched once here and stored - an API key carries it as
            # a '-us15' suffix, and an OAuth token simply does not.
            meta_response = await client.get(
                self.METADATA,
                headers={"Authorization": f"OAuth {token}", "User-Agent": "seo-analytics-mcp"},
            )
            if meta_response.status_code >= 400:
                raise LinkRefused(
                    "Mailchimp issued a token but would not say which data centre the "
                    "account lives in, so no query could be addressed to it."
                )
            payload = meta_response.json() or {}

        dc = str(payload.get("dc") or "")
        if not dc:
            raise LinkRefused("Mailchimp did not name a data centre for this account.")
        return Linked(
            token=token,
            scopes=[],
            meta={
                "data_center": dc,
                "account_name": (payload.get("accountname") or None),
                "login": ((payload.get("login") or {}).get("email") or None),
                # Mailchimp OAuth tokens do not expire and there is no refresh token,
                # so nothing here needs renewing on a timer.
                "expires": None,
            },
        )


# --- LinkedIn ------------------------------------------------------------------


class LinkedIn:
    platform = connections.LINKEDIN
    label = "LinkedIn"

    # Read-only members of the Community Management product. rw_organization_admin is
    # deliberately absent: it is the one that would let this write, and every LinkedIn
    # tool here is a read.
    scopes: tuple[str, ...] = ("r_organization_social",)

    AUTHORIZE = "https://www.linkedin.com/oauth/v2/authorization"
    TOKEN = "https://www.linkedin.com/oauth/v2/accessToken"

    def configured(self) -> bool:
        profile = config.active_profile()
        return bool(profile.linkedin_client_id and profile.linkedin_client_secret)

    def setup_hint(self) -> str:
        return (
            "LinkedIn sign-in is not configured on this server. Create an app at "
            "developer.linkedin.com, request the Community Management API product, add "
            "the redirect URI this server prints at startup, and set "
            "LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET in its environment."
        )

    def authorization_url(self, state: str, redirect_uri: str) -> str:
        profile = config.active_profile()
        return f"{self.AUTHORIZE}?" + urlencode(
            {
                "response_type": "code",
                "client_id": profile.linkedin_client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "scope": " ".join(self.scopes),
            }
        )

    async def exchange(self, code: str, redirect_uri: str) -> Linked:
        profile = config.active_profile()
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(
                self.TOKEN,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": profile.linkedin_client_id,
                    "client_secret": profile.linkedin_client_secret,
                    "redirect_uri": redirect_uri,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "seo-analytics-mcp",
                },
            )
        if response.status_code >= 400:
            raise LinkRefused(
                "LinkedIn would not complete the connection. The usual causes are a "
                f"redirect URI that is not registered on the app - this server uses "
                f"{redirect_uri} - or the Community Management API product not being "
                "approved yet."
            )
        payload = response.json() or {}
        token = str(payload.get("access_token") or "")
        if not token:
            raise LinkRefused("LinkedIn returned no access token, so there is nothing to use.")

        # LinkedIn member tokens last about sixty days and most apps are not approved
        # for refresh tokens, so the expiry is recorded to be reported rather than
        # discovered when somebody's queries start failing.
        seconds = payload.get("expires_in")
        granted = str(payload.get("scope") or "").replace(",", " ").split()
        return Linked(
            token=token,
            scopes=granted or list(self.scopes),
            meta={
                "expires_in_days": round(int(seconds) / 86400) if seconds else None,
                "refreshable": bool(payload.get("refresh_token")),
            },
        )


ALL = {p.platform: p for p in (Mailchimp(), LinkedIn())}


def named(platform: str):
    try:
        return ALL[platform]
    except KeyError:
        raise LinkRefused(
            f"{platform!r} is not a platform this server can connect. "
            f"Choose one of: {', '.join(sorted(ALL))}."
        ) from None
