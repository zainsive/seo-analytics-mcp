"""Google sign-in as the front door: storage, admission policy, and the whole flow.

The flow test drives the real SDK routes end to end with only Google's own HTTP calls
stubbed, because every interesting mistake in an OAuth implementation lives in the
seams between the handlers rather than inside any one of them.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from seo_mcp.sso import google, store
from seo_mcp.sso.provider import GoogleFederatedProvider

DOMAIN = "example.com"
PERSON = f"someone@{DOMAIN}"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """A throwaway database and a complete sign-in configuration."""
    monkeypatch.setenv("SEO_MCP_TOKEN_DB", str(tmp_path / "tokens.db"))
    monkeypatch.setattr(
        google.config,
        "active_profile",
        lambda: SimpleNamespace(
            google_sso_client_id="client-id.apps.googleusercontent.com",
            google_sso_client_secret="fixture-secret-not-a-real-credential",
            google_sso_domains=DOMAIN,
            google_sso_emails="contractor@elsewhere.test",
            public_url="https://mcp.example.com",
        ),
    )


# --- admission policy ---------------------------------------------------------


def test_a_person_from_the_allowed_domain_is_admitted():
    got = google._admit({"email": PERSON, "email_verified": True, "hd": DOMAIN})
    assert got["email"] == PERSON


def test_a_named_individual_outside_the_domain_is_admitted():
    got = google._admit({"email": "contractor@elsewhere.test", "email_verified": True})
    assert "allowlist" in got["via"]


def test_a_stranger_is_refused_and_told_who_may_enter():
    with pytest.raises(google.SignInRefused) as exc:
        google._admit({"email": "nobody@random.test", "email_verified": True})
    assert "not permitted" in str(exc.value) and DOMAIN in str(exc.value)


def test_a_gmail_address_that_merely_ends_in_the_domain_is_refused():
    """The hosted-domain claim is what separates a Workspace account from a personal
    one that happens to spell the domain in its local part."""
    with pytest.raises(google.SignInRefused):
        google._admit({"email": "attacker@notexample.com", "email_verified": True})
    with pytest.raises(google.SignInRefused):
        google._admit({"email": "x@evil.test", "email_verified": True, "hd": "evil.test"})


def test_an_unverified_google_address_is_refused():
    with pytest.raises(google.SignInRefused, match="unverified"):
        google._admit({"email": PERSON, "email_verified": False})


def test_an_identity_with_no_email_is_refused():
    with pytest.raises(google.SignInRefused, match="email"):
        google._admit({"email_verified": True})


def test_with_no_policy_configured_nobody_is_admitted(monkeypatch):
    """Failing closed matters more here than anywhere else: the open version of this
    mistake admits every Google account in existence."""
    monkeypatch.setattr(
        google.config,
        "active_profile",
        lambda: SimpleNamespace(
            google_sso_client_id="c",
            google_sso_client_secret="s",
            google_sso_domains=None,
            google_sso_emails=None,
            public_url="https://mcp.example.com",
        ),
    )
    with pytest.raises(google.SignInRefused, match="admit nobody"):
        google._admit({"email": PERSON, "email_verified": True})


def test_the_server_refuses_to_enable_sign_in_without_a_policy(monkeypatch):
    from seo_mcp.sso import provider

    monkeypatch.setattr(
        google.config,
        "active_profile",
        lambda: SimpleNamespace(
            google_sso_client_id="c",
            google_sso_client_secret="s",
            google_sso_domains=None,
            google_sso_emails=None,
            public_url="https://mcp.example.com",
        ),
    )
    assert provider.settings() is None  # off, rather than open


def test_sign_in_stays_off_without_a_public_url(monkeypatch):
    from seo_mcp.sso import provider

    monkeypatch.setattr(
        google.config,
        "active_profile",
        lambda: SimpleNamespace(
            google_sso_client_id="c",
            google_sso_client_secret="s",
            google_sso_domains=DOMAIN,
            google_sso_emails=None,
            public_url=None,
        ),
    )
    assert provider.settings() is None


def test_the_authorization_url_forces_an_account_choice():
    """Otherwise a shared machine silently reuses whoever signed in last."""
    params = parse_qs(urlparse(google.authorization_url("state-123")).query)
    assert params["prompt"] == ["select_account"]
    assert params["state"] == ["state-123"]
    assert params["redirect_uri"] == ["https://mcp.example.com/auth/google/callback"]
    assert params["hd"] == [DOMAIN]


# --- storage ------------------------------------------------------------------


def test_an_authorization_code_cannot_be_redeemed_twice():
    code = store.issue_code({"subject": PERSON})
    assert store.consume_code(code) == {"subject": PERSON}
    assert store.consume_code(code) is None


def test_a_pending_login_cannot_be_replayed():
    state = store.start_login({"client_id": "c"})
    assert store.finish_login(state) == {"client_id": "c"}
    assert store.finish_login(state) is None


def test_tokens_are_stored_hashed(tmp_path):
    """Same rule as the static tokens: the file can be copied off the box and still
    hands over nothing. Checks the write-ahead log too, where a fresh commit lives."""
    token, _ = store.issue_token("access", client_id="c", subject=PERSON, scopes=["seo"], ttl=60)
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("tokens.db*"))
    assert raw, "no database files found - the fixture path is wrong"
    assert token.encode() not in raw
    assert store.digest(token).encode() in raw


def test_an_expired_token_does_not_load():
    token, _ = store.issue_token("access", client_id="c", subject=PERSON, scopes=["seo"], ttl=-1)
    assert store.load_token(token, "access") is None


def test_a_token_of_the_wrong_kind_does_not_load():
    """A refresh token presented as a bearer credential must not open anything."""
    token, _ = store.issue_token("refresh", client_id="c", subject=PERSON, scopes=["seo"], ttl=60)
    assert store.load_token(token, "access") is None
    assert store.load_token(token, "refresh") is not None


def test_revoking_a_person_cuts_off_every_token_they_hold():
    held = [
        store.issue_token("access", client_id=f"c{i}", subject=PERSON, scopes=["seo"], ttl=60)[0]
        for i in range(3)
    ]
    other, _ = store.issue_token(
        "access", client_id="c", subject="other@example.com", scopes=["seo"], ttl=60
    )
    assert store.revoke_person(PERSON) == 3
    assert all(store.load_token(t, "access") is None for t in held)
    assert store.load_token(other, "access") is not None


def test_people_lists_who_has_signed_in():
    store.issue_token("access", client_id="c", subject=PERSON, scopes=["seo"], ttl=60)
    assert [p["email"] for p in store.people()] == [PERSON]


# --- the provider -------------------------------------------------------------


async def test_a_static_token_still_works_after_sign_in_is_enabled():
    """The reason this can be switched on without disconnecting anyone."""
    from seo_mcp import tokens as static_tokens

    static = static_tokens.mint("alice")
    got = await GoogleFederatedProvider().load_access_token(static)
    assert got is not None and got.subject == "alice"


async def test_an_unknown_token_opens_nothing():
    assert await GoogleFederatedProvider().load_access_token("not-a-token") is None


async def test_a_revoked_sign_in_token_stops_working():
    provider = GoogleFederatedProvider()
    token, _ = store.issue_token("access", client_id="c", subject=PERSON, scopes=["seo"], ttl=600)
    assert await provider.load_access_token(token) is not None
    store.revoke_token(token)
    assert await provider.load_access_token(token) is None


async def test_refreshing_rotates_the_refresh_token():
    """A stolen refresh token is then good for one use, and the theft surfaces as a
    failure for the real client rather than silent shared access."""
    from mcp.shared.auth import OAuthClientInformationFull

    provider = GoogleFederatedProvider()
    client = OAuthClientInformationFull(
        client_id="c", client_secret="s", redirect_uris=["https://ai.test/cb"]
    )
    issued = provider._issue(client_id="c", subject=PERSON, scopes=["seo"])
    old = issued.refresh_token
    assert old is not None

    loaded = await provider.load_refresh_token(client, old)
    assert loaded is not None
    again = await provider.exchange_refresh_token(client, loaded, ["seo"])
    assert again.refresh_token != old
    assert await provider.load_refresh_token(client, old) is None


async def test_a_code_belonging_to_another_client_is_not_loadable():
    from mcp.shared.auth import OAuthClientInformationFull

    provider = GoogleFederatedProvider()
    code = store.issue_code(
        {
            "client_id": "the-real-one",
            "redirect_uri": "https://ai.test/cb",
            "redirect_uri_provided_explicitly": True,
            "code_challenge": "x",
            "scopes": ["seo"],
            "subject": PERSON,
        }
    )
    impostor = OAuthClientInformationFull(
        client_id="someone-else", client_secret="s", redirect_uris=["https://ai.test/cb"]
    )
    assert await provider.load_authorization_code(impostor, code) is None


# --- the whole flow, through the SDK's own routes -----------------------------


@pytest.fixture
def app(monkeypatch):
    """The real Starlette app with auth enabled, Google's HTTP calls stubbed."""
    from mcp.server.auth.routes import create_auth_routes
    from mcp.server.auth.settings import ClientRegistrationOptions
    from pydantic import AnyUrl
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, RedirectResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    provider = GoogleFederatedProvider()

    async def fake_identify(code: str):
        if code == "bad-person":
            return google._admit({"email": "nobody@random.test", "email_verified": True})
        return google._admit({"email": PERSON, "email_verified": True, "hd": DOMAIN})

    monkeypatch.setattr(google, "identify", fake_identify)

    async def callback(request):
        try:
            destination = await provider.complete_login(
                request.query_params["state"], request.query_params["code"]
            )
        except google.SignInRefused as exc:
            return JSONResponse({"refused": str(exc)}, status_code=403)
        return RedirectResponse(destination, status_code=302)

    routes = list(
        create_auth_routes(
            provider=provider,
            issuer_url=AnyUrl("https://mcp.example.com"),
            service_documentation_url=None,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=["seo"], default_scopes=["seo"]
            ),
        )
    )
    routes.append(Route("/auth/google/callback", callback))
    return TestClient(Starlette(routes=routes)), provider


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return verifier, challenge


def test_the_whole_sign_in_flow_issues_a_working_token(app):
    client, provider = app
    verifier, challenge = _pkce()

    registered = client.post(
        "/register",
        json={
            "redirect_uris": ["https://ai.test/cb"],
            "token_endpoint_auth_method": "client_secret_post",
        },
    )
    assert registered.status_code == 201, registered.text
    creds = registered.json()

    # 1. the AI client sends the person to /authorize, which bounces them to Google
    authorize = client.get(
        "/authorize",
        params={
            "client_id": creds["client_id"],
            "redirect_uri": "https://ai.test/cb",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "client-state",
            "scope": "seo",
        },
        follow_redirects=False,
    )
    assert authorize.status_code in (302, 307)
    google_url = authorize.headers["location"]
    assert google_url.startswith(google.AUTH_ENDPOINT)
    state = parse_qs(urlparse(google_url).query)["state"][0]

    # 2. Google sends them back to us, and we bounce them to the AI client with a code
    back = client.get(
        "/auth/google/callback",
        params={"state": state, "code": "google-code"},
        follow_redirects=False,
    )
    assert back.status_code == 302, back.text
    returned = parse_qs(urlparse(back.headers["location"]).query)
    assert returned["state"] == ["client-state"]
    code = returned["code"][0]

    # 3. the AI client redeems the code
    token = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://ai.test/cb",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text
    body = token.json()
    assert body["token_type"] == "Bearer" and body["refresh_token"]

    # 4. and the token it got actually opens the door, as the right person
    import anyio

    verified = anyio.run(provider.load_access_token, body["access_token"])
    assert verified is not None and verified.subject == PERSON


def test_a_wrong_pkce_verifier_is_rejected(app):
    """Without this check a stolen authorization code is enough on its own."""
    client, _ = app
    _, challenge = _pkce()
    creds = client.post(
        "/register",
        json={
            "redirect_uris": ["https://ai.test/cb"],
            "token_endpoint_auth_method": "client_secret_post",
        },
    ).json()
    authorize = client.get(
        "/authorize",
        params={
            "client_id": creds["client_id"],
            "redirect_uri": "https://ai.test/cb",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "seo",
        },
        follow_redirects=False,
    )
    state = parse_qs(urlparse(authorize.headers["location"]).query)["state"][0]
    back = client.get(
        "/auth/google/callback",
        params={"state": state, "code": "google-code"},
        follow_redirects=False,
    )
    code = parse_qs(urlparse(back.headers["location"]).query)["code"][0]

    token = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://ai.test/cb",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "code_verifier": "wrong-verifier-entirely",
        },
    )
    assert token.status_code == 400
    assert "code_verifier" in token.text


def test_a_refused_person_gets_no_code(app):
    client, _ = app
    _, challenge = _pkce()
    creds = client.post(
        "/register",
        json={
            "redirect_uris": ["https://ai.test/cb"],
            "token_endpoint_auth_method": "client_secret_post",
        },
    ).json()
    authorize = client.get(
        "/authorize",
        params={
            "client_id": creds["client_id"],
            "redirect_uri": "https://ai.test/cb",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "seo",
        },
        follow_redirects=False,
    )
    state = parse_qs(urlparse(authorize.headers["location"]).query)["state"][0]
    refused = client.get(
        "/auth/google/callback",
        params={"state": state, "code": "bad-person"},
        follow_redirects=False,
    )
    assert refused.status_code == 403
    assert "not permitted" in refused.json()["refused"]


def test_the_discovery_document_advertises_the_endpoints(app):
    client, _ = app
    meta = client.get("/.well-known/oauth-authorization-server")
    assert meta.status_code == 200
    body = meta.json()
    assert body["authorization_endpoint"].endswith("/authorize")
    assert body["token_endpoint"].endswith("/token")
    assert "S256" in body["code_challenge_methods_supported"]


# --- the header alias, which is what keeps existing clients working -----------


def _alias_app():
    """A bare ASGI app behind the alias middleware, reporting what it received."""
    from seo_mcp.bearer import AliasHeaderMiddleware

    seen: dict[str, bytes | None] = {}

    async def app(scope, receive, send):
        headers = dict(scope["headers"])
        seen["authorization"] = headers.get(b"authorization")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return AliasHeaderMiddleware(app), seen


async def _call(middleware, headers: list[tuple[bytes, bytes]]):
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    await middleware({"type": "http", "headers": headers, "path": "/mcp"}, receive, send)


async def test_an_x_api_key_becomes_an_authorization_header():
    """The SDK's bearer backend reads Authorization and nothing else, but claude.ai
    will not let a connector set that name."""
    middleware, seen = _alias_app()
    await _call(middleware, [(b"x-api-key", b"abc123")])
    assert seen["authorization"] == b"Bearer abc123"


async def test_an_alias_already_carrying_bearer_is_not_doubled():
    middleware, seen = _alias_app()
    await _call(middleware, [(b"x-api-key", b"Bearer abc123")])
    assert seen["authorization"] == b"Bearer abc123"


async def test_a_real_authorization_header_is_left_alone():
    """A client's own OAuth token must never be displaced by an alias."""
    middleware, seen = _alias_app()
    await _call(middleware, [(b"authorization", b"Bearer real"), (b"x-api-key", b"other")])
    assert seen["authorization"] == b"Bearer real"


async def test_nothing_is_invented_when_no_credential_is_sent():
    middleware, seen = _alias_app()
    await _call(middleware, [(b"accept", b"application/json")])
    assert seen["authorization"] is None


async def test_the_header_name_is_matched_regardless_of_case():
    middleware, seen = _alias_app()
    await _call(middleware, [(b"X-API-KEY", b"abc123")])
    assert seen["authorization"] == b"Bearer abc123"


async def test_a_non_http_scope_passes_straight_through():
    """Lifespan messages must not be touched."""
    from seo_mcp.bearer import AliasHeaderMiddleware

    reached = []

    async def app(scope, receive, send):
        reached.append(scope["type"])

    await AliasHeaderMiddleware(app)({"type": "lifespan"}, None, None)
    assert reached == ["lifespan"]
