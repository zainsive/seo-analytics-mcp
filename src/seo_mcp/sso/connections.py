"""What each signed-in person has connected, and the credentials to act as them.

One row per person per platform. Today only Google is stored here; Mailchimp and
LinkedIn will slot in beside it with the same shape, which is why `platform` is a
column rather than a table name.

The refresh token is the only field encrypted, because it is the only one that has to
be readable again. Scopes and timestamps are plain so that a question like "who still
needs to grant Analytics" can be answered without decrypting anything.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import store, vault

GOOGLE = "google"
MAILCHIMP = "mailchimp"
LINKEDIN = "linkedin"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    subject     TEXT NOT NULL,
    platform    TEXT NOT NULL,
    secret      TEXT NOT NULL,
    scopes      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    last_used   TEXT,
    meta        TEXT,
    PRIMARY KEY (subject, platform)
);
"""


@dataclass(frozen=True)
class Connection:
    """One person's access to one platform. `refresh_token` is already decrypted.

    `meta` carries the platform's non-secret details - Mailchimp's data centre, the
    account name somebody recognises, when a token runs out. It is stored in the clear
    on purpose: "whose LinkedIn expires this month" should be answerable without
    decrypting every credential in the table to find out.
    """

    subject: str
    platform: str
    refresh_token: str
    scopes: list[str]
    updated_at: str
    meta: dict[str, Any] = field(default_factory=dict)

    def covers(self, required: set[str]) -> bool:
        return required <= set(self.scopes)


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = store.connect(path)
    conn.executescript(_SCHEMA)
    # CREATE TABLE IF NOT EXISTS does nothing to a table that already exists, so a
    # deployment that ran the earlier schema would be missing `meta` and every read
    # would fail on a column that the code is certain is there.
    held = {row["name"] for row in conn.execute("PRAGMA table_info(connections)")}
    if "meta" not in held:
        conn.execute("ALTER TABLE connections ADD COLUMN meta TEXT")
    conn.commit()
    return conn


def remember(
    subject: str,
    platform: str,
    refresh_token: str,
    scopes: list[str],
    path: Path | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Store or replace somebody's credential for one platform.

    Encryption happens here rather than at the call site, so there is no code path that
    can write one of these in the clear by forgetting a step.
    """
    sealed = vault.seal(refresh_token)
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO connections "
            "  (subject, platform, secret, scopes, created_at, updated_at, meta) "
            "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), ?) "
            "ON CONFLICT(subject, platform) DO UPDATE SET "
            "  secret = excluded.secret, scopes = excluded.scopes, "
            "  updated_at = excluded.updated_at, meta = excluded.meta",
            (
                subject.lower(),
                platform,
                sealed,
                " ".join(sorted(scopes)),
                json.dumps(meta or {}),
            ),
        )


def recall(subject: str, platform: str = GOOGLE, path: Path | None = None) -> Connection | None:
    """One person's credential, decrypted, or None if they have not connected it.

    A row whose secret will not decrypt is treated as absent. That happens when the
    encryption key has been rotated, and the honest answer is "not connected" - the
    person signs in again and the row is replaced.
    """
    with connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM connections WHERE subject = ? AND platform = ?",
            (subject.lower(), platform),
        ).fetchone()
    if row is None:
        return None
    secret = vault.unseal(row["secret"])
    if secret is None:
        return None
    return Connection(
        subject=row["subject"],
        platform=row["platform"],
        refresh_token=secret,
        scopes=row["scopes"].split() if row["scopes"] else [],
        updated_at=row["updated_at"],
        meta=_meta(row["meta"]),
    )


def _meta(raw: Any) -> dict[str, Any]:
    """Never let a malformed row take down a query. It holds conveniences, not keys."""
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def touch(subject: str, platform: str = GOOGLE, path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.execute(
            "UPDATE connections SET last_used = datetime('now') WHERE subject = ? AND platform = ?",
            (subject.lower(), platform),
        )


def forget(subject: str, platform: str | None = None, path: Path | None = None) -> int:
    """Drop somebody's stored credentials. Platform None means all of them."""
    with connect(path) as conn:
        if platform:
            cur = conn.execute(
                "DELETE FROM connections WHERE subject = ? AND platform = ?",
                (subject.lower(), platform),
            )
        else:
            cur = conn.execute("DELETE FROM connections WHERE subject = ?", (subject.lower(),))
    return cur.rowcount


def rows(path: Path | None = None) -> list[dict[str, Any]]:
    """Every connection, for reporting. Carries no secret."""
    with connect(path) as conn:
        found = conn.execute(
            "SELECT subject, platform, scopes, created_at, updated_at, last_used, meta "
            "FROM connections ORDER BY subject, platform"
        ).fetchall()
    return [
        {
            "email": r["subject"],
            "platform": r["platform"],
            "scopes": r["scopes"].split() if r["scopes"] else [],
            "connected": (r["created_at"] or "")[:10],
            "last_used": r["last_used"],
            "meta": _meta(r["meta"]),
        }
        for r in found
    ]
