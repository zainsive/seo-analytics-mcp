"""Attaching a second account to somebody already signed in.

The security of the whole flow rests on one property: the callback is an
unauthenticated URL, so nothing the browser sends may decide whose account this
becomes. That is what most of these tests are about.
"""

from __future__ import annotations

import pytest

from seo_mcp.sso import connections, link, platforms

ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture
def configured(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("SEO_MCP_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SEO_MCP_PUBLIC_URL", "https://mcp.example.com")
    monkeypatch.setenv("MAILCHIMP_CLIENT_ID", "mc-client")
    monkeypatch.setenv("MAILCHIMP_CLIENT_SECRET", "mc-secret")
    monkeypatch.setenv("LINKEDIN_CLIENT_ID", "li-client")
    monkeypatch.setenv("LINKEDIN_CLIENT_SECRET", "li-secret")


def test_the_callback_url_is_one_path_for_every_platform(configured):
    """Each platform has to have it registered on its own app, so there is exactly one
    string to copy rather than one per platform."""
    assert link.redirect_uri() == "https://mcp.example.com/auth/connect/callback"


def test_the_person_is_fixed_when_the_link_is_made_not_when_it_is_clicked(configured):
    """The whole flow rests on this. The callback is unauthenticated, so if the
    subject came from the request then anyone could attach their own Mailchimp to a
    colleague's email, or attach a colleague's account to themselves."""
    from seo_mcp.sso import store

    url = link.start(ALICE, "mailchimp")
    state = url.split("state=")[1].split("&")[0]

    parked = store.finish_login(state)
    assert parked["subject"] == ALICE
    assert parked["platform"] == "mailchimp"
    # Nothing in the URL sent to the platform carries the email for it to echo back.
    assert ALICE not in url


async def test_a_replayed_callback_attaches_nothing(configured):
    """State is single use. A link forwarded or re-opened must not connect twice."""
    url = link.start(ALICE, "mailchimp")
    state = url.split("state=")[1].split("&")[0]

    from seo_mcp.sso import store

    store.finish_login(state)  # the first, legitimate use

    with pytest.raises(platforms.LinkRefused) as caught:
        await link.finish(state, "some-code")
    assert "already been used" in str(caught.value)


async def test_a_state_from_the_google_flow_cannot_be_used_to_attach_an_account(configured):
    """Both flows park state in the same table. Without the kind check, a Google
    sign-in state would be claimable here, where the payload means something else."""
    from seo_mcp.sso import store

    state = store.start_login({"client_id": "abc", "redirect_uri": "https://x/cb"})
    with pytest.raises(platforms.LinkRefused):
        await link.finish(state, "code")


def test_an_unconfigured_platform_says_what_to_register(monkeypatch):
    monkeypatch.setenv("SEO_MCP_PUBLIC_URL", "https://mcp.example.com")
    monkeypatch.delenv("MAILCHIMP_CLIENT_ID", raising=False)
    monkeypatch.delenv("MAILCHIMP_CLIENT_SECRET", raising=False)

    with pytest.raises(platforms.LinkRefused) as caught:
        link.start(ALICE, "mailchimp")
    assert "Registered apps" in str(caught.value)


def test_an_unknown_platform_is_refused_rather_than_guessed(configured):
    with pytest.raises(platforms.LinkRefused) as caught:
        link.start(ALICE, "facebook")
    assert "linkedin" in str(caught.value) and "mailchimp" in str(caught.value)


def test_the_linkedin_request_asks_for_no_write_scope(configured):
    """Every LinkedIn tool here reads. Asking for the admin scope would be asking for
    permission to post as the Page, which nothing in this server does."""
    url = link.start(ALICE, "linkedin")
    assert "r_organization_social" in url
    assert "rw_organization_admin" not in url
    assert "w_member_social" not in url


async def test_a_finished_mailchimp_link_stores_the_data_centre_in_the_clear(
    configured, monkeypatch, tmp_path
):
    """The data centre is not a secret and every later call needs it in the hostname.
    Keeping it readable means a broken connection can be diagnosed without decrypting
    anybody's credential."""
    monkeypatch.setattr(connections, "connect", lambda path=None: _db(tmp_path))

    async def fake_exchange(code, redirect_uri):
        return platforms.Linked(
            token="mc-token", scopes=[], meta={"data_center": "us15", "account_name": "STG"}
        )

    monkeypatch.setattr(platforms.ALL["mailchimp"], "exchange", fake_exchange)

    url = link.start(ALICE, "mailchimp")
    state = url.split("state=")[1].split("&")[0]
    message = await link.finish(state, "the-code")

    assert ALICE in message
    stored = connections.recall(ALICE, connections.MAILCHIMP)
    assert stored.refresh_token == "mc-token"
    assert stored.meta["data_center"] == "us15"
    # And the token itself is not sitting in the file.
    assert b"mc-token" not in (tmp_path / "linked.db").read_bytes()


def _db(tmp_path):
    """A real connections database in a temporary file."""
    import sqlite3

    from seo_mcp.sso import store as sso_store

    conn = sso_store.connect(tmp_path / "linked.db")
    conn.executescript(connections._SCHEMA)
    held = {row["name"] for row in conn.execute("PRAGMA table_info(connections)")}
    if "meta" not in held:
        conn.execute("ALTER TABLE connections ADD COLUMN meta TEXT")
    conn.commit()
    assert isinstance(conn, sqlite3.Connection)
    return conn
