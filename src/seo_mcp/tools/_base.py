"""The @google_tool decorator: one place owns error translation.

Without this, a raw exception reaches the model as the bare string
"Error executing tool <name>" and every carefully written message in errors.py
is thrown away.
"""

from __future__ import annotations

import functools
import sys
from collections.abc import Callable, Mapping
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .. import config
from ..errors import Ctx, translate

# Client-visible intent. A client that gates destructive tools behind its own
# approval UI needs these; a `confirm` argument alone is model-controlled and
# proves nothing about human intent.
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITES_LOCALLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)

_NEVER_IN_CACHE_KEY = frozenset({"creds", "profile", "key", "self", "cls", "ctx"})


def locals_without(*extra: str) -> dict[str, Any]:
    """The calling tool's arguments, minus anything that must not shape a cache key.

    Credentials are excluded because they are secret; the profile is excluded
    because cache.key() adds it separately.
    """
    frame = sys._getframe(1)
    skip = _NEVER_IN_CACHE_KEY | set(extra)
    return {k: v for k, v in frame.f_locals.items() if k not in skip and not k.startswith("_")}


def google_tool(*, api: str, scope: str | None = None) -> Callable[[Callable], Callable]:
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(*a: Any, **kw: Any) -> Any:
            try:
                profile_name = config.active_profile().name
            except ToolError:  # an invalid SEO_MCP_PROFILE must still report itself
                profile_name = "?"
            ctx = Ctx(
                profile=profile_name,
                api=api,
                scope_needed=scope,
                site_url=kw.get("site_url"),
                property_id=kw.get("property_id"),
            )
            try:
                return await fn(*a, **kw)
            except ToolError:
                raise  # already actionable, do not re-wrap
            except Exception as exc:
                raise translate(exc, ctx) from exc

        return wrapper

    return deco


def require(value: str | None, *, what: str, env_var: str, lister: str) -> str:
    """Resolve an identifier that has a configurable default, or say exactly how to set it."""
    if value:
        return value
    raise ToolError(
        f"No {what} was given and no default is configured. Pass it explicitly, or set "
        f"{env_var} in the server's env block in your MCP client config. Call {lister} "
        "to see what this account can access."
    )


def as_rows(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Drop empty values so a single-record response stays small."""
    return {k: v for k, v in mapping.items() if v not in (None, "", [], {})}
