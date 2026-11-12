"""The OAuth authorization server, federated to Google.

An AI client sees an ordinary OAuth 2.1 provider: it registers itself, sends the person
to /authorize, and redeems a code at /token. What actually happens at /authorize is a
detour through Google, so this server never handles a password and never decides who
anybody is - it only decides whether the person Google vouched for is allowed in.

The SDK verifies PKCE, the redirect_uri round trip and client authentication before
calling anything here, so none of that is reimplemented.

Static access tokens keep working. `load_access_token` checks the signed-in tokens
first and falls back to the issued-by-hand ones, which is what lets Google sign-in be
added to a live deployment without cutting anybody off mid-conversation.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from .. import tokens as static_tokens
from . import google, store

SCOPE = "seo"  # one scope: everything here is read-only analytics


class GoogleFederatedProvider:
    """OAuthAuthorizationServerProvider backed by SQLite, authenticated by Google."""

    # --- clients --------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = store.load_client(client_id)
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Dynamic registration, which is how an AI client introduces itself.

        Anyone may register: registration alone grants nothing, because every token
        still requires a human to complete a Google sign-in this server approves of.
        """
        store.save_client(
            client_info.client_id,
            client_info.client_secret,
            client_info.model_dump(mode="json", exclude_none=True),
        )

    # --- authorize ------------------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Hand the browser to Google, remembering what to do when it comes back."""
        if not google.configured():
            raise AuthorizeError(
                error="server_error",
                error_description=(
                    "Google sign-in is not configured on this server. Set "
                    "GOOGLE_SSO_CLIENT_ID and GOOGLE_SSO_CLIENT_SECRET."
                ),
            )
        state = store.start_login(
            {
                "client_id": client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "code_challenge": params.code_challenge,
                "client_state": params.state,
                "scopes": params.scopes or [SCOPE],
                "resource": params.resource,
            }
        )
        return google.authorization_url(state)

    async def complete_login(self, state: str, google_code: str) -> str:
        """Called by the Google callback route. Returns where to send the browser next.

        Failures are raised, not redirected: an error carried back to the AI client as
        a redirect is invisible to the person staring at the browser, and this is the
        one place where a human is actually watching.
        """
        pending = store.finish_login(state)
        if pending is None:
            raise google.SignInRefused(
                "This sign-in link has already been used or has expired. Start again "
                "from your AI tool."
            )
        identity = await google.identify(google_code)
        code = store.issue_code(
            {
                "client_id": pending["client_id"],
                "redirect_uri": pending["redirect_uri"],
                "redirect_uri_provided_explicitly": pending["redirect_uri_provided_explicitly"],
                "code_challenge": pending["code_challenge"],
                "scopes": pending["scopes"],
                "subject": identity["email"],
                "resource": pending.get("resource"),
            }
        )
        return construct_redirect_uri(
            pending["redirect_uri"], code=code, state=pending.get("client_state")
        )

    # --- codes ----------------------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        payload = store.peek_code(authorization_code)
        if payload is None or payload["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=payload["scopes"],
            expires_at=time.time() + store.CODE_TTL,
            client_id=payload["client_id"],
            code_challenge=payload["code_challenge"],
            redirect_uri=AnyUrl(payload["redirect_uri"]),
            redirect_uri_provided_explicitly=payload["redirect_uri_provided_explicitly"],
            resource=payload.get("resource"),
            subject=payload["subject"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        payload = store.consume_code(authorization_code.code)
        if payload is None:
            # Already redeemed, or expired between load and exchange. Minting a second
            # pair of tokens for one code is the classic replay.
            raise TokenError(
                error="invalid_grant",
                error_description="That authorization code has already been used.",
            )
        return self._issue(
            client_id=client.client_id,
            subject=payload["subject"],
            scopes=payload["scopes"],
            resource=payload.get("resource"),
        )

    # --- refresh --------------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        record = store.load_token(refresh_token, "refresh")
        if record is None or record["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=record["client_id"],
            scopes=record["scopes"],
            expires_at=int(record["expires_at"]) if record["expires_at"] else None,
            subject=record["subject"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotate: the presented refresh token dies as the new pair is issued, so a
        # stolen one is good for at most one use and the theft shows up as a failure
        # for the real client rather than silent shared access.
        store.revoke_token(refresh_token.token)
        return self._issue(
            client_id=client.client_id,
            subject=refresh_token.subject or "unknown",
            scopes=scopes or refresh_token.scopes,
        )

    # --- verification ---------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Both kinds of credential arrive here.

        Signed-in tokens first, then the hand-issued static ones. The fallback is what
        lets this be switched on without disconnecting anybody.
        """
        if record := store.load_token(token, "access"):
            return AccessToken(
                token=token,
                client_id=record["client_id"],
                scopes=record["scopes"],
                expires_at=int(record["expires_at"]) if record["expires_at"] else None,
                subject=record["subject"],
            )
        if label := static_tokens.identify(token):
            return AccessToken(
                token=token,
                client_id=f"static:{label}",
                scopes=[SCOPE],
                expires_at=None,
                subject=label,
            )
        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        store.revoke_token(token.token)

    # --- helpers --------------------------------------------------------------

    def _issue(
        self,
        *,
        client_id: str,
        subject: str,
        scopes: list[str],
        resource: str | None = None,
    ) -> OAuthToken:
        access, expires_at = store.issue_token(
            "access", client_id=client_id, subject=subject, scopes=scopes, ttl=store.ACCESS_TTL
        )
        refresh, _ = store.issue_token(
            "refresh", client_id=client_id, subject=subject, scopes=scopes, ttl=store.REFRESH_TTL
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=int(expires_at - time.time()) if expires_at else None,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )


def settings() -> Any:
    """AuthSettings for the SDK, or None when Google sign-in is not configured.

    Never raises. This is called while the module is being imported, so a missing
    setting must degrade to "sign-in is off" with a warning rather than prevent the
    server from starting at all.
    """
    if not google.configured():
        return None
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions

    try:
        base = google.public_url()
    except Exception:
        logging.getLogger("seo_mcp").error(
            "Google sign-in is configured but SEO_MCP_PUBLIC_URL is not set, so it "
            "stays OFF. Set it to this server's public address, e.g. "
            "https://mcp.example.com, and restart."
        )
        return None
    if not (google.allowed_domains() or google.allowed_emails()):
        logging.getLogger("seo_mcp").error(
            "Google sign-in is configured but neither GOOGLE_SSO_ALLOWED_DOMAINS nor "
            "GOOGLE_SSO_ALLOWED_EMAILS is set, so it stays OFF. Enabling it without a "
            "policy would let any Google account in the world sign in."
        )
        return None
    return AuthSettings(
        issuer_url=AnyUrl(base),
        resource_server_url=AnyUrl(base),
        # Set explicitly, not left to the default. This server is both the
        # authorization server and the only resource behind it, so there is no second
        # audience a token could have been issued for and nothing for the check to
        # catch. Saying so here means a future SDK release flipping the default cannot
        # silently start rejecting every token, including the static ones, which carry
        # no resource indicator at all.
        validate_token_resource=False,
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
        ),
        required_scopes=[SCOPE],
    )
