"""The access-token store, and the gate that reads it."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from seo_mcp import bearer
from seo_mcp import tokens as store


@pytest.fixture
def db(tmp_path):
    return tmp_path / "tokens.db"


def test_a_minted_token_identifies_its_owner(db):
    token = store.mint("alice", path=db)
    assert store.identify(token, db) == "alice"


def test_an_unissued_token_identifies_nobody(db):
    store.mint("alice", path=db)
    assert store.identify("not-a-real-token", db) is None
    assert store.identify("", db) is None


def _all_bytes(db) -> bytes:
    """The database plus its write-ahead log and shared-memory file.

    In WAL mode a fresh commit lives in the -wal file, so checking only the .db would
    pass while the secret sat in plaintext next to it.
    """
    return b"".join(
        p.read_bytes()
        for p in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm"))
        if p.exists()
    )


def test_the_token_itself_is_never_written_to_disk(db):
    """The whole reason for a store rather than an environment variable: these files can
    be copied off the box and must grant nobody anything."""
    token = store.mint("alice", path=db)
    raw = _all_bytes(db)
    assert token.encode() not in raw
    assert store.digest(token).encode() in raw  # the hash is what is kept


def test_the_token_is_still_absent_after_a_checkpoint(db):
    """Guard against the previous test passing only because the write had not landed."""
    token = store.mint("alice", path=db)
    with store.connect(db) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert token.encode() not in _all_bytes(db)


def test_use_and_revocation_do_not_write_the_token_either(db):
    token = store.mint("alice", path=db)
    store.identify(token, db)
    store.revoke("alice", path=db)
    assert token.encode() not in _all_bytes(db)


def test_two_tokens_never_collide(db):
    assert len({store.mint(f"user{i}", path=db) for i in range(50)}) == 50


def test_revoking_takes_effect_without_a_restart(db):
    """No process is restarted anywhere in this test - that is the point. The gate
    queries per request, so withdrawing access cannot require disconnecting everyone."""
    token = store.mint("bob", path=db)
    assert store.identify(token, db) == "bob"
    assert store.revoke("bob", path=db) == ["bob"]
    assert store.identify(token, db) is None


def test_a_revoked_token_is_kept_as_a_record_not_deleted(db):
    store.mint("bob", path=db)
    store.revoke("bob", path=db)
    row = next(r for r in store.rows(path=db) if r["label"] == "bob")
    assert row["state"] == "revoked"


def test_revoking_touches_only_the_named_person(db):
    alice, bob = store.mint("alice", path=db), store.mint("bob", path=db)
    store.revoke("bob", path=db)
    assert store.identify(alice, db) == "alice"
    assert store.identify(bob, db) is None


def test_a_token_can_be_revoked_by_its_listed_id(db):
    token = store.mint("carol", path=db)
    row_id = str(store.rows(path=db)[0]["id"])
    assert store.revoke(row_id, path=db) == ["carol"]
    assert store.identify(token, db) is None


def test_revoking_something_that_does_not_exist_reports_nothing_revoked(db):
    assert store.revoke("nobody", path=db) == []


def test_revoking_twice_is_not_an_error_but_changes_nothing(db):
    store.mint("bob", path=db)
    assert store.revoke("bob", path=db) == ["bob"]
    assert store.revoke("bob", path=db) == []


def test_an_expired_token_stops_working(db):
    token = store.mint("temp", days=1, path=db)
    past = (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds")
    with store.connect(db) as conn:
        conn.execute("UPDATE tokens SET expires_at = ?", (past,))
    assert store.identify(token, db) is None
    assert store.rows(path=db)[0]["state"] == "expired"


def test_a_token_inside_its_expiry_still_works(db):
    assert store.identify(store.mint("temp", days=30, path=db), db) == "temp"


def test_a_token_with_no_expiry_does_not_expire(db):
    store.mint("alice", path=db)
    assert store.rows(path=db)[0]["expires"] is None


def test_use_is_recorded_so_a_dormant_token_can_be_spotted(db):
    token = store.mint("alice", path=db)
    assert store.rows(path=db)[0]["last_used"] is None
    store.identify(token, db)
    assert store.rows(path=db)[0]["last_used"] is not None


def test_repeated_use_within_the_minute_does_not_rewrite_the_row(db):
    """One disk write per MCP call would buy an audit trail nobody reads to the second."""
    token = store.mint("alice", path=db)
    store.identify(token, db)
    first = store.rows(path=db)[0]["last_used"]
    for _ in range(20):
        store.identify(token, db)
    assert store.rows(path=db)[0]["last_used"] == first


def test_an_unparseable_last_used_is_replaced_rather_than_crashing(db):
    token = store.mint("alice", path=db)
    with store.connect(db) as conn:
        conn.execute("UPDATE tokens SET last_used = 'not a date'")
    assert store.identify(token, db) == "alice"
    assert store.rows(path=db)[0]["last_used"] != "not a date"


def test_a_label_is_required_so_no_token_is_anonymous(db):
    for bad in ("", "   "):
        with pytest.raises(ValueError, match="who it is for"):
            store.mint(bad, path=db)


def test_listing_exposes_no_secret(db):
    store.mint("alice", path=db)
    assert "digest" not in store.rows(path=db)[0]


def test_the_database_is_only_readable_by_its_owner(db):
    store.mint("alice", path=db)
    assert db.stat().st_mode & 0o077 == 0


def test_an_unreadable_store_denies_rather_than_admits(tmp_path, monkeypatch):
    """A broken store must fail closed. identify() returning a label on error would
    turn a disk problem into an open door."""
    monkeypatch.setattr(store, "connect", lambda *_a, **_k: (_ for _ in ()).throw(sqlite3.Error()))
    assert store.identify("anything", tmp_path / "x.db") is None
    assert store.is_known("anything", tmp_path / "x.db") is False


def test_opening_the_store_twice_does_not_lose_anything(db):
    token = store.mint("alice", path=db)
    store.connect(db).close()
    assert store.identify(token, db) == "alice"


# --- the gate ----------------------------------------------------------------


def test_the_server_refuses_to_start_with_no_token_anywhere(db, monkeypatch):
    """Serving openly is never the fallback."""
    monkeypatch.setenv(store.DB_VAR, str(db))
    monkeypatch.delenv(bearer.TOKENS_VAR, raising=False)
    with pytest.raises(SystemExit, match="Refusing to start"):
        bearer.build_resolver()


def test_the_refusal_names_the_command_that_fixes_it(db, monkeypatch):
    monkeypatch.setenv(store.DB_VAR, str(db))
    monkeypatch.delenv(bearer.TOKENS_VAR, raising=False)
    with pytest.raises(SystemExit) as exc:
        bearer.build_resolver()
    assert "token mint" in str(exc.value)


def test_a_minted_token_is_enough_to_start(db, monkeypatch):
    monkeypatch.setenv(store.DB_VAR, str(db))
    monkeypatch.delenv(bearer.TOKENS_VAR, raising=False)
    token = store.mint("alice", path=db)
    resolve, described = bearer.build_resolver()
    assert resolve(token) == "alice"
    assert "1 token(s)" in described


def test_environment_tokens_still_work_so_a_missing_database_cannot_brick_a_deployment(
    db, monkeypatch
):
    monkeypatch.setenv(store.DB_VAR, str(db))
    monkeypatch.setenv(bearer.TOKENS_VAR, "legacy:" + "x" * 30)
    resolve, described = bearer.build_resolver()
    assert resolve("x" * 30) == "legacy"
    assert "plaintext" in described  # and it says to move them


def test_the_environment_cannot_resurrect_a_revoked_token(db, monkeypatch):
    """A token revoked in the store must stay dead even if the same string is sitting
    in SEO_MCP_TOKENS - otherwise revocation is advisory."""
    monkeypatch.setenv(store.DB_VAR, str(db))
    token = store.mint("alice", path=db)
    monkeypatch.setenv(bearer.TOKENS_VAR, f"alice:{token}")
    resolve, _ = bearer.build_resolver()
    assert resolve(token) == "alice"
    store.revoke("alice", path=db)
    assert resolve(token) is None


def test_revocation_reaches_a_resolver_that_is_already_running(db, monkeypatch):
    monkeypatch.setenv(store.DB_VAR, str(db))
    monkeypatch.delenv(bearer.TOKENS_VAR, raising=False)
    token = store.mint("alice", path=db)
    resolve, _ = bearer.build_resolver()  # built before the revocation
    assert resolve(token) == "alice"
    store.revoke("alice", path=db)
    assert resolve(token) is None


# --- the gate, over real HTTP ------------------------------------------------


@pytest.fixture
def gated(db, monkeypatch):
    """A Starlette app with the real middleware in front of a trivial endpoint."""
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    monkeypatch.setenv(store.DB_VAR, str(db))
    monkeypatch.delenv(bearer.TOKENS_VAR, raising=False)
    token = store.mint("alice", path=db)

    async def data(request):
        return JSONResponse({"who": request.state.token_label})

    app = Starlette(
        routes=[
            Route("/mcp", data),
            Route("/healthz", lambda _r: PlainTextResponse("ok")),
        ]
    )
    resolve, _ = bearer.build_resolver()
    app.add_middleware(BaseHTTPMiddleware, dispatch=bearer.make_middleware(resolve))
    return TestClient(app, raise_server_exceptions=False), token


def test_a_request_with_no_token_is_refused(gated):
    client, _ = gated
    response = client.get("/mcp")
    assert response.status_code == 401
    assert "Bearer" in response.headers["www-authenticate"]


def test_the_refusal_says_what_to_send(gated):
    client, _ = gated
    assert "Authorization: Bearer" in client.get("/mcp").json()["detail"]


def test_a_wrong_token_is_refused(gated):
    client, _ = gated
    assert client.get("/mcp", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_the_wrong_scheme_is_refused(gated):
    client, token = gated
    assert client.get("/mcp", headers={"Authorization": f"Basic {token}"}).status_code == 401


def test_a_valid_token_gets_through_and_is_attributed(gated):
    client, token = gated
    response = client.get("/mcp", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {"who": "alice"}


def test_the_token_survives_surrounding_whitespace(gated):
    client, token = gated
    assert client.get("/mcp", headers={"Authorization": f"Bearer  {token} "}).status_code == 200


def test_the_health_check_stays_open_so_uptime_monitoring_works(gated):
    client, _ = gated
    assert client.get("/healthz").status_code == 200


def test_revoking_locks_a_live_client_out_on_its_next_request(db, gated):
    """The reason for querying per request: no restart, no disconnecting anyone else."""
    client, token = gated
    assert client.get("/mcp", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    store.revoke("alice", path=db)
    assert client.get("/mcp", headers={"Authorization": f"Bearer {token}"}).status_code == 401
