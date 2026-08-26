"""Profiles, platform-correct paths, env precedence.

Precedence, highest first: explicit tool argument > env var > profile config.json > built-in.
The server never reads a credential path from the client config - only a profile name.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

APP = "seo-mcp"
PROFILE_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def config_root() -> Path:
    """XDG on Linux, ~/Library/Application Support on macOS, %APPDATA% on Windows."""
    if env := os.environ.get("SEO_MCP_HOME"):
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home())) / APP
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP


@dataclass(frozen=True)
class Profile:
    name: str
    root: Path

    @property
    def token_path(self) -> Path:
        return self.root / "token.json"

    @property
    def client_path(self) -> Path:
        return self.root / "client_secret.json"

    @property
    def settings_path(self) -> Path:
        return self.root / "config.json"

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    @property
    def export_dir(self) -> Path:
        return self.root / "exports"

    def settings(self) -> dict[str, Any]:
        """Per-profile defaults. Absent or unreadable means 'no defaults', never an error -
        a broken config.json must not stop the server from explaining itself."""
        try:
            return json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def default(self, key: str, env_var: str) -> str | None:
        value = os.environ.get(env_var) or self.settings().get(key)
        return str(value) if value else None

    @property
    def default_site(self) -> str | None:
        return self.default("gsc_default_site", "GSC_DEFAULT_SITE")

    @property
    def default_property(self) -> str | None:
        return self.default("ga4_default_property", "GA4_DEFAULT_PROPERTY")

    @property
    def indexnow_key(self) -> str | None:
        return self.default("indexnow_key", "INDEXNOW_KEY")

    @property
    def indexnow_host(self) -> str | None:
        return self.default("indexnow_host", "INDEXNOW_HOST")


def profile_named(name: str) -> Profile:
    if not PROFILE_RE.fullmatch(name):
        raise ToolError(
            f"Invalid profile name {name!r}: letters, digits, '-' and '_' only "
            "(it becomes a directory name)."
        )
    return Profile(name=name, root=config_root() / name)


def active_profile() -> Profile:
    return profile_named(os.environ.get("SEO_MCP_PROFILE", "default"))


def list_profiles() -> list[Profile]:
    root = config_root()
    if not root.is_dir():
        return []
    return [
        Profile(name=p.name, root=p)
        for p in sorted(root.iterdir())
        if p.is_dir() and PROFILE_RE.fullmatch(p.name)
    ]
