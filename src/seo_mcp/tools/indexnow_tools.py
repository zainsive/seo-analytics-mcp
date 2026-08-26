"""IndexNow tools - the only ones that change the outside world.

Strangers will run this against real sites, so every submission is treated as
destructive by default: a dry run first, an unforgeable token from that dry run, and
an explicit confirm. `confirm` alone is a model-controlled argument and proves nothing
about human intent; the token proves the dry run actually happened and that the URL
set has not changed since the user saw it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import config
from ..clients import indexnow
from ..server import mcp
from . import _base


def _host(host: str | None) -> str:
    return _base.require(
        host or config.active_profile().indexnow_host,
        what="host",
        env_var="INDEXNOW_HOST",
        lister="auth_status",
    )


def _key() -> str:
    key = config.active_profile().indexnow_key
    if not key:
        raise ToolError(
            "No IndexNow key is configured. Generate a key of 8-128 characters "
            "(a-z, A-Z, 0-9, '-'), host it at https://<your-host>/<key>.txt as plain "
            "text containing exactly that key, then set INDEXNOW_KEY and INDEXNOW_HOST "
            "in the server's env block in your MCP client config. The key is never "
            "passed as a tool argument - tool arguments are visible in the transcript."
        )
    return key


def _batch_token(host: str, urls: list[str], endpoint: str) -> str:
    """Binds a confirmation to one exact URL set. Change a URL and the token no longer
    matches, so a re-planned batch cannot ride an old approval."""
    payload = json.dumps([host, endpoint, sorted(urls)], sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


@mcp.tool(annotations=_base.READ_ONLY)
async def indexnow_verify_key(
    host: str | None = None, key_location: str | None = None
) -> dict[str, Any]:
    """Check that this host's IndexNow key file is correctly published: reachable,
    HTTP 200, served as text, and containing exactly the configured key. Every
    submission runs this check itself and aborts if it fails, so use this tool to
    diagnose a rejected submission. The key is read from configuration, never passed in.
    """
    host = _host(host)
    check = await indexnow.verify_key(host, _key(), key_location)
    return {
        "host": host,
        "verified": check.ok,
        "detail": check.detail,
        "key_location": key_location or f"https://{host}/<key>.txt",
        "note": (
            "IndexNow notifies "
            f"{indexnow.PARTICIPANTS}. Google does not participate in IndexNow, so this "
            "does not affect Google indexing."
        ),
    }


@mcp.tool(annotations=_base.DESTRUCTIVE)
async def indexnow_submit(
    urls: list[str],
    host: str | None = None,
    endpoint: str = indexnow.SHARED_ENDPOINT,
    confirm: bool = False,
    submission_token: str | None = None,
) -> dict[str, Any]:
    """Submit URLs to IndexNow so participating search engines recrawl them. THIS ACTS
    ON THE LIVE WEB and cannot be undone.

    Called without confirm it is a DRY RUN: it validates the key file and the URL list
    and returns a submission_token. Show that dry run to the user, then call again with
    confirm=true AND the exact submission_token you were given. The token is bound to
    that precise URL list, so if the list changes you must dry-run again.

    IndexNow notifies Bing, Yandex, Naver, Seznam.cz, Yep and Amazon. Google does NOT
    participate - this will not speed up Google indexing. Per-endpoint HTTP status is
    returned verbatim, because 200, 202, 400, 403, 422 and 429 all mean different things.
    """
    host = _host(host)
    key = _key()
    if endpoint not in indexnow.KNOWN_ENDPOINTS:
        raise ToolError(
            f"{endpoint!r} is not a known IndexNow endpoint. Use the shared endpoint "
            f"{indexnow.SHARED_ENDPOINT}, which propagates to every participant, or one "
            f"of: {', '.join(sorted(indexnow.KNOWN_ENDPOINTS))}."
        )
    cleaned = indexnow.validate_batch(host, urls)
    check = await indexnow.verify_key(host, key, None)
    token = _batch_token(host, cleaned, endpoint)
    common = {
        "host": host,
        "endpoint": endpoint,
        "url_count": len(cleaned),
        "urls_preview": cleaned[:10],
        "key_verified": check.ok,
        "key_detail": check.detail,
        "participants": indexnow.PARTICIPANTS,
        "google_note": "Google does not participate in IndexNow.",
    }

    if not check.ok:
        raise ToolError(
            f"IndexNow key verification failed, so nothing was submitted: {check.detail} "
            "Publish the key file and run indexnow_verify_key again."
        )

    if not confirm:
        return {
            **common,
            "dry_run": True,
            "submission_token": token,
            "next_step": (
                "Nothing was submitted. Show this list to the user. If they agree, call "
                f"indexnow_submit again with confirm=true and submission_token='{token}' "
                "and the identical urls list."
            ),
        }

    if submission_token != token:
        raise ToolError(
            "confirm=true requires the submission_token from a dry run of this exact URL "
            f"list, and the token given does not match (expected one derived from these "
            f"{len(cleaned)} URLs). Call indexnow_submit without confirm first, show the "
            "result to the user, and pass back the token it returns unchanged."
        )

    results = await indexnow.submit(host, key, cleaned, endpoint=endpoint)
    return {
        **common,
        "dry_run": False,
        "submitted": True,
        "results": [
            {
                "endpoint": r.endpoint,
                "urls_sent": r.count,
                "http_status": r.status,
                "meaning": r.meaning,
                "response_body": r.body,
            }
            for r in results
        ],
        "note": (
            "A 200 or 202 means the URLs were received, not that they were indexed. "
            "Each engine decides independently whether and when to crawl."
        ),
    }
