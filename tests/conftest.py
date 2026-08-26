import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:  # tests run from a source tree without installing
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """No test may read or write the developer's real profile directory."""
    monkeypatch.setenv("SEO_MCP_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SEO_MCP_PROFILE", "default")
    for var in ("GSC_DEFAULT_SITE", "GA4_DEFAULT_PROPERTY", "INDEXNOW_KEY", "INDEXNOW_HOST"):
        monkeypatch.delenv(var, raising=False)
    yield
