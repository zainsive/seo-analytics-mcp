"""The MCP server instance and its stdio entry point.

Nothing here may write to stdout: on a stdio transport stdout carries JSON-RPC and
a single stray print() corrupts the stream. Logging goes to stderr, always.
"""

from __future__ import annotations

import logging
import os
import sys

from mcp.server import MCPServer

from . import __version__, config
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

WRITE_TOOLS = {"gsc_submit_sitemap": scopes.GSC_WRITE}


def hide_ungranted_write_tools() -> list[str]:
    """A tool the token cannot use is worse than a missing tool: the model keeps
    retrying it. When a token exists and lacks the write scope, unregister the tools
    that need it. With no token at all every tool stays visible, so auth_status and
    the error messages can explain what to do.
    """
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


def main() -> None:
    _setup_logging()
    for name in hide_ungranted_write_tools():
        logging.getLogger("seo_mcp").info(
            "%s is not registered: the stored token lacks its write scope.", name
        )
    mcp.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
