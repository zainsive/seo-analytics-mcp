"""Load configuration from a .env file.

Resolution order, highest first: real environment > .env file > built-in default.
A value already present in the environment is never overwritten, so a container's
injected secrets always beat a file that happened to ship in the image.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_FILE_VAR = "SEO_MCP_ENV_FILE"
DEFAULT_NAMES = (".env", ".env.local")


def candidates(explicit: str | None = None) -> list[Path]:
    if explicit:
        return [Path(explicit).expanduser()]
    if from_env := os.environ.get(ENV_FILE_VAR):
        return [Path(from_env).expanduser()]
    cwd = Path.cwd()
    return [cwd / name for name in DEFAULT_NAMES]


def _warn_if_world_readable(path: Path) -> None:
    """A .env holding a service-account key deserves the same care as a token file."""
    try:
        mode = path.stat().st_mode
    except OSError:  # pragma: no cover
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        logger.warning(
            "%s is readable by other users (mode %s). It holds credentials - run: chmod 600 %s",
            path,
            oct(mode & 0o777),
            path,
        )


def load(explicit: str | None = None) -> Path | None:
    """Apply the first .env found. Returns the file used, or None.

    Missing file is not an error: the hosted deployment injects real environment
    variables instead, and the local stdio install needs neither.
    """
    for path in candidates(explicit):
        if not path.is_file():
            continue
        _warn_if_world_readable(path)
        applied = 0
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, sep, value = line.partition("=")
            if not sep:
                continue
            key = key.strip()
            if not key or key in os.environ:  # the real environment always wins
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ[key] = value
            applied += 1
        logger.info("loaded %d value(s) from %s", applied, path)
        return path
    return None
