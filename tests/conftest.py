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


@pytest.fixture(scope="session")
def _service_account_pem():
    """A real RSA key, generated here - never a fixture checked into the repo.

    Session-scoped because a 2048-bit keygen per test is the slowest thing in the
    suite by a wide margin.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def key_info(_service_account_pem):
    """A complete service-account key file, fresh for every test.

    Per-test rather than session-scoped: one test deletes a field to check the
    "incomplete key" error, and a shared dict would carry that deletion into every
    test that ran after it.
    """
    return {
        "type": "service_account",
        "project_id": "demo",
        "client_email": "seo-reader@demo.iam.gserviceaccount.com",
        "private_key": _service_account_pem,
        "private_key_id": "k1",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
