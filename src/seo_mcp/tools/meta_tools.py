"""auth_status - the tool that exists so the model can tell a confused user what to do."""

from __future__ import annotations

import asyncio
from typing import Any

from .. import config
from ..auth import scopes, store
from ..clients import ga4, gsc
from ..server import mcp
from . import _base


async def _probe(name: str, coro: Any) -> dict[str, Any]:
    try:
        result = await coro
        return {"api": name, "answers": True, "visible": result.total_rows}
    except Exception as exc:  # a probe reports, it never raises
        return {"api": name, "answers": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


@mcp.tool(annotations=_base.READ_ONLY)
async def auth_status() -> dict[str, Any]:
    """Report which profile is active, which scopes its token holds, and which Google
    APIs actually answer. Call this first whenever another tool returns a permission
    error: the response names the exact command the user should run in their terminal.
    """
    profile = config.active_profile()
    held = store.scopes_held(profile)
    out: dict[str, Any] = {
        "profile": profile.name,
        "profile_dir": str(profile.root),
        "authorised": store.has_token(profile),
        "scopes_held": held,
        "can_write": scopes.satisfies(held, {scopes.GSC_WRITE}),
        "defaults": _base.as_rows(
            {
                "gsc_default_site": profile.default_site,
                "ga4_default_property": profile.default_property,
                "indexnow_host": profile.indexnow_host,
                "indexnow_key_configured": bool(profile.indexnow_key),
            }
        ),
        "notes": [],
    }

    if not out["authorised"]:
        out["next_step"] = (
            "No token is stored for this profile. In a terminal, run: "
            "`seo-mcp auth --client-secret ./client_secret.json` (run `seo-mcp doctor` "
            "first if the OAuth client has not been created yet - it prints the Google "
            "Cloud Console steps in order). Restart this MCP client afterwards."
        )
        out["notes"].append(
            "This server cannot open a browser from inside a tool call, so authorisation "
            "has to happen once in a terminal."
        )
        return out

    if warning := store.testing_status_warning(profile):
        out["notes"].append(warning)
    if age := store.token_age_days(profile):
        out["token_age_days"] = round(age, 1)

    try:
        creds = store.load(profile, {scopes.GSC_READ})
    except Exception as exc:
        out["next_step"] = f"The stored token could not be used: {type(exc).__name__}: {exc}"
        return out

    probes = [_probe("searchconsole.googleapis.com", gsc.list_sites(creds))]
    if scopes.satisfies(held, {scopes.GA4_READ}):
        probes.append(_probe("analyticsadmin.googleapis.com", ga4.list_properties(creds)))
    out["apis"] = await asyncio.gather(*probes)

    broken = [p["api"] for p in out["apis"] if not p["answers"]]
    if broken:
        out["next_step"] = (
            f"These APIs did not answer: {', '.join(broken)}. The usual cause is that the "
            "API is not enabled on the Google Cloud project that issued the OAuth client. "
            "Run `seo-mcp doctor` for the specific reason."
        )
    else:
        out["next_step"] = "Everything needed is working. No action required."
    return out
