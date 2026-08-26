"""The only module that writes to the outside world, and the only natively async one.

Host validation happens before the request because IndexNow answers a mismatched
batch with a 422 that says nothing useful. Status codes are never collapsed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from mcp.server.mcpserver.exceptions import ToolError

SHARED_ENDPOINT = "https://api.indexnow.org/indexnow"  # propagates to all participants
KNOWN_ENDPOINTS = {
    "https://api.indexnow.org/indexnow",
    "https://www.bing.com/indexnow",
    "https://searchadvisor.naver.com/indexnow",
    "https://search.seznam.cz/indexnow",
    "https://yandex.com/indexnow",
    "https://indexnow.yep.com/indexnow",
}
MAX_BATCH = 10_000
KEY_RE = re.compile(r"^[A-Za-z0-9-]{8,128}$")
PARTICIPANTS = "Bing, Yandex, Naver, Seznam.cz, Yep and Amazon"

STATUS_MEANING = {
    200: "OK - URLs received",
    202: "Accepted - received, key validation pending",
    400: "Bad request - invalid format",
    403: "Forbidden - key not valid for this host",
    404: "Not found - endpoint or key file missing",
    422: "Unprocessable - URLs do not match the host, or the key does not match the schema",
    429: "Too many requests - rate limited, retry later",
}


@dataclass(frozen=True)
class KeyCheck:
    ok: bool
    detail: str
    scope_prefix: str | None = None


@dataclass(frozen=True)
class SubmitResult:
    endpoint: str
    count: int
    status: int
    meaning: str
    body: str = ""
    urls: list[str] = field(default_factory=list)


async def verify_key(host: str, key: str, key_location: str | None = None) -> KeyCheck:
    """Gate for every submission. Asserts 200, a text content type, and an exact match."""
    if not KEY_RE.match(key or ""):
        return KeyCheck(
            False,
            f"Key must be 8-128 characters of a-z, A-Z, 0-9 or '-'; got {len(key or '')} "
            "characters. Generate a fresh one and host it at "
            f"https://{host}/<key>.txt containing exactly that key.",
        )
    url = key_location or f"https://{host}/{key}.txt"
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        try:
            r = await client.get(url)
        except httpx.HTTPError as exc:
            return KeyCheck(False, f"Could not fetch {url}: {exc}")
    if r.status_code != 200:
        return KeyCheck(False, f"{url} returned {r.status_code}; it must return 200.")
    content_type = r.headers.get("content-type", "")
    if content_type and not content_type.split(";")[0].strip().startswith("text/"):
        return KeyCheck(
            False,
            f"{url} is served as {content_type!r}; it must be served as text/plain.",
        )
    if r.text.strip() != key:
        return KeyCheck(False, f"{url} does not contain the key exactly (got {r.text[:40]!r}).")
    return KeyCheck(True, f"Verified at {url}.", scope_prefix=url.rsplit("/", 1)[0])


def validate_batch(host: str, urls: list[str]) -> list[str]:
    """Reject a batch before spending a request on it. Returns the cleaned list."""
    if not urls:
        raise ToolError("No URLs were given to submit.")
    cleaned = [u.strip() for u in urls if u and u.strip()]
    bad_scheme = [u for u in cleaned if urlsplit(u).scheme not in ("http", "https")]
    if bad_scheme:
        raise ToolError(
            f"{len(bad_scheme)} URL(s) are not absolute http(s) URLs, e.g. {bad_scheme[0]!r}. "
            "IndexNow needs full URLs including the scheme."
        )
    bad_host = [u for u in cleaned if urlsplit(u).hostname != host]
    if bad_host:
        raise ToolError(
            f"{len(bad_host)} URL(s) are not on host '{host}', e.g. {bad_host[0]}. "
            "IndexNow rejects a batch whose URLs do not match the declared host (422)."
        )
    duplicates = len(cleaned) - len(set(cleaned))
    if duplicates:
        cleaned = list(dict.fromkeys(cleaned))
    return cleaned


async def submit(
    host: str,
    key: str,
    urls: list[str],
    *,
    endpoint: str = SHARED_ENDPOINT,
    key_location: str | None = None,
) -> list[SubmitResult]:
    """Never flatten status codes. 200, 202, 400, 403, 422 and 429 all mean different
    things and the caller must be able to tell them apart."""
    cleaned = validate_batch(host, urls)
    out: list[SubmitResult] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(cleaned), MAX_BATCH):
            chunk = cleaned[i : i + MAX_BATCH]
            body: dict[str, object] = {"host": host, "key": key, "urlList": chunk}
            if key_location:
                body["keyLocation"] = key_location
            try:
                r = await client.post(endpoint, json=body)
            except httpx.HTTPError as exc:
                raise ToolError(
                    f"Could not reach the IndexNow endpoint {endpoint}: {exc}. "
                    "Nothing was submitted."
                ) from exc
            out.append(
                SubmitResult(
                    endpoint=endpoint,
                    count=len(chunk),
                    status=r.status_code,
                    meaning=STATUS_MEANING.get(r.status_code, "unknown"),
                    body=r.text[:200],
                    urls=chunk[:5],
                )
            )
    return out
