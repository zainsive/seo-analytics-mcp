"""Starting and finishing the hand-over of a second account.

The security of this rests on one thing: the link somebody clicks must only ever be
able to attach an account to the person who asked for it. So the subject is written
into the parked state while they are authenticated over MCP, and the callback reads it
back from there. Nothing the browser sends decides whose account this becomes - the
callback is an unauthenticated URL, and if it took the subject from a query parameter
then anybody could attach their own Mailchimp to somebody else's email, or worse,
attach somebody else's to themselves.

The state is single-use and short-lived, which is what the Google flow already relies
on, so the same store backs both.
"""

from __future__ import annotations

from .. import config
from . import connections, google, platforms, store

CALLBACK_PATH = "/auth/connect/callback"

KIND = "platform-link"


def redirect_uri() -> str:
    """Where the platform sends the browser back. One path serves every platform.

    Each platform still has to have this registered on its own app, and they differ in
    how fussy they are about it, so it is printed at startup rather than left to be
    guessed from the docs.
    """
    return f"{google.public_url()}{CALLBACK_PATH}"


def start(subject: str, platform: str) -> str:
    """The URL to send the person to, for them to approve the hand-over."""
    adapter = platforms.named(platform)
    if not adapter.configured():
        raise platforms.LinkRefused(adapter.setup_hint())
    state = store.start_login(
        {"kind": KIND, "platform": adapter.platform, "subject": subject.lower()}
    )
    return adapter.authorization_url(state, redirect_uri())


async def finish(state: str, code: str) -> str:
    """Claim the parked request and store the credential. Returns a line for the page."""
    pending = store.finish_login(state)
    if pending is None or pending.get("kind") != KIND:
        raise platforms.LinkRefused(
            "This link has already been used or has expired. Start again from your AI tool."
        )
    adapter = platforms.named(str(pending["platform"]))
    subject = str(pending["subject"])

    linked = await adapter.exchange(code, redirect_uri())
    connections.remember(
        subject,
        adapter.platform,
        linked.token,
        list(linked.scopes),
        meta=linked.meta,
    )
    return f"{adapter.label} is now connected to {subject}."


def connected(subject: str) -> list[str]:
    """Which platforms this person has attached. Used to tell them what is left."""
    return [
        row["platform"]
        for row in connections.rows()
        if row["email"] == subject.lower() and row["platform"] != connections.GOOGLE
    ]


def available() -> list[str]:
    """Platforms this server could connect, if somebody asked."""
    return sorted(name for name, adapter in platforms.ALL.items() if adapter.configured())


def enabled() -> bool:
    """Whether the connect flow can work at all: it needs a public URL to come back to."""
    return bool(config.active_profile().public_url) and bool(available())
