"""Per-person access tokens, held in SQLite.

Why a database and not `SEO_MCP_TOKENS`:

* Only the SHA-256 of each token is stored. The database file can be copied off the
  box and it still grants nobody access, whereas an environment variable holds every
  live secret in plaintext for anything that can read the process or the .env.
* Revoking takes effect on the next request. Editing the environment needs a
  `systemctl restart`, which drops every connected client to remove one person.
* `last_used` shows which teammate is actually using the server and which token has
  gone quiet and should be withdrawn.

SQLite rather than a database server: this holds one small table, serves a few hundred
reads a day, and the host runs a public website in under 2 GB of RAM. A daemon, a port,
a password and a backup routine would all be new ways to fail for no gain - and when a
database server is down nobody can authenticate at all, while a file cannot fail to
start.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DB_VAR = "SEO_MCP_TOKEN_DB"
TOKEN_BYTES = 32  # 256 bits from the OS CSPRNG
LAST_USED_GRANULARITY = 60  # seconds; see _touch

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    label      TEXT NOT NULL,
    digest     TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    last_used  TEXT,
    note       TEXT
);
"""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def digest(token: str) -> str:
    """SHA-256, deliberately not bcrypt or argon2.

    A slow KDF exists to make guessing a human-chosen password expensive. These tokens
    are 256 bits of CSPRNG output, so there is no dictionary to work through and no
    preimage to find; a slow hash would only add latency to every single request.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def db_path() -> Path:
    if raw := os.environ.get(DB_VAR):
        return Path(raw).expanduser()
    from . import config

    return config.config_root() / "tokens.db"


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) and apply the schema. Safe to call repeatedly.

    A fresh connection per operation: opening SQLite costs tens of microseconds, which
    is far cheaper than reasoning about one shared handle across an event loop and a
    thread pool.
    """
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # WAL so recording last_used never blocks a concurrent lookup.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    conn.commit()
    if fresh:
        # Only ever readable by the service account, like the credentials beside it.
        os.chmod(path, 0o600)
    return conn


def mint(
    label: str, *, days: int | None = None, note: str | None = None, path: Path | None = None
) -> str:
    """Create a token and return it. This is the only moment it exists in readable form."""
    label = label.strip()
    if not label:
        raise ValueError("A token needs a label saying who it is for.")
    token = secrets.token_urlsafe(TOKEN_BYTES)
    expires = _iso(_now() + timedelta(days=days)) if days else None
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO tokens (label, digest, created_at, expires_at, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (label, digest(token), _iso(_now()), expires, note),
        )
    return token


def _touch(conn: sqlite3.Connection, row_id: int, last_used: str | None) -> None:
    """Record use at minute granularity.

    Writing on every request would mean a disk write per MCP call for an audit trail
    nobody reads to the second, so a token already marked within the last minute is
    left alone. Read `last_used` as "active around then", not as an exact timestamp.
    """
    now = _now()
    if last_used:
        try:
            if (now - datetime.fromisoformat(last_used)).total_seconds() < LAST_USED_GRANULARITY:
                return
        except ValueError:
            pass  # unparseable value: overwrite it with a good one
    conn.execute("UPDATE tokens SET last_used = ? WHERE id = ?", (_iso(now), row_id))
    conn.commit()


def identify(presented: str, path: Path | None = None) -> str | None:
    """Return the label for a live token, or None.

    The lookup is by hash, so an attacker cannot learn a valid token from timing: they
    would have to find a SHA-256 preimage. That is strictly better than comparing the
    presented string against every known token in turn.
    """
    if not presented:
        return None
    try:
        conn = connect(path)
    except sqlite3.Error:
        return None  # no store, or unreadable: callers fall back to the environment
    try:
        row = conn.execute(
            "SELECT id, label, expires_at, revoked_at, last_used FROM tokens WHERE digest = ?",
            (digest(presented),),
        ).fetchone()
        if row is None or row["revoked_at"]:
            return None
        if row["expires_at"] and row["expires_at"] <= _iso(_now()):
            return None
        _touch(conn, row["id"], row["last_used"])
        return str(row["label"])
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def is_known(presented: str, path: Path | None = None) -> bool:
    """Whether this token was ever issued here, live or not.

    Lets the caller tell "never heard of it" from "withdrawn", so a revocation cannot
    be undone by the same string lingering in an environment variable.
    """
    if not presented:
        return False
    try:
        conn = connect(path)
    except sqlite3.Error:
        return False
    try:
        return (
            conn.execute("SELECT 1 FROM tokens WHERE digest = ?", (digest(presented),)).fetchone()
            is not None
        )
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def revoke(selector: str, *, path: Path | None = None) -> list[str]:
    """Revoke by label or by id. Returns the labels revoked.

    Revoked rows are kept, not deleted: the record of who had access and when it was
    withdrawn is the point of an audit trail.
    """
    selector = selector.strip()
    with connect(path) as conn:
        clause, value = ("id = ?", selector) if selector.isdigit() else ("label = ?", selector)
        rows = conn.execute(
            f"SELECT id, label FROM tokens WHERE {clause} AND revoked_at IS NULL",  # noqa: S608
            (value,),
        ).fetchall()
        if rows:
            conn.execute(
                f"UPDATE tokens SET revoked_at = ? WHERE {clause} AND revoked_at IS NULL",  # noqa: S608
                (_iso(_now()), value),
            )
    return [str(r["label"]) for r in rows]


def _state(row: sqlite3.Row, now: str) -> str:
    if row["revoked_at"]:
        return "revoked"
    if row["expires_at"] and row["expires_at"] <= now:
        return "expired"
    return "active"


def rows(*, path: Path | None = None) -> list[dict[str, Any]]:
    """Every token for display. Carries no secret - the digest is deliberately omitted."""
    now = _iso(_now())
    with connect(path) as conn:
        found = conn.execute("SELECT * FROM tokens ORDER BY id").fetchall()
    return [
        {
            "id": r["id"],
            "label": r["label"],
            "state": _state(r, now),
            "created": (r["created_at"] or "")[:10],
            "expires": (r["expires_at"] or "")[:10] or None,
            "last_used": r["last_used"],
            "note": r["note"],
        }
        for r in found
    ]


def active_count(path: Path | None = None) -> int:
    try:
        return sum(1 for r in rows(path=path) if r["state"] == "active")
    except sqlite3.Error:
        return 0
