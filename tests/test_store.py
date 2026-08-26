"""Token file permissions, atomicity, and the age arithmetic doctor reports on."""

import datetime
import json
import os
import time

import pytest

from seo_mcp import config
from seo_mcp.auth import scopes, store
from seo_mcp.errors import AuthMissing, ScopeMissing, TokenCorrupt


@pytest.fixture
def profile():
    return config.active_profile()


def write_token(profile, *, scopes_held=None, granted_at=None, refresh="r", expires_in=3600):
    # A future expiry keeps load() from attempting a network refresh: no test in this
    # suite is allowed to touch the network.
    expiry = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=expires_in)
    payload = {
        "token": "t",
        "refresh_token": refresh,
        "client_id": "cid",
        "client_secret": "cs",
        "expiry": expiry.replace(tzinfo=None).isoformat(timespec="seconds") + "Z",
        "scopes": scopes_held if scopes_held is not None else sorted(scopes.for_read()),
    }
    if granted_at is not None:
        payload["_seo_mcp"] = {"granted_at": granted_at, "version": 1}
    store._write_private(profile.token_path, payload)
    return payload


@pytest.mark.skipif(not hasattr(os, "fchmod"), reason="POSIX permissions only")
def test_the_token_is_never_world_readable(profile):
    write_token(profile)
    assert oct(profile.token_path.stat().st_mode & 0o777) == "0o600"


def test_a_failed_write_leaves_no_temporary_file_behind(profile, monkeypatch):
    class Unserialisable:
        pass

    with pytest.raises(TypeError):
        store._write_private(profile.token_path, {"bad": Unserialisable()})
    assert list(profile.root.iterdir()) == []  # no .tok-* residue


def test_writing_twice_replaces_atomically_without_a_partial_file(profile):
    write_token(profile, scopes_held=["a"])
    write_token(profile, scopes_held=["b"])
    assert json.loads(profile.token_path.read_text())["scopes"] == ["b"]
    assert [p.name for p in profile.root.iterdir()] == ["token.json"]


def test_missing_token_raises_auth_missing(profile):
    with pytest.raises(AuthMissing):
        store.load(profile, scopes.for_read())


def test_a_token_without_the_required_scope_raises_scope_missing(profile):
    write_token(profile, scopes_held=[scopes.GA4_READ])
    with pytest.raises(ScopeMissing) as exc:
        store.load(profile, {scopes.GSC_READ})
    assert exc.value.missing == [scopes.GSC_READ]


def test_the_write_scope_implies_read_access(profile):
    write_token(profile, scopes_held=[scopes.GSC_WRITE, scopes.GA4_READ])
    creds = store.load(profile, scopes.for_read())
    assert creds.valid  # loaded from disk, no network refresh needed


def test_a_corrupt_token_file_is_reported_as_corrupt_not_as_missing(profile):
    profile.root.mkdir(parents=True, exist_ok=True)
    profile.token_path.write_text("{not json")
    with pytest.raises(TokenCorrupt):
        store.load(profile, set())


def test_scopes_held_reads_without_constructing_credentials(profile):
    write_token(profile, scopes_held=["z", "a"])
    assert store.scopes_held(profile) == ["a", "z"]
    assert store.scopes_held(config.profile_named("absent")) == []


def test_age_comes_from_the_recorded_grant_time_not_the_file_mtime(profile):
    write_token(profile, granted_at=time.time() - 10 * 86400)
    assert store.token_age_days(profile) == pytest.approx(10, abs=0.01)


def test_a_fresh_token_gets_the_seven_day_testing_caution(profile):
    write_token(profile, granted_at=time.time() - 2 * 86400)
    warning = store.testing_status_warning(profile)
    assert "Publish app" in warning and "day 7" in warning


def test_a_token_that_survived_past_day_seven_proves_the_app_was_published(profile):
    write_token(profile, granted_at=time.time() - 9 * 86400)
    assert store.testing_status_warning(profile) is None


def test_has_token_is_false_before_authorisation(profile):
    assert store.has_token(profile) is False
    write_token(profile)
    assert store.has_token(profile) is True
