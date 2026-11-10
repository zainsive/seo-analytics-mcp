"""Bearer-token gate for the hosted deployment.

Every request to the MCP endpoint must carry the access token in a header.
Anything else gets 401 and no further processing - the request never reaches a tool,
a credential or Google.

`x-api-key` is the documented name, because that is what client UIs actually permit:
claude.ai allowlists header names on custom connectors and rejects anything else when
the connector is saved, and it reserves `Authorization` for its own OAuth token. Several
equivalent spellings are accepted, since which ones a given dialog offers varies, and a
name the client refuses to send is indistinguishable from a broken server.

The `Bearer ` prefix is optional. `x-api-key` conventionally carries a bare key while
`Authorization` requires the scheme, people paste whatever they were handed, and
accepting either costs nothing: the token must still match exactly. It removes the
single most common setup mistake.

Tokens are issued per person rather than shared, so one leaver is revoked without
rotating everyone. The label on each token is what appears in the logs; the token
itself never is.

Tokens come from the SQLite store in tokens.py. `SEO_MCP_TOKENS` still works and is
checked second, so a deployment whose database is missing or unreadable can still be
brought up from the environment.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
from collections.abc import Callable

from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

TOKENS_VAR = "SEO_MCP_TOKENS"
OPEN_PATHS = ("/healthz",)  # uptime checks only; reveals nothing but a version string
# In preference order; the first is what the docs and error messages name. The list is
# broad on purpose: claude.ai only permits allowlisted header names and the offered set
# differs between accounts, so narrowing this buys nothing and costs a support round
# trip. `authorization` is checked LAST because a client or proxy may put its own OAuth
# token there, which must not shadow the key somebody deliberately configured.
HEADER_NAMES = (
    "x-api-key",
    "x-auth-token",
    "x-apikey",
    "api-key",
    "apikey",
    "x-api-token",
    "x-authorization",
    "authorization",
)
PRIMARY_HEADER = HEADER_NAMES[0]
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


def identify_env(presented: str, tokens: dict[str, str]) -> str | None:
    """Constant-time lookup over environment tokens, which are held in plaintext.

    A plain dict hit would leak length and prefix through timing. The database path
    needs no such care: it matches on a hash, so there is nothing to approximate.
    """
    match = None
    for known, label in tokens.items():
        if hmac.compare_digest(presented, known):
            match = label
    return match


def credential(headers) -> tuple[str | None, str | None]:
    """Pull the token out of whichever accepted header carries it.

    Returns (token, problem); exactly one is set. A problem is a sentence for the 401
    body, so a misconfigured client gets told what is wrong rather than just "no".
    """
    for name in HEADER_NAMES:
        raw = (headers.get(name) or "").strip()
        if not raw:
            continue
        scheme, sep, rest = raw.partition(" ")
        if not sep:
            if raw.lower() == "bearer":
                # Stripping "Bearer " leaves "Bearer", which would otherwise be taken
                # for a bare key and fail with the unhelpful "not recognised".
                return None, f"The {name} header says 'Bearer' but carries no token after it."
            return raw, None  # a bare key, as x-api-key conventionally carries it
        if scheme.lower() == "bearer":
            token = rest.strip()
            if token:
                return token, None
            return None, f"The {name} header says 'Bearer' but carries no token after it."
        return None, (
            f"The {name} header uses the {scheme!r} scheme, which this server does not "
            "accept. Send the token on its own, or prefixed with 'Bearer '."
        )
    return None, None  # nothing was presented at all


def _unauthorised(detail: str) -> JSONResponse:
    return JSONResponse(
        {"error": "unauthorized", "detail": detail},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="seo-analytics-mcp"'},
    )


def make_middleware(resolve: Callable[[str], str | None]):
    """Dispatch for Starlette's BaseHTTPMiddleware.

    `resolve` is called per request rather than consulted from a cached snapshot. That
    is what makes revocation immediate: the alternative is a restart, which disconnects
    everyone to remove one person.
    """

    async def dispatch(request: Request, call_next):
        if request.url.path in OPEN_PATHS:
            return await call_next(request)

        presented, problem = credential(request.headers)
        peer = request.client.host if request.client else "?"
        client = request.headers.get("x-forwarded-for", peer)

        if problem:
            logger.warning("401 malformed credential from %s", client)
            return _unauthorised(problem)
        if not presented:
            logger.warning("401 no credentials from %s %s", client, request.url.path)
            return _unauthorised(
                "This server requires an access token. Send it in an "
                f"'{PRIMARY_HEADER}' header with every request."
            )

        label = resolve(presented)
        if label is None:
            logger.warning("401 unrecognised token from %s", client)
            return _unauthorised("That token is not recognised. Ask for a current one.")

        request.state.token_label = label
        logger.info("%s %s from %s", request.method, request.url.path, label)
        return await call_next(request)

    return dispatch


def generate() -> str:
    return secrets.token_urlsafe(32)


def build_resolver() -> tuple[Callable[[str], str | None], str]:
    """The lookup used by the running server, plus a line describing it for the log.

    Raises SystemExit when no token exists anywhere. Serving openly is never the
    fallback: a hosted deployment that answers everyone is one leaked URL away from
    handing over the analytics, and nothing about it looks broken.
    """
    from . import tokens as store

    env_tokens = parse_tokens()
    minted = store.active_count()

    if not minted and not env_tokens:
        raise SystemExit(
            "Refusing to start: no access token exists, so every request would be "
            "answered without a credential.\n"
            "  Issue one per person:  seo-analytics-mcp token mint alice\n"
            f"  Database:              {store.db_path()}\n"
            f"Alternatively set {TOKENS_VAR} to comma-separated 'label:token' pairs, "
            "though those are stored in plaintext and need a restart to change."
        )

    def resolve(presented: str) -> str | None:
        if label := store.identify(presented):
            return label
        if store.is_known(presented):
            # Issued here, but revoked or expired. Stop: falling through would let the
            # same string sitting in SEO_MCP_TOKENS undo a revocation.
            return None
        return identify_env(presented, env_tokens)

    described = f"{minted} token(s) in {store.db_path()}"
    if env_tokens:
        described += (
            f", plus {len(env_tokens)} from {TOKENS_VAR} "
            f"({', '.join(sorted(set(env_tokens.values())))}) - move these into the "
            "database, where they are hashed rather than held in plaintext"
        )
    return resolve, described
