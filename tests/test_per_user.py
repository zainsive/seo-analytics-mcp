"""Per-person credentials: who a query runs as, and what they may see from cache.

The cache tests here matter more than they look. Once queries run as whoever is signed
in, a cache shared between people is a way around the permissions, and it fails
silently - the second person simply gets an answer.
"""

from __future__ import annotations

from types import SimpleNamespace

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
