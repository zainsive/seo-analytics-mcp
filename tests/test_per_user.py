"""Per-person credentials: who a query runs as, and what they may see from cache.

The cache tests here matter more than they look. Once queries run as whoever is signed
in, a cache shared between people is a way around the permissions, and it fails
silently - the second person simply gets an answer.
"""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import unquote_plus

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from seo_mcp import cache, credentials, identity
from seo_mcp.auth import scopes
from seo_mcp.config import Profile

ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture
def profile(tmp_path):
    return Profile(name="default", root=tmp_path)


@pytest.fixture
def as_nobody(monkeypatch):
    monkeypatch.setattr(identity, "caller", lambda: None)


def signed_in_as(monkeypatch, email):
    monkeypatch.setattr(identity, "caller", lambda: email)
    monkeypatch.setattr(cache.identity, "caller", lambda: email)
    monkeypatch.setattr(credentials.identity, "caller", lambda: email)


def _a_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


# --- who is calling -----------------------------------------------------------


def test_outside_a_request_there_is_no_caller():
    """The CLI and the stdio server have no HTTP context, and must not blow up."""
    assert identity.caller() is None


def test_a_static_token_is_not_mistaken_for_a_person(monkeypatch):
    """It is a credential, not a human. Treating it as one would let it inherit
    whichever person signed in most recently."""
    token = SimpleNamespace(client_id="static:automation", subject="automation")
    monkeypatch.setattr("mcp.server.auth.middleware.auth_context.get_access_token", lambda: token)
    assert identity.caller() is None


def test_a_signed_in_person_is_identified_by_email(monkeypatch):
    token = SimpleNamespace(client_id="abc-123", subject="Alice@Example.com")
    monkeypatch.setattr("mcp.server.auth.middleware.auth_context.get_access_token", lambda: token)
    assert identity.caller() == ALICE  # lowercased, so it matches what is stored


# --- cache isolation ----------------------------------------------------------


def test_two_people_asking_the_same_question_get_different_cache_keys(profile, monkeypatch):
    params = {"site_url": "sc-domain:example.com", "days": 28}
    signed_in_as(monkeypatch, ALICE)
    alice_key = cache.key(profile, "gsc.search", params)
    signed_in_as(monkeypatch, BOB)
    bob_key = cache.key(profile, "gsc.search", params)
    assert alice_key != bob_key


def test_one_person_asking_twice_hits_the_same_entry(profile, monkeypatch):
    """Isolation must not cost everyone their cache."""
    params = {"site_url": "sc-domain:example.com"}
    signed_in_as(monkeypatch, ALICE)
    assert cache.key(profile, "gsc.search", params) == cache.key(profile, "gsc.search", params)


def test_a_signed_in_person_does_not_share_a_key_with_an_anonymous_caller(profile, monkeypatch):
    """The anonymous path uses the server's own credential and sees everything, so
    sharing a key with it would be the widest possible leak."""
    params = {"site_url": "sc-domain:example.com"}
    signed_in_as(monkeypatch, ALICE)
    mine = cache.key(profile, "gsc.search", params)
    monkeypatch.setattr(cache.identity, "caller", lambda: None)
    assert cache.key(profile, "gsc.search", params) != mine


# --- which credential a query runs as -----------------------------------------


def test_a_signed_in_person_who_has_connected_nothing_is_told_to_sign_in(monkeypatch, tmp_path):
    monkeypatch.setenv("SEO_MCP_TOKEN_DB", str(tmp_path / "t.db"))
    signed_in_as(monkeypatch, ALICE)
    with pytest.raises(ToolError) as exc:
        credentials.for_scopes({scopes.GSC_READ})
    assert ALICE in str(exc.value) and "sign in again" in str(exc.value)


def test_a_missing_scope_is_named_in_words_a_person_can_act_on(monkeypatch, tmp_path):
    """ "webmasters.readonly" tells somebody nothing about what to click."""
    from cryptography.fernet import Fernet

    from seo_mcp.sso import connections, vault

    monkeypatch.setenv("SEO_MCP_TOKEN_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("SEO_MCP_SECRET_KEY", Fernet.generate_key().decode())
    vault._cipher.cache_clear()
    connections.remember(ALICE, connections.GOOGLE, "r", [scopes.GA4_READ])
    signed_in_as(monkeypatch, ALICE)

    with pytest.raises(ToolError) as exc:
        credentials.for_scopes({scopes.GSC_READ})
    assert "Search Console" in str(exc.value)
    assert "webmasters.readonly" not in str(exc.value)


def test_a_signed_in_person_never_falls_through_to_the_service_account(monkeypatch, tmp_path):
    """The failure this whole arrangement exists to prevent: somebody sees data their
    own account cannot reach, and nothing looks wrong."""
    monkeypatch.setenv("SEO_MCP_TOKEN_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("SEO_MCP_SERVICE_ACCOUNT_JSON", "{}")  # present but must be ignored
    signed_in_as(monkeypatch, ALICE)
    with pytest.raises(ToolError, match="has not connected"):
        credentials.for_scopes({scopes.GSC_READ})


def test_an_anonymous_caller_still_uses_the_servers_own_credential(monkeypatch, as_nobody):
    """Static tokens and the CLI keep working exactly as before."""
    monkeypatch.setattr(credentials.identity, "caller", lambda: None)
    monkeypatch.setattr(credentials, "_service_account_info", lambda: None)
    captured = {}
    monkeypatch.setattr(
        credentials.store, "load", lambda profile, required: captured.update(required=required)
    )
    credentials.for_scopes({scopes.GSC_READ})
    assert captured["required"] == {scopes.GSC_READ}


async def test_a_403_for_a_signed_in_person_names_them_not_the_service_account():
    """The likeliest failure after per-person access ships: the person granted the
    scope but was never added to the property. Telling them to grant access to the
    service account sends them to fix something that is not in their way."""
    import json as _json

    import httplib2
    from googleapiclient.errors import HttpError

    from seo_mcp.errors import Ctx, translate

    exc = HttpError(
        httplib2.Response({"status": 403}),
        _json.dumps({"error": {"code": 403, "errors": [{"reason": "forbidden"}]}}).encode(),
        uri="u",
    )
    message = str(
        translate(
            exc,
            Ctx(
                site_url="sc-domain:example.com",
                grantee="bob@example.com",
                grantee_is_person=True,
            ),
        )
    )
    assert "bob@example.com has no access to sc-domain:example.com" in message
    assert "service account" not in message
    # Signing in again is the fix for a missing scope and useless for a missing grant.
    # Confusing the two is how somebody loops through consent screens for an hour.
    assert "signing in again will not change it" in message.lower()


async def test_auth_status_reports_the_caller_as_the_one_who_needs_the_grant(monkeypatch):
    """describe() answers "who needs adding to the property". With a person calling,
    that is the person - even on a server that holds a service account."""
    from seo_mcp import credentials

    monkeypatch.setattr(credentials.identity, "caller", lambda: "bob@example.com")
    monkeypatch.setattr(credentials.connections, "recall", lambda subject, **kw: None)

    described = credentials.describe()
    assert described["mode"] == "per_person"
    assert described["grantee"] == "bob@example.com"
    # The headline must not claim access the caller does not have.
    assert described["authorised"] is False
    assert "bob@example.com" in described["note"]


def _as_hosted(monkeypatch, key_info):
    """Make the process look like the deployed server: a service-account key, no
    client secret and no stored token.

    The API probes are stubbed out. They are shared with the local-install path and
    tested there, and left live they would have this suite calling Google over the
    network to find out whether a generated throwaway key can read Search Console.
    """
    import json as _json

    from seo_mcp import cli, credentials

    monkeypatch.setenv(credentials.SERVICE_ACCOUNT_JSON, _json.dumps(key_info))
    monkeypatch.delenv(credentials.SERVICE_ACCOUNT_FILE, raising=False)

    async def no_probes(profile, creds, held):
        return []

    monkeypatch.setattr(cli, "_doctor_apis", no_probes)


async def test_doctor_does_not_tell_a_working_hosted_server_it_is_broken(monkeypatch, key_info):
    """doctor ran the local-install checks unconditionally, so on the deployed server
    it reported "no client secret", "no token stored" and "the server will not work
    until they pass" - on a server that was serving fine at that moment."""
    from seo_mcp import cli, config

    _as_hosted(monkeypatch, key_info)
    checks = await cli._doctor(config.active_profile())
    names = {c["check"] for c in checks}

    assert "client secret" not in names
    assert "token" not in names
    assert "service account" in names
    detail = next(c["detail"] for c in checks if c["check"] == "service account")
    assert detail == key_info["client_email"]


async def test_doctor_fails_when_sign_in_is_on_but_nothing_can_be_stored(monkeypatch, key_info):
    """The quiet one. Google sign-in works, people consent happily, and then every
    grant fails to store because no encryption key is set - so it presents as consent
    being broken rather than as one unset variable."""
    from seo_mcp import cli, config
    from seo_mcp.sso import vault

    _as_hosted(monkeypatch, key_info)
    monkeypatch.setenv("GOOGLE_SSO_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_SSO_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("GOOGLE_SSO_ALLOWED_DOMAINS", "example.com")
    monkeypatch.delenv(vault.KEY_VAR, raising=False)

    checks = await cli._doctor(config.active_profile())
    key_check = next(c for c in checks if c["check"] == "credential key")
    assert key_check["ok"] is False
    assert "no grant can be stored" in key_check["detail"]

    # And with the key set, it passes.
    from cryptography.fernet import Fernet

    monkeypatch.setenv(vault.KEY_VAR, Fernet.generate_key().decode())
    vault._cipher.cache_clear()
    checks = await cli._doctor(config.active_profile())
    assert next(c for c in checks if c["check"] == "credential key")["ok"] is True


async def test_doctor_flags_sign_in_left_without_a_policy(monkeypatch, key_info):
    """Sign-in with no domain and no allowlist admits nobody, which is the safe
    failure - but it should be visible rather than discovered by a confused teammate."""
    from seo_mcp import cli, config

    _as_hosted(monkeypatch, key_info)
    monkeypatch.setenv("GOOGLE_SSO_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_SSO_CLIENT_SECRET", "csecret")
    monkeypatch.delenv("GOOGLE_SSO_ALLOWED_DOMAINS", raising=False)
    monkeypatch.delenv("GOOGLE_SSO_ALLOWED_EMAILS", raising=False)

    checks = await cli._doctor(config.active_profile())
    policy = next(c for c in checks if c["check"] == "google sign-in")
    assert policy["ok"] is False
    assert "admits nobody" in policy["detail"]


# --- YouTube reads as the person too ------------------------------------------


def test_the_sign_in_asks_for_youtube_in_the_same_consent(monkeypatch):
    """Consent can only be asked once without sending everyone round again, so the
    scopes YouTube needs go in the first screen even before YouTube is configured.
    Adding them later would cost every person another sign-in."""
    from seo_mcp.sso import google

    monkeypatch.setenv("GOOGLE_SSO_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_SSO_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SEO_MCP_PUBLIC_URL", "https://mcp.example.com")

    requested = set(google.SCOPES)
    assert scopes.for_youtube() <= requested
    assert scopes.for_read() <= requested

    # The scopes are percent-encoded into the query string, so decode before looking.
    url = google.authorization_url("state-123")
    decoded = unquote_plus(url)
    for scope in scopes.for_youtube():
        assert scope in decoded
    # Without these two a returning person yields no refresh token, so there would be
    # nothing to store and the grant would last exactly one hour.
    assert "access_type=offline" in url
    assert "prompt=select_account+consent" in url


async def test_youtube_reads_as_the_signed_in_person_not_the_shared_token(monkeypatch):
    """The shared channel-owner token is the thing this phase exists to stop using
    while a person is asking. If it were still preferred, everyone would see the
    channel through one account and it would look like it worked."""
    from seo_mcp import credentials as creds_mod
    from seo_mcp import youtube_auth

    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "the-shared-token")
    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_SECRET", "secret")
    assert youtube_auth.configured()  # the fallback is available and still ignored

    signed_in_as(monkeypatch, ALICE)
    monkeypatch.setattr(youtube_auth.identity, "caller", lambda: ALICE)

    asked: dict[str, object] = {}

    def fake_as_person(subject, required):
        asked["subject"], asked["required"] = subject, set(required)
        return SimpleNamespace(token="alice-youtube-token")

    monkeypatch.setattr(creds_mod, "as_person", fake_as_person)

    result = await youtube_auth.credentials()
    assert result.token == "alice-youtube-token"
    assert asked["subject"] == ALICE
    assert asked["required"] == scopes.for_youtube()


async def test_a_person_without_the_youtube_grant_is_told_in_plain_words(monkeypatch, tmp_path):
    """'yt-analytics.readonly' means nothing to somebody being asked to sign in again."""
    from seo_mcp.sso import connections

    monkeypatch.setenv("SEO_MCP_SECRET_KEY", _a_key())
    connections.remember(
        BOB, connections.GOOGLE, "refresh", sorted(scopes.for_read()), path=tmp_path / "c.db"
    )
    # credentials.connections IS this module, so capture the real function first -
    # patching the attribute would otherwise make recall call itself forever.
    real_recall = connections.recall
    monkeypatch.setattr(
        credentials.connections,
        "recall",
        lambda subject, **kw: real_recall(subject, path=tmp_path / "c.db"),
    )

    with pytest.raises(ToolError) as caught:
        credentials.as_person(BOB, scopes.for_youtube())

    message = str(caught.value)
    assert "YouTube Analytics" in message
    assert "yt-analytics.readonly" not in message  # the raw scope helps nobody


async def test_a_caller_who_is_not_a_person_still_uses_the_shared_token(monkeypatch):
    """A hand-issued token or a cron job has no Google account of its own, so the
    shared channel-owner credential stays exactly as it was."""
    from seo_mcp import youtube_auth

    monkeypatch.setattr(youtube_auth.identity, "caller", lambda: None)
    monkeypatch.delenv("YOUTUBE_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("YOUTUBE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("YOUTUBE_OAUTH_CLIENT_SECRET", raising=False)

    assert youtube_auth.source() == "none"
    with pytest.raises(ToolError) as caught:
        await youtube_auth.credentials()
    assert "YOUTUBE_REFRESH_TOKEN" in str(caught.value)


def test_source_names_the_person_rather_than_the_missing_server_token(monkeypatch):
    """configured() answers whether the SERVER holds a token. Diagnostics that ask it
    about a signed-in person conclude 'not configured' and send them to run a CLI
    command on a machine they have never seen."""
    from seo_mcp import youtube_auth

    monkeypatch.delenv("YOUTUBE_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr(youtube_auth.identity, "caller", lambda: ALICE)
    assert youtube_auth.configured() is False
    assert youtube_auth.source() == "person"


async def test_the_not_connected_message_names_what_was_actually_asked_for(monkeypatch):
    """Answering a YouTube question with "nothing to read Search Console with" reads
    as the server having lost track of the question."""
    monkeypatch.setattr(credentials.connections, "recall", lambda subject, **kw: None)

    youtube = str(
        pytest.raises(ToolError, credentials.as_person, ALICE, scopes.for_youtube()).value
    )
    assert "YouTube" in youtube
    assert "Search Console" not in youtube

    search = str(pytest.raises(ToolError, credentials.as_person, ALICE, {scopes.GSC_READ}).value)
    assert "Search Console" in search
    assert "YouTube" not in search
