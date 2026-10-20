"""auth_status - the tool that exists so the model can tell a confused user what to do.

The only tool that knows which credential mode is active, because it is the only one
whose whole job is explaining the deployment to whoever is asking.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .. import config, credentials
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
    """Report how this server is authenticated and which Google APIs actually answer.
    Call this first whenever another tool returns a permission error: the response names
    the exact fix, which differs depending on whether this is a local install or a
    hosted deployment.
    """
    profile = config.active_profile()
    identity = credentials.describe()
    out: dict[str, Any] = {
        **identity,
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

    if identity["mode"] == "user":
        out["profile_dir"] = str(profile.root)
        out["can_write"] = scopes.satisfies(identity["scopes_held"], {scopes.GSC_WRITE})
        if not identity["authorised"]:
            out["next_step"] = (
                "No token is stored for this profile. In a terminal, run: "
                "`seo-analytics-mcp auth --client-secret ./client_secret.json` (run "
                "`seo-analytics-mcp doctor` first if the OAuth client has not been "
                "created yet - it prints the Google Cloud Console steps in order). "
                "Restart this MCP client afterwards."
            )
            out["notes"].append(
                "This server cannot open a browser from inside a tool call, so "
                "authorisation has to happen once in a terminal."
            )
            return out
        if warning := store.testing_status_warning(profile):
            out["notes"].append(warning)
        if age := store.token_age_days(profile):
            out["token_age_days"] = round(age, 1)
    else:
        out["notes"].append(identity.pop("note"))
        out["can_write"] = False

    try:
        creds = credentials.for_scopes({scopes.GSC_READ})
    except Exception as exc:
        out["next_step"] = f"The credential could not be used: {type(exc).__name__}: {exc}"
        return out

    probes = [_probe("searchconsole.googleapis.com", gsc.list_sites(creds))]
    if identity["mode"] == "service_account" or scopes.satisfies(
        identity.get("scopes_held"), {scopes.GA4_READ}
    ):
        try:
            ga_creds = credentials.for_scopes({scopes.GA4_READ})
            probes.append(_probe("analyticsadmin.googleapis.com", ga4.list_properties(ga_creds)))
        except Exception:  # reported by the GSC probe's own failure if credentials are bad
            pass
    out["apis"] = await asyncio.gather(*probes)

    broken = [p["api"] for p in out["apis"] if not p["answers"]]
    # An API that answers but exposes nothing is the normal failure for a service
    # account, and it is the whole reason to call this tool. Reporting "no action
    # required" here sends the operator to debug the wrong layer entirely.
    empty = [p["api"] for p in out["apis"] if p["answers"] and not p.get("visible")]
    if not broken and empty:
        who = identity.get("service_account_email") or "the authorised account"
        out["next_step"] = (
            f"Authentication works and the APIs answer, but {who} can see no properties "
            f"on {', '.join(empty)}. Access is granted per property and is a separate "
            "step: add that identity in Search Console under Settings > Users and "
            "permissions (Full, if URL inspection is needed), and in GA4 under Admin > "
            "Property access management (Viewer). It applies within a minute, with no "
            "restart or redeploy."
        )
        out["notes"].append(
            "Nothing will return data until that grant exists - every query answers 403."
        )
    elif not broken:
        out["next_step"] = "Everything needed is working. No action required."
    elif identity["mode"] == "service_account":
        email = identity.get("service_account_email")
        out["next_step"] = (
            f"These APIs did not answer: {', '.join(broken)}. Either the API is not "
            "enabled on the service account's Google Cloud project, or "
            f"{email} has not been granted access to the property. Add it under Search "
            "Console > Settings > Users and permissions, and GA4 > Admin > Property "
            "access management."
        )
    else:
        out["next_step"] = (
            f"These APIs did not answer: {', '.join(broken)}. The usual cause is that the "
            "API is not enabled on the Google Cloud project that issued the OAuth client. "
            "Run `seo-analytics-mcp doctor` for the specific reason."
        )
    return out
