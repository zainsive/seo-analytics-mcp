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


def _google_sign_in():
    """(provider, settings), or (None, None) when Google sign-in is off.

    Resolved at import because the SDK takes both at construction. Anything wrong with
    the configuration leaves sign-in off and the server running on static tokens, which
    is the failure a deployment can recover from.
    """
    try:
        from .sso.provider import GoogleFederatedProvider, settings

        if (cfg := settings()) is None:
            return None, None
        return GoogleFederatedProvider(), cfg
    except Exception:  # pragma: no cover - import-time safety net
        logging.getLogger("seo_mcp").exception("Google sign-in failed to load; staying off")
        return None, None


_SSO_PROVIDER, _SSO_SETTINGS = _google_sign_in()

mcp = MCPServer(
    name="seo",
    version=__version__,
    instructions=INSTRUCTIONS,
    log_level=log_level(),
    auth_server_provider=_SSO_PROVIDER,
    auth=_SSO_SETTINGS,
)


def sign_in_enabled() -> bool:
    return _SSO_PROVIDER is not None


# Tool modules register themselves on import. This has to happen after `mcp` exists,
# which is why the imports sit at the bottom of the module.
from .tools import composite as _composite  # noqa: E402,F401
from .tools import ga4_tools as _ga4_tools  # noqa: E402,F401
from .tools import gsc_tools as _gsc_tools  # noqa: E402,F401
from .tools import indexnow_tools as _indexnow_tools  # noqa: E402,F401
from .tools import linkedin_tools as _linkedin_tools  # noqa: E402,F401
from .tools import mailchimp_tools as _mailchimp_tools  # noqa: E402,F401
from .tools import meta_tools as _meta_tools  # noqa: E402,F401
from .tools import youtube_tools as _youtube_tools  # noqa: E402,F401

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
body{{margin:0;background:#fff;color:#1A1D1D;font:400 18px/1.6 Georgia,"Times New Roman",serif;
display:flex;min-height:100vh;align-items:center;justify-content:center;padding:24px}}
.box{{max-width:30rem}}h1{{font-size:1.5rem;margin:0 0 14px;letter-spacing:-.01em}}
p{{margin:0 0 12px;color:#555C5C}}.m{{font:400 14px/1.6 ui-monospace,Menlo,monospace;color:#8A9191}}
</style></head><body><div class="box"><h1>{title}</h1>{body}</div></body></html>"""


@mcp.custom_route("/auth/google/callback", ["GET"])
async def google_callback(request):
    """Where Google returns the person after they sign in.

    A human is watching this page, so failures are rendered here rather than bounced
    back to the AI client, where the reason would be invisible.
    """
    from starlette.responses import HTMLResponse, RedirectResponse

    from .sso import google as sso_google

    log = logging.getLogger("seo_mcp")

    def refuse(message: str, status: int = 400):
        return HTMLResponse(
            _PAGE.format(
                title="Sign-in refused",
                body=f"<p>{message}</p><p class='m'>You can close this tab.</p>",
            ),
            status_code=status,
        )

    if error := request.query_params.get("error"):
        log.warning("google sign-in returned %s", error)
        return refuse(
            "Google did not complete the sign-in. If you chose Cancel, start again "
            "from your AI tool."
        )
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        return refuse("That link is incomplete. Start the sign-in again from your AI tool.")
    if _SSO_PROVIDER is None:  # pragma: no cover - route only reachable when enabled
        return refuse("Google sign-in is not enabled on this server.", status=404)

    try:
        destination = await _SSO_PROVIDER.complete_login(state, code)
    except sso_google.SignInRefused as exc:
        log.warning("sign-in refused: %s", exc)
        return refuse(str(exc), status=403)
    except Exception:
        log.exception("sign-in failed")
        return refuse("Something went wrong completing the sign-in. Please try again.", 500)
    return RedirectResponse(destination, status_code=302)


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
    from starlette.middleware.base import BaseHTTPMiddleware

    from .bearer import AliasHeaderMiddleware, build_resolver, make_middleware

    if sign_in_enabled():
        from .sso import google as sso_google
        from .sso import store as sso_store

        log.warning(
            "Google sign-in is ON: %s may connect. Callback %s",
            ", ".join(sso_google.allowed_domains() or sso_google.allowed_emails()),
            sso_google.redirect_uri(),
        )
        log.warning(
            "%d client(s) registered; static tokens still accepted alongside sign-in",
            sso_store.client_count(),
        )
        resolve, token_summary = None, None
    else:
        # Refuses to start when no token exists anywhere, rather than serving openly.
        resolve, token_summary = build_resolver()

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
    if sign_in_enabled():
        # The SDK already guards the MCP endpoint, and the provider's
        # load_access_token accepts the static tokens too. All that is missing is
        # getting a credential sent as x-api-key into the header the SDK reads.
        app.add_middleware(AliasHeaderMiddleware)
    else:
        app.add_middleware(BaseHTTPMiddleware, dispatch=make_middleware(resolve))
        log.warning("bearer token required on every request; %s", token_summary)
    uvicorn.run(app, host=host, port=port, log_level=log_level().lower(), access_log=False)


if __name__ == "__main__":  # pragma: no cover
    main()
