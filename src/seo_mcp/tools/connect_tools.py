"""The one tool that hands somebody a link to attach their own Mailchimp or LinkedIn.

A tool rather than a page people are sent to, because the moment somebody needs it is
the moment a query just failed inside their AI tool, and the shortest path from there
is the assistant producing the link in the same breath as the error.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError

from ..identity import caller
from ..server import mcp
from ..sso import connections, link, platforms
from . import _base


@mcp.tool(annotations=_base.READ_ONLY)
async def connect_account(platform: Literal["mailchimp", "linkedin"]) -> dict[str, Any]:
    """Get a link to connect your own Mailchimp or LinkedIn to this server.

    Call this when a Mailchimp or LinkedIn tool says the person has not connected that
    account. Show them the url and tell them to open it: they approve it in their own
    browser, and afterwards the data tools read as them. Nothing is connected until
    they do, and the link works once.
    """
    subject = caller()
    if subject is None:
        raise ToolError(
            "Connecting an account needs a signed-in person, and this request did not "
            "come from one. Sign in to the connector with Google first - a credential "
            "issued by hand has nobody to attach an account to."
        )
    try:
        url = link.start(subject, platform)
    except platforms.LinkRefused as exc:
        raise ToolError(str(exc)) from exc

    adapter = platforms.ALL[platform]
    existing = connections.recall(subject, platform)
    return {
        "url": url,
        "platform": adapter.label,
        "connecting_as": subject,
        "already_connected": existing is not None,
        "next_step": (
            f"Open this link in a browser and approve it. It is tied to {subject}, "
            "works once, and expires in a few minutes."
        ),
        "notes": [
            f"{adapter.label} will be read as you afterwards, so you see exactly what "
            f"your own {adapter.label} account is allowed to see.",
            "Re-connecting replaces the stored credential; it never adds a second one.",
        ],
    }
