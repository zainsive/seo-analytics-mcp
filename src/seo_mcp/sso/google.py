"""The Google half of the sign-in: send the person to Google, check who came back.

This server is the OAuth authorization server as far as an AI client is concerned, but
it never sees a password. The actual login is Google's, and all this module does is
start that hop and verify the identity that returns from it.

Verification is delegated to google-auth rather than decoding the JWT here. It owns
signature checking against Google's rotating keys, the issuer and audience rules and
the clock skew allowance, and a hand-rolled version of that is how people end up
accepting unsigned tokens.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from mcp.server.mcpserver.exceptions import ToolError

from .. import config

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
CALLBACK_PATH = "/auth/google/callback"

# Who you are. Always requested; without these there is nobody to admit.
IDENTITY_SCOPES = ("openid", "email", "profile")

# What you may read. Requested in the same consent so that signing in once is the
# whole setup - asking later would mean a second trip through Google at the moment
# somebody is trying to get an answer.
DATA_SCOPES = (
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/analytics.readonly",
)
SCOPES = IDENTITY_SCOPES + DATA_SCOPES


class SignInRefused(Exception):
    """Google authenticated somebody this server will not admit."""


@dataclass(frozen=True)
class SignedIn:
    """A verified person, and the grant that lets the server act for them."""

    email: str
    name: str | None
    via: str
    refresh_token: str | None
    scopes: list[str]

    def granted(self, scope: str) -> bool:
        return scope in self.scopes


def configured() -> bool:
    profile = config.active_profile()
    return bool(profile.google_sso_client_id and profile.google_sso_client_secret)


def public_url() -> str:
    url = (config.active_profile().public_url or "").rstrip("/")
    if not url:
        raise ToolError(
            "SEO_MCP_PUBLIC_URL is not set. Google sign-in needs the server's own public "
            "address to build its redirect URI, and it must match the one registered in "
            "the Google console exactly - for example https://mcp.example.com"
        )
    return url


def redirect_uri() -> str:
    return f"{public_url()}{CALLBACK_PATH}"


def allowed_domains() -> list[str]:
    raw = config.active_profile().google_sso_domains or ""
    return [d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()]


def allowed_emails() -> list[str]:
    raw = config.active_profile().google_sso_emails or ""
    return [e.strip().lower() for e in raw.split(",") if e.strip()]


def authorization_url(state: str) -> str:
    """Where to send the person's browser to sign in."""
    profile = config.active_profile()
    params = {
        "client_id": profile.google_sso_client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        # Without this Google issues an access token good for an hour and no refresh
        # token, so the server could act for somebody exactly once.
        "access_type": "offline",
        # select_account forces the chooser, because a shared machine otherwise reuses
        # whoever signed in last. consent is what makes Google re-issue a refresh token
        # for somebody who has authorised before; without it a returning person gets
        # none and there is nothing to store.
        "prompt": "select_account consent",
        "include_granted_scopes": "true",
    }
    if domains := allowed_domains():
        # A hint only - it pre-fills the chooser. The claim is still checked below,
        # because anyone can edit this out of the URL.
        params["hd"] = domains[0]
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


async def identify(code: str) -> SignedIn:
    """Exchange Google's code for a verified person and their grant, or refuse.

    Raises SignInRefused with a sentence fit to show somebody in a browser, because
    that is exactly where it ends up.
    """
    profile = config.active_profile()

    def work() -> dict[str, Any]:
        import google.auth.transport.requests
        import google.oauth2.id_token
        import requests

        response = requests.post(
            TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": profile.google_sso_client_id,
                "client_secret": profile.google_sso_client_secret,
                "redirect_uri": redirect_uri(),
                "grant_type": "authorization_code",
            },
            timeout=20,
        )
        if response.status_code >= 400:
            raise SignInRefused(
                "Google would not complete the sign-in. The usual cause is a redirect "
                f"URI mismatch: this server uses {redirect_uri()}, which must appear "
                "verbatim in the Google console under Authorised redirect URIs."
            )
        payload = response.json()
        token = payload.get("id_token")
        if not token:
            raise SignInRefused("Google returned no identity token, so nobody can be identified.")
        claims = google.oauth2.id_token.verify_oauth2_token(
            token,
            google.auth.transport.requests.Request(),
            audience=profile.google_sso_client_id,
        )
        # Google reports what it actually granted, which can be less than was asked
        # for: the consent screen lets people untick things.
        return {
            "claims": claims,
            "refresh_token": payload.get("refresh_token"),
            "scopes": str(payload.get("scope") or "").split(),
        }

    try:
        result = await asyncio.to_thread(work)
    except SignInRefused:
        raise
    except Exception as exc:
        raise SignInRefused(f"Google sign-in could not be verified: {str(exc)[:200]}") from exc

    admitted = _admit(result["claims"])
    return SignedIn(
        email=admitted["email"],
        name=admitted.get("name"),
        via=admitted["via"],
        refresh_token=result["refresh_token"],
        scopes=result["scopes"],
    )


def _admit(claims: dict[str, Any]) -> dict[str, Any]:
    """Decide whether this verified Google identity may use the server."""
    email = str(claims.get("email") or "").lower()
    if not email:
        raise SignInRefused("Google did not return an email address, so access cannot be scoped.")
    if not claims.get("email_verified"):
        raise SignInRefused(
            f"The Google account {email} has an unverified email address, so it cannot be "
            "used to sign in here."
        )

    emails, domains = allowed_emails(), allowed_domains()
    if not emails and not domains:
        raise SignInRefused(
            "No sign-in policy is configured, so this server will admit nobody. Set "
            "GOOGLE_SSO_ALLOWED_DOMAINS to the company domain - refusing everyone is "
            "the safe failure here, since the alternative admits the whole internet."
        )

    if email in emails:
        return {"email": email, "name": claims.get("name"), "via": "email allowlist"}

    # The hosted-domain claim is what proves a Workspace account, rather than a Gmail
    # address that merely ends in the right letters.
    hosted = str(claims.get("hd") or "").lower()
    domain = email.rpartition("@")[2]
    if domains and (hosted in domains or (not hosted and domain in domains)):
        return {"email": email, "name": claims.get("name"), "via": f"domain {domain}"}

    raise SignInRefused(
        f"{email} is not permitted to use this connector. Access is limited to "
        + (f"{', '.join(domains)}" if domains else "a named list of addresses")
        + ". Ask whoever runs the server to add you."
    )
