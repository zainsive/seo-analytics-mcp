"""Bearer-token gate for the hosted deployment.

Every request to the MCP endpoint must carry `Authorization: Bearer <token>`.
Anything else gets 401 and no further processing - the request never reaches a tool,
a credential or Google.

Tokens are issued per person rather than shared, so a leaver is revoked by deleting
one line instead of rotating everyone. The label on each token is what appears in the
logs; the token itself never is.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets

from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

TOKENS_VAR = "SEO_MCP_TOKENS"
OPEN_PATHS = ("/healthz",)  # uptime checks only; reveals nothing but a version string
MIN_TOKEN_LEN = 24


def parse_tokens(raw: str | None = None) -> dict[str, str]:
    """`SEO_MCP_TOKENS=alice:tok1,bob:tok2` -> {token: label}.

    A bare token with no label is accepted and labelled "unnamed", because a deployment
    that cannot start is worse than one with poor log attribution.
    """
    text = (raw if raw is not None else os.environ.get(TOKENS_VAR, "")).strip()
    out: dict[str, str] = {}
    for entry in text.split(","):
        entry = entry.strip()
        if not entry:
            continue
        label, sep, token = entry.partition(":")
        if not sep:
            label, token = "unnamed", label
        label, token = label.strip(), token.strip()
        if not token:
            continue
        if len(token) < MIN_TOKEN_LEN:
            logger.warning(
                "token for %r is only %d characters; use at least %d. Generate one "
                "with: seo-analytics-mcp token <name>",
                label,
                len(token),
                MIN_TOKEN_LEN,
            )
        out[token] = label or "unnamed"
    return out


def identify(presented: str, tokens: dict[str, str]) -> str | None:
    """Constant-time lookup. A plain dict hit would leak length and prefix through timing."""
    match = None
    for known, label in tokens.items():
        if hmac.compare_digest(presented, known):
            match = label
    return match


def _unauthorised(detail: str) -> JSONResponse:
    return JSONResponse(
        {"error": "unauthorized", "detail": detail},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="seo-analytics-mcp"'},
    )


def make_middleware(tokens: dict[str, str]):
    """Dispatch for Starlette's BaseHTTPMiddleware."""

    async def dispatch(request: Request, call_next):
        if request.url.path in OPEN_PATHS:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        scheme, _, presented = header.partition(" ")
        peer = request.client.host if request.client else "?"
        client = request.headers.get("x-forwarded-for", peer)

        if not header:
            logger.warning("401 no credentials from %s %s", client, request.url.path)
            return _unauthorised(
                "This server requires a bearer token. Send the header "
                "'Authorization: Bearer <your token>' with every request."
            )
        if scheme.lower() != "bearer" or not presented.strip():
            logger.warning("401 malformed header from %s", client)
            return _unauthorised("Authorization header must be of the form 'Bearer <your token>'.")

        label = identify(presented.strip(), tokens)
        if label is None:
            logger.warning("401 unrecognised token from %s", client)
            return _unauthorised("That token is not recognised. Ask for a current one.")

        request.state.token_label = label
        logger.info("%s %s from %s", request.method, request.url.path, label)
        return await call_next(request)

    return dispatch


def generate() -> str:
    return secrets.token_urlsafe(32)
