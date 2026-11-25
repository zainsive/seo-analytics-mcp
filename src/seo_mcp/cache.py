"""gzip-json TTL cache on disk, keyed by caller + profile + method + params.

mtime is the clock.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from . import identity
from .config import Profile
from .shaping import QueryResult

TTL_DISCOVERY = 24 * 3600  # gsc_list_sites, ga4_list_properties
TTL_GSC = 6 * 3600  # search analytics - GSC barely moves intraday
TTL_GSC_LIVE = 15 * 60  # data_state="all" queries: fresh data, short TTL
TTL_INSPECT = 60 * 60  # URL inspection - 2,000/day per site, worth caching hard
TTL_GA4 = 3 * 3600
# IndexNow is never cached. Submissions are not idempotent from the user's point of view.

MAX_CACHE_FILES = 400


def key(profile: Profile, method: str, params: Mapping[str, Any]) -> str:
    """The cache key.

    The profile is in it so two Google accounts cannot collide on one machine. The
    CALLER is in it for a sharper reason: once queries run as whoever is signed in,
    two people asking the same question are asking two different questions, and
    answering the second from the first one's cache would hand them a view of data
    they may not be allowed to see. A shared key would turn the cache into a way
    around the permissions this whole arrangement exists to honour.
    """
    payload = json.dumps(
        {
            "who": identity.caller() or "",
            "p": profile.name,
            "m": method,
            "a": {k: v for k, v in sorted(params.items()) if v is not None},
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _write(path: Path, result: QueryResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_bytes(gzip.compress(json.dumps(result.to_dict(), default=str).encode()))
        tmp.replace(path)
    except OSError:  # a cache that cannot write is not an error the user should see
        tmp.unlink(missing_ok=True)


def _prune(directory: Path) -> None:
    try:
        files = sorted(directory.glob("*.json.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:  # pragma: no cover
        return
    for stale in files[MAX_CACHE_FILES:]:
        stale.unlink(missing_ok=True)


async def get_or_set(
    profile: Profile,
    k: str,
    *,
    ttl: int,
    producer: Callable[[], Awaitable[QueryResult]],
) -> QueryResult:
    """gzip-json on disk, mtime as the clock. No index, no locking: a torn read just
    misses and re-fetches, which is the correct failure for a cache."""
    path = profile.cache_dir / f"{k}.json.gz"
    if path.exists() and (time.time() - path.stat().st_mtime) < ttl:
        try:
            return QueryResult.from_dict(json.loads(gzip.decompress(path.read_bytes())))
        except Exception:
            path.unlink(missing_ok=True)
    result = await producer()
    _write(path, result)
    _prune(profile.cache_dir)
    return result


def size_bytes(profile: Profile) -> int:
    try:
        return sum(p.stat().st_size for p in profile.cache_dir.glob("*.json.gz"))
    except OSError:  # pragma: no cover
        return 0


def clear(profile: Profile) -> int:
    removed = 0
    for p in profile.cache_dir.glob("*.json.gz"):
        p.unlink(missing_ok=True)
        removed += 1
    return removed
