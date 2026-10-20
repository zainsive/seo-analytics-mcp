"""Credential selection and .env loading. No network, no real keys."""

import base64
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from seo_mcp import config, credentials, env
from seo_mcp.auth import scopes


@pytest.fixture
def key_info():
    """A real RSA key, generated here - never a fixture checked into the repo."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return {
        "type": "service_account",
        "project_id": "demo",
        "client_email": "seo-reader@demo.iam.gserviceaccount.com",
        "private_key": pem,
        "private_key_id": "k1",
        "token_uri": "https://oauth2.googleapis.com/token",
    }


def test_user_mode_is_the_default_so_local_installs_are_unaffected():
    assert credentials.mode() == "user"
    assert credentials.describe()["mode"] == "user"


def test_inline_json_selects_service_account_mode(monkeypatch, key_info):
    monkeypatch.setenv(credentials.SERVICE_ACCOUNT_JSON, json.dumps(key_info))
    assert credentials.mode() == "service_account"
    assert credentials.service_account_email() == key_info["client_email"]


def test_base64_is_accepted_because_env_files_mangle_private_key_newlines(monkeypatch, key_info):
    encoded = base64.b64encode(json.dumps(key_info).encode()).decode()
    monkeypatch.setenv(credentials.SERVICE_ACCOUNT_JSON, encoded)
    assert credentials.mode() == "service_account"
    creds = credentials.for_scopes({scopes.GSC_READ})
    assert creds.scopes == [scopes.GSC_READ]  # only what the caller asked for


def test_a_key_file_path_also_works(monkeypatch, tmp_path, key_info):
    path = tmp_path / "sa.json"
    path.write_text(json.dumps(key_info))
    monkeypatch.setenv(credentials.SERVICE_ACCOUNT_FILE, str(path))
    assert credentials.service_account_email() == key_info["client_email"]


def test_a_missing_key_file_says_so(monkeypatch, tmp_path):
    monkeypatch.setenv(credentials.SERVICE_ACCOUNT_FILE, str(tmp_path / "absent.json"))
    with pytest.raises(ToolError, match="does not exist"):
        credentials.mode()


def test_garbage_is_rejected_as_neither_json_nor_base64(monkeypatch):
    monkeypatch.setenv(credentials.SERVICE_ACCOUNT_JSON, "!! not a key !!")
    with pytest.raises(ToolError, match="neither JSON nor valid base64"):
        credentials.mode()


def test_an_oauth_client_file_pasted_by_mistake_is_named_as_the_mistake(monkeypatch):
    """The likeliest operator error: pasting client_secret.json instead of the key."""
    monkeypatch.setenv(
        credentials.SERVICE_ACCOUNT_JSON, json.dumps({"installed": {"client_id": "x"}})
    )
    with pytest.raises(ToolError, match="does not hold a service-account key"):
        credentials.mode()


def test_a_truncated_key_is_rejected_rather_than_failing_later(monkeypatch, key_info):
    del key_info["private_key"]
    monkeypatch.setenv(credentials.SERVICE_ACCOUNT_JSON, json.dumps(key_info))
    with pytest.raises(ToolError, match="missing 'private_key'"):
        credentials.mode()


def test_describe_never_leaks_key_material(monkeypatch, key_info):
    monkeypatch.setenv(credentials.SERVICE_ACCOUNT_JSON, json.dumps(key_info))
    blob = json.dumps(credentials.describe())
    assert "PRIVATE KEY" not in blob
    assert key_info["private_key"] not in blob
    assert "private_key" not in blob


def test_describe_explains_that_granting_is_a_separate_step(monkeypatch, key_info):
    monkeypatch.setenv(credentials.SERVICE_ACCOUNT_JSON, json.dumps(key_info))
    assert "Users and permissions" in credentials.describe()["note"]


# --- .env loading ------------------------------------------------------------


def test_env_file_values_are_applied(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text('# a comment\nexport A_VAL=1\nB_VAL="two words"\nC_VAL=three\n')
    monkeypatch.delenv("A_VAL", raising=False)
    assert env.load(str(f)) == f
    import os

    assert os.environ["A_VAL"] == "1"
    assert os.environ["B_VAL"] == "two words"
    assert os.environ["C_VAL"] == "three"


def test_the_real_environment_always_beats_the_file(tmp_path, monkeypatch):
    """A container's injected secret must win over a .env that shipped in the image."""
    f = tmp_path / ".env"
    f.write_text("WINNER=from-file\n")
    monkeypatch.setenv("WINNER", "from-environment")
    env.load(str(f))
    import os

    assert os.environ["WINNER"] == "from-environment"


def test_a_missing_env_file_is_not_an_error(tmp_path):
    assert env.load(str(tmp_path / "absent.env")) is None


def test_a_world_readable_env_file_is_warned_about(tmp_path, caplog):
    f = tmp_path / ".env"
    f.write_text("X=1\n")
    f.chmod(0o644)
    with caplog.at_level("WARNING"):
        env.load(str(f))
    assert "readable by other users" in caplog.text


# --- hosted-mode behaviour ---------------------------------------------------


def test_exports_are_on_by_default_for_local_installs():
    assert config.exports_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "FALSE"])
def test_exports_can_be_disabled_for_a_container_with_no_reachable_disk(monkeypatch, value):
    monkeypatch.setenv("SEO_MCP_EXPORTS", value)
    assert config.exports_enabled() is False


def test_the_endpoint_path_defaults_but_can_be_made_unguessable(monkeypatch):
    from seo_mcp import server

    assert server.endpoint_path() == "/mcp"
    monkeypatch.setenv(server.ENDPOINT_PATH_VAR, "mcp/9f3c1e7a5b2d48c6/")
    assert server.endpoint_path() == "/mcp/9f3c1e7a5b2d48c6"


def test_service_account_mode_never_registers_a_write_tool(monkeypatch, key_info):
    """A server shared with a team must not be able to write to the live web.

    hide_ungranted_write_tools() mutates the module-level server, which is correct in
    production (called once at startup) but leaks between tests, so the registry is
    snapshotted and restored here.
    """
    from seo_mcp import server

    registry = server.mcp._tool_manager._tools
    saved = dict(registry)
    try:
        monkeypatch.setenv(credentials.SERVICE_ACCOUNT_JSON, json.dumps(key_info))
        removed = server.hide_ungranted_write_tools()
        assert "gsc_submit_sitemap" in removed
        assert "gsc_submit_sitemap" not in registry
        assert "gsc_search_analytics" in registry  # read tools are untouched
    finally:
        registry.clear()
        registry.update(saved)


def test_a_403_in_service_account_mode_says_grant_not_reauthorise():
    """ "Re-authorise" is useless advice for a service account - the fix is a grant."""
    import json as _json

    import httplib2
    from googleapiclient.errors import HttpError

    from seo_mcp.errors import Ctx, translate

    exc = HttpError(
        httplib2.Response({"status": 403}),
        _json.dumps({"error": {"code": 403, "errors": [{"reason": "forbidden"}]}}).encode(),
        uri="u",
    )
    message = str(
        translate(
            exc,
            Ctx(
                site_url="sc-domain:example.com",
                service_account_email="bot@p.iam.gserviceaccount.com",
            ),
        )
    )
    assert "bot@p.iam.gserviceaccount.com has no access to sc-domain:example.com" in message
    assert "Users and permissions" in message
    assert "--reauth" not in message  # nothing to re-authorise
    assert "no redeploy is required" in message


async def test_an_error_names_a_site_that_came_from_a_default(monkeypatch, key_info):
    """The identifier is resolved from config, so the message must still name it
    rather than falling back to "this resource"."""
    import json as _json

    import httplib2
    import pytest as _pytest
    from googleapiclient.errors import HttpError
    from mcp.server.mcpserver.exceptions import ToolError

    from seo_mcp.clients import gsc
    from seo_mcp.server import mcp as server_mcp

    # A usable credential, so the tool reaches the client rather than failing on auth.
    monkeypatch.setenv(credentials.SERVICE_ACCOUNT_JSON, json.dumps(key_info))
    monkeypatch.setenv("GSC_DEFAULT_SITE", "sc-domain:from-default.com")

    async def boom(*a, **kw):
        raise HttpError(
            httplib2.Response({"status": 403}),
            _json.dumps({"error": {"code": 403, "errors": [{"reason": "forbidden"}]}}).encode(),
            uri="u",
        )

    monkeypatch.setattr(gsc, "list_sites", boom)
    with _pytest.raises(ToolError) as exc:
        await server_mcp.call_tool("gsc_list_sites", {})
    assert "sc-domain:from-default.com" in str(exc.value)
    assert "this resource" not in str(exc.value)


def test_without_an_allowlist_only_localhost_is_accepted():
    """The SDK default. Correct for a local or tunnelled test, fatal behind a proxy."""
    from seo_mcp import server

    assert server.transport_security() is None


def test_the_public_hostname_can_be_allowed_for_a_reverse_proxy(monkeypatch):
    """Without this every proxied request answers 421 Invalid Host header - a failure
    that looks like a proxy misconfiguration and is not."""
    from seo_mcp import server

    monkeypatch.setenv(server.ALLOWED_HOSTS_VAR, "mcp.example.com, mcp2.example.com")
    security = server.transport_security()
    assert security.allowed_hosts == ["mcp.example.com", "mcp2.example.com"]
    assert "https://mcp.example.com" in security.allowed_origins
    assert security.enable_dns_rebinding_protection is True  # still on, just scoped


def test_the_reported_version_matches_the_installed_distribution():
    """A mismatch here silently reports 0.0.0.dev0 in doctor output and issue reports."""
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as dist_version
    from pathlib import Path

    import tomllib

    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    name = pyproject["project"]["name"]
    try:
        installed = dist_version(name)
    except PackageNotFoundError:
        import pytest as _pytest

        _pytest.skip("package not installed in this environment")
    from seo_mcp import __version__

    assert __version__ == installed
