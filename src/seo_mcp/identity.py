"""Who is making this request.

Until now nothing asked. One service account answered for everybody, so the question
had no consequences. With per-person credentials it decides which Google account a
query runs as, and - just as importantly - which cache entries a request may see.

Three kinds of caller, and the difference matters:

  signed in   a person who completed a Google sign-in. Has an email, and possibly
              their own stored credentials.
  anonymous   a request carrying a hand-issued static token. Real and authorised, but
              not a person: nothing to act as, so it falls back to the server's own
              credential.
  none        no request context at all - the CLI, the stdio server, tests.

`caller()` returns the email for the first and None for the other two, which is
deliberate: a static token must never be mistaken for a person, or it would inherit
whichever person last signed in.
"""

from __future__ import annotations

STATIC_PREFIX = "static:"


def caller() -> str | None:
    """The signed-in person's email, or None when the request is not a person's.

    Never raises. Outside a request there is simply no caller, and every path here has
    to work from the CLI as well as from the server.
    """
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token

        token = get_access_token()
    except Exception:
        return None
    if token is None:
        return None
    # Static tokens are given a client_id of "static:<label>" when they are verified,
    # and a subject of that label. They identify a credential, not a human.
    if str(token.client_id or "").startswith(STATIC_PREFIX):
        return None
    subject = (token.subject or "").strip().lower()
    return subject or None


def describe() -> str:
    """A short phrase naming the caller, for log lines and error messages."""
    return caller() or "this server"
