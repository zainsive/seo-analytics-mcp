"""Encryption for the one kind of secret this server has to be able to read back.

Everything else here is stored as a hash: access tokens, authorization codes, the
static tokens. A hash is enough because the server only ever needs to recognise those
values, never reproduce them.

A person's Google refresh token is different. The whole point of holding it is to
present it to Google later, so it has to come back out intact. That makes the database
worth stealing in a way it was not before - one file would otherwise hand over every
teammate's Google access - so the tokens are encrypted with a key that lives outside
it, in the environment.

Fernet rather than anything hand-rolled: it is authenticated encryption with a defined
format, so a tampered row fails loudly instead of decrypting to rubbish.
"""

from __future__ import annotations

import os
from functools import lru_cache

from mcp.server.mcpserver.exceptions import ToolError

KEY_VAR = "SEO_MCP_SECRET_KEY"

_GENERATE = (
    "python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
)
HOW_TO_SET = (
    f"Generate one with:\n  {_GENERATE}\n"
    f"then set {KEY_VAR} in the server's environment. Keep it out of the database and "
    f"out of version control: it is the only thing protecting the stored credentials.\n"
    f"Changing it does not break the server - everyone simply signs in again."
)


def configured() -> bool:
    return bool(os.environ.get(KEY_VAR, "").strip())


@lru_cache(maxsize=1)
def _cipher(key: str):
    from cryptography.fernet import Fernet

    try:
        return Fernet(key.encode())
    except Exception as exc:
        raise ToolError(
            f"{KEY_VAR} is not a valid Fernet key ({exc}). It must be 32 url-safe "
            f"base64-encoded bytes.\n{HOW_TO_SET}"
        ) from exc


def _require_key() -> str:
    key = os.environ.get(KEY_VAR, "").strip()
    if not key:
        raise ToolError(
            "Per-person credentials cannot be stored because no encryption key is "
            f"configured.\n{HOW_TO_SET}"
        )
    return key


def seal(plaintext: str) -> str:
    """Encrypt a secret for storage. Refuses rather than storing it readable."""
    return _cipher(_require_key()).encrypt(plaintext.encode()).decode()


def unseal(ciphertext: str) -> str | None:
    """Decrypt, or None when it cannot be read.

    Returns None rather than raising on a bad token: the usual cause is that the key
    was rotated, and the right response is to ask that person to sign in again, not to
    fail the whole request with a cryptography error.
    """
    from cryptography.fernet import InvalidToken

    try:
        return _cipher(_require_key()).decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ToolError):
        return None
