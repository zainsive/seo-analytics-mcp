"""Storage for the Google sign-in front door.

Shares the one SQLite file with the static access tokens, so a deployment still has a
single thing to back up and a single thing to chmod.

What is hashed and what is not, and why:

* Access and refresh tokens are stored as SHA-256 only, like the static tokens beside
  them. The file can be copied off the box and it hands over nothing.
* Authorization codes are hashed too, and deleted the moment they are redeemed.
* Client secrets are stored recoverable, because the SDK's client authenticator
  compares `client.client_secret` against the presented value with hmac.compare_digest
  and so must be handed the real thing. That is a constraint of the protocol
  implementation, not a choice. The exposure is small: a client secret alone opens
  nothing, since the authorization code flow still requires a human to complete a
  Google sign-in, and the file is 0600.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from .. import tokens

# Codes are redeemed within seconds in practice; anything older is a replay or a
# half-finished login nobody came back to.
CODE_TTL = 300
ACCESS_TTL = 60 * 60
REFRESH_TTL = 60 * 60 * 24 * 30
PENDING_TTL = 600  # one Google round-trip, generously

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sso_clients (
    client_id     TEXT PRIMARY KEY,
    client_secret TEXT,
    info          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sso_pending (
    state      TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sso_codes (
    code_hash  TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sso_tokens (
    token_hash TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    client_id  TEXT NOT NULL,
    subject    TEXT NOT NULL,
    scopes     TEXT NOT NULL,
    expires_at REAL,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    last_used  TEXT
);
CREATE INDEX IF NOT EXISTS sso_tokens_subject ON sso_tokens(subject);
"""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def connect(path: Path | None = None) -> sqlite3.Connection:
    """The access-token database, with the sign-in tables added."""
    conn = tokens.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _sweep(conn: sqlite3.Connection) -> None:
    """Drop whatever has aged out. Called on every write, which is often enough for a
    table that sees a handful of rows a day and avoids needing a scheduled job."""
    now = time.time()
    conn.execute("DELETE FROM sso_pending WHERE expires_at < ?", (now,))
    conn.execute("DELETE FROM sso_codes WHERE expires_at < ?", (now,))
    conn.execute(
        "DELETE FROM sso_tokens WHERE expires_at IS NOT NULL AND expires_at < ?",
        (now - REFRESH_TTL,),
    )


# --- clients ------------------------------------------------------------------


def save_client(client_id: str, client_secret: str | None, info: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sso_clients (client_id, client_secret, info, created_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (client_id, client_secret, json.dumps(info)),
        )


def load_client(client_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT info FROM sso_clients WHERE client_id = ?", (client_id,)
        ).fetchone()
    return json.loads(row["info"]) if row else None


def client_count() -> int:
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) AS n FROM sso_clients").fetchone()["n"])


# --- the Google round trip ----------------------------------------------------


def start_login(payload: dict[str, Any]) -> str:
    """Park an in-flight authorization request and return the state to send to Google."""
    state = secrets.token_urlsafe(24)
    with connect() as conn:
        _sweep(conn)
        conn.execute(
            "INSERT INTO sso_pending (state, payload, expires_at) VALUES (?, ?, ?)",
            (state, json.dumps(payload), time.time() + PENDING_TTL),
        )
    return state


def finish_login(state: str) -> dict[str, Any] | None:
    """Claim a parked request. Single use: a replayed callback finds nothing."""
    with connect() as conn:
        row = conn.execute(
            "SELECT payload, expires_at FROM sso_pending WHERE state = ?", (state,)
        ).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM sso_pending WHERE state = ?", (state,))
        if row["expires_at"] < time.time():
            return None
    return json.loads(row["payload"])


# --- authorization codes ------------------------------------------------------


def issue_code(payload: dict[str, Any]) -> str:
    code = secrets.token_urlsafe(32)
    with connect() as conn:
        _sweep(conn)
        conn.execute(
            "INSERT INTO sso_codes (code_hash, payload, expires_at) VALUES (?, ?, ?)",
            (digest(code), json.dumps(payload), time.time() + CODE_TTL),
        )
    return code


def peek_code(code: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT payload, expires_at FROM sso_codes WHERE code_hash = ?", (digest(code),)
        ).fetchone()
    if row is None or row["expires_at"] < time.time():
        return None
    return json.loads(row["payload"])


def consume_code(code: str) -> dict[str, Any] | None:
    """Redeem once. A code presented twice is either a bug or an attack; either way the
    second attempt must fail rather than mint a second pair of tokens."""
    with connect() as conn:
        row = conn.execute(
            "SELECT payload, expires_at FROM sso_codes WHERE code_hash = ?", (digest(code),)
        ).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM sso_codes WHERE code_hash = ?", (digest(code),))
        if row["expires_at"] < time.time():
            return None
    return json.loads(row["payload"])


# --- tokens -------------------------------------------------------------------


def issue_token(
    kind: str, *, client_id: str, subject: str, scopes: list[str], ttl: int | None
) -> tuple[str, float | None]:
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + ttl if ttl else None
    with connect() as conn:
        _sweep(conn)
        conn.execute(
            "INSERT INTO sso_tokens (token_hash, kind, client_id, subject, scopes, "
            "expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            (digest(token), kind, client_id, subject, " ".join(scopes), expires_at),
        )
    return token, expires_at


def load_token(token: str, kind: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM sso_tokens WHERE token_hash = ? AND kind = ?", (digest(token), kind)
        ).fetchone()
        if row is None or row["revoked_at"]:
            return None
        if row["expires_at"] is not None and row["expires_at"] < time.time():
            return None
        # Minute granularity, as for the static tokens: an audit trail nobody reads to
        # the second is not worth a disk write per request.
        if not row["last_used"] or time.time() - _parsed(row["last_used"]) > 60:
            conn.execute(
                "UPDATE sso_tokens SET last_used = datetime('now') WHERE token_hash = ?",
                (digest(token),),
            )
    return {
        "client_id": row["client_id"],
        "subject": row["subject"],
        "scopes": row["scopes"].split() if row["scopes"] else [],
        "expires_at": row["expires_at"],
    }


def _parsed(stamp: str) -> float:
    try:
        return time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return 0.0


def revoke_token(token: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE sso_tokens SET revoked_at = datetime('now') "
            "WHERE token_hash = ? AND revoked_at IS NULL",
            (digest(token),),
        )
    return cur.rowcount > 0


def revoke_person(subject: str) -> int:
    """Cut off one human everywhere, across every client they ever authorised."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE sso_tokens SET revoked_at = datetime('now') "
            "WHERE subject = ? AND revoked_at IS NULL",
            (subject,),
        )
    return cur.rowcount


def people() -> list[dict[str, Any]]:
    """Who has signed in, and when they were last active."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT subject, COUNT(*) AS tokens, MAX(last_used) AS last_used, "
            "MIN(created_at) AS first_seen, "
            "SUM(CASE WHEN revoked_at IS NULL THEN 1 ELSE 0 END) AS live "
            "FROM sso_tokens GROUP BY subject ORDER BY subject"
        ).fetchall()
    return [
        {
            "email": r["subject"],
            "first_seen": (r["first_seen"] or "")[:10],
            "last_used": r["last_used"],
            "live_tokens": r["live"],
        }
        for r in rows
    ]
