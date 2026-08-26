"""Token persistence: read and refresh only. Never prompts, never opens a browser.

This module is reachable from the server. auth/flow.py is not - see RULE 2.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from ..config import Profile
from ..errors import AuthMissing, ScopeMissing, TokenCorrupt
from . import scopes as scope_sets

TESTING_TOKEN_DAYS = 7


def _write_private(path: Path, payload: dict[str, Any]) -> None:
    """0600 from the moment it exists: mkstemp, fchmod, atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tok-")
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        if not hasattr(os, "fchmod"):  # Windows: no fchmod, and ACLs ignore the mode
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save(profile: Profile, creds: Credentials, *, granted_at: float | None = None) -> None:
    payload = json.loads(creds.to_json())
    payload["_seo_mcp"] = {"granted_at": granted_at or time.time(), "version": 1}
    _write_private(profile.token_path, payload)


def raw_token(profile: Profile) -> dict[str, Any] | None:
    try:
        return json.loads(profile.token_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def scopes_held(profile: Profile) -> list[str]:
    raw = raw_token(profile) or {}
    held = raw.get("scopes") or []
    return sorted(str(s) for s in held)


def has_token(profile: Profile) -> bool:
    return profile.token_path.exists()


def load(profile: Profile, required_scopes: set[str]) -> Credentials:
    if not profile.token_path.exists():
        raise AuthMissing(profile.name)
    try:
        raw = json.loads(profile.token_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TokenCorrupt(str(profile.token_path), str(exc)) from exc
    # Scopes are checked from the payload first, so a missing grant is reported as a
    # missing grant even when the rest of the file is unusable.
    if not scope_sets.satisfies(raw.get("scopes"), required_scopes):
        raise ScopeMissing(scope_sets.missing(raw.get("scopes"), required_scopes), profile.name)
    try:
        creds = Credentials.from_authorized_user_info(raw)
    except ValueError as exc:
        raise TokenCorrupt(str(profile.token_path), str(exc)) from exc
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())  # RefreshError bubbles to errors.translate
        save(profile, creds, granted_at=(raw.get("_seo_mcp") or {}).get("granted_at"))
    return creds


def token_age_days(profile: Profile) -> float | None:
    raw = raw_token(profile)
    if not raw:
        return None
    granted = (raw.get("_seo_mcp") or {}).get("granted_at")
    if not isinstance(granted, (int, float)):
        try:
            granted = profile.token_path.stat().st_mtime
        except OSError:  # pragma: no cover
            return None
    return max(0.0, (time.time() - float(granted)) / 86400)


def testing_status_warning(profile: Profile) -> str | None:
    """Heuristic, and honest about it: publishing status is not readable via any API.
    A token that has survived past day 7 proves the app was published."""
    age = token_age_days(profile)
    if age is None or age >= TESTING_TOKEN_DAYS:
        return None
    return (
        f"Token was granted {age:.1f} days ago. If your OAuth app is still in 'Testing' "
        f"publishing status it will stop working at day {TESTING_TOKEN_DAYS}. Press "
        "'Publish app' on the OAuth consent screen to make it permanent."
    )
