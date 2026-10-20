"""The MCP server instance and its stdio entry point.

Nothing here may write to stdout: on a stdio transport stdout carries JSON-RPC and
a single stray print() corrupts the stream. Logging goes to stderr, always.
"""

from __future__ import annotations

import logging
import os
import sys

from mcp.server import MCPServer

from . import __version__, config, credentials
from .auth import scopes, store

INSTRUCTIONS = (
    "Search Console, GA4 and IndexNow for the user's own properties.\n"
    "Call auth_status first if any tool returns a permission error - its message names "
    "the exact command the user should run.\n"
    "Resolve identifiers with gsc_list_sites / ga4_list_properties before querying: a "
    "Search Console domain property is written 'sc-domain:example.com', a GA4 property "
    "as 'properties/123456789'.\n"
    "Dates accept YYYY-MM-DD, 'today', 'yesterday' or 'NdaysAgo'; every response echoes "
    "the absolute range it actually used.\n"
    "Rows carrying the 'query' dimension are anonymised by Google and do not sum to "
    "property totals - never compute shares from them.\n"
    "Read the 'notes' field of every response and pass its caveats on to the user.\n"
    "indexnow_submit and gsc_submit_sitemap act on the live web. Show the dry run to the "
    "user and get their agreement before confirming either one."
)


LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def log_level() -> str:
    """SEO_MCP_LOG_LEVEL governs both our logging and the SDK's, so a user who turns
    logging down gets a quiet stderr rather than a partly quiet one."""
    requested = os.environ.get("SEO_MCP_LOG_LEVEL", "WARNING").upper()
    return requested if requested in LOG_LEVELS else "WARNING"


def _setup_logging() -> None:
    logging.basicConfig(
        stream=sys.stderr,  # never stdout: that is the JSON-RPC channel
        level=log_level(),
        format="%(levelname)s %(name)s: %(message)s",
    )


mcp = MCPServer(name="seo", version=__version__, instructions=INSTRUCTIONS, log_level=log_level())

# Tool modules register themselves on import. This has to happen after `mcp` exists,
# which is why the imports sit at the bottom of the module.
from .tools import composite as _composite  # noqa: E402,F401
from .tools import ga4_tools as _ga4_tools  # noqa: E402,F401
from .tools import gsc_tools as _gsc_tools  # noqa: E402,F401
from .tools import indexnow_tools as _indexnow_tools  # noqa: E402,F401
from .tools import meta_tools as _meta_tools  # noqa: E402,F401


@mcp.custom_route("/healthz", ["GET"])
async def healthz(_request):
    """Liveness for the hosted deployment. Deliberately says nothing about the
    credential - an unauthenticated probe should not reveal which account is wired up."""
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ok", "version": __version__})


WRITE_TOOLS = {"gsc_submit_sitemap": scopes.GSC_WRITE}


def hide_ungranted_write_tools() -> list[str]:
    """A tool the credential cannot use is worse than a missing tool: the model keeps
    retrying it. When the active credential lacks the write scope, unregister the tools
    that need it. With no credential at all every tool stays visible, so auth_status and
    the error messages can explain what to do.

    Service-account mode is read-only by policy for this deployment: a hosted server
    shared with a team should never be able to write to the live web.
    """
    if credentials.mode() == "service_account":
        held: list[str] = []
    else:
        profile = config.active_profile()
        if not store.has_token(profile):
            return []
        held = store.scopes_held(profile)
    removed = []
    for name, needed in WRITE_TOOLS.items():
        if not scopes.satisfies(held, {needed}):
            try:
                mcp.remove_tool(name)
                removed.append(name)
            except Exception:  # pragma: no cover - already absent
                pass
    return removed


ENDPOINT_PATH_VAR = "SEO_MCP_ENDPOINT_PATH"
DEFAULT_ENDPOINT_PATH = "/mcp"


def endpoint_path() -> str:
    """Where the HTTP endpoint listens.

    Deployed, this should be an unguessable path - it is the only thing standing
    between a stranger with the URL and your analytics. Anything else is a config
    error worth refusing rather than silently serving on a guessable route.
    """
    raw = (os.environ.get(ENDPOINT_PATH_VAR) or DEFAULT_ENDPOINT_PATH).strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw.rstrip("/") or DEFAULT_ENDPOINT_PATH


ALLOWED_HOSTS_VAR = "SEO_MCP_ALLOWED_HOSTS"


def transport_security():
    """DNS-rebinding protection, configured for whatever hostname fronts this server.

    The SDK enables this by default with an empty allowlist, so behind a reverse proxy
    every request arrives with Host: <your domain> and is answered with
    421 "Invalid Host header" - a failure that looks like a proxy misconfiguration and
    is not. Set SEO_MCP_ALLOWED_HOSTS to the public hostname(s) when deploying.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    raw = os.environ.get(ALLOWED_HOSTS_VAR, "").strip()
    if not raw:
        return None  # localhost only, which is right for a local or tunnelled test
    hosts = [h.strip() for h in raw.split(",") if h.strip()]
    origins = [f"{scheme}://{h}" for h in hosts for scheme in ("https", "http") if "*" not in h]
    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins)


def _register_write_tool_state() -> None:
    for name in hide_ungranted_write_tools():
        logging.getLogger("seo_mcp").info(
            "%s is not registered: the active credential lacks its write scope.", name
        )


def main() -> None:
    _setup_logging()
    _register_write_tool_state()
    mcp.run(transport="stdio")


def serve_http(host: str = "127.0.0.1", port: int = 8080) -> None:
    """Run the hosted deployment: one credential, many connecting clients.

    stateless_http=True so instances can scale horizontally behind a load balancer -
    a session pinned to one container would break the moment it scaled.
    """
    import uvicorn

    # Nobody can read a file inside this container, so summaries must be self-sufficient.
    os.environ.setdefault("SEO_MCP_EXPORTS", "0")
    _setup_logging()
    _register_write_tool_state()
    path = endpoint_path()
    log = logging.getLogger("seo_mcp")
    log.warning("serving MCP over HTTP on %s:%s%s", host, port, path)
    if path == DEFAULT_ENDPOINT_PATH:
        log.warning(
            "endpoint is the default %s. Set %s to a long random path before exposing "
            "this to the internet - the path is the access control.",
            DEFAULT_ENDPOINT_PATH,
            ENDPOINT_PATH_VAR,
        )
    security = transport_security()
    if security is None:
        log.warning(
            "%s is unset: only localhost Host headers are accepted. Behind a reverse "
            "proxy every request will fail with 421 Invalid Host header - set it to the "
            "public hostname, e.g. %s=mcp.example.com",
            ALLOWED_HOSTS_VAR,
            ALLOWED_HOSTS_VAR,
        )
    else:
        log.warning("accepting Host headers: %s", ", ".join(security.allowed_hosts))
    app = mcp.streamable_http_app(
        streamable_http_path=path, stateless_http=True, transport_security=security
    )
    uvicorn.run(app, host=host, port=port, log_level=log_level().lower(), access_log=False)


if __name__ == "__main__":  # pragma: no cover
    main()
