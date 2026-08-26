"""Path resolution per platform and the profile-name gate that guards a directory name."""

import os
import sys

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from seo_mcp import config


def test_seo_mcp_home_overrides_every_platform_default(tmp_path, monkeypatch):
    monkeypatch.setenv("SEO_MCP_HOME", str(tmp_path / "custom"))
    assert config.config_root() == tmp_path / "custom"


@pytest.mark.parametrize(
    "platform,expected_fragment", [("darwin", "Application Support"), ("linux", ".config")]
)
def test_platform_specific_roots(platform, expected_fragment, monkeypatch):
    monkeypatch.delenv("SEO_MCP_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(os, "name", "posix")
    assert expected_fragment in str(config.config_root())


def test_xdg_config_home_is_respected_on_linux(monkeypatch, tmp_path):
    monkeypatch.delenv("SEO_MCP_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.config_root() == tmp_path / "seo-mcp"


@pytest.mark.parametrize("name", ["default", "client-a", "Client_9", "a" * 64])
def test_valid_profile_names(name):
    assert config.profile_named(name).name == name


@pytest.mark.parametrize("name", ["../escape", "a/b", "", "a" * 65, "with space", "."])
def test_a_profile_name_that_could_escape_the_directory_is_rejected(name):
    with pytest.raises(ToolError, match="Invalid profile name"):
        config.profile_named(name)


def test_profile_paths_all_live_under_the_profile_directory():
    profile = config.active_profile()
    for path in (
        profile.token_path,
        profile.client_path,
        profile.cache_dir,
        profile.export_dir,
        profile.settings_path,
    ):
        assert profile.root in path.parents


def test_env_var_beats_the_profile_settings_file(monkeypatch):
    profile = config.active_profile()
    profile.root.mkdir(parents=True)
    profile.settings_path.write_text('{"gsc_default_site": "sc-domain:from-file.com"}')
    assert profile.default_site == "sc-domain:from-file.com"
    monkeypatch.setenv("GSC_DEFAULT_SITE", "sc-domain:from-env.com")
    assert profile.default_site == "sc-domain:from-env.com"


def test_a_broken_settings_file_is_ignored_rather_than_fatal():
    """A server that cannot start cannot explain itself."""
    profile = config.active_profile()
    profile.root.mkdir(parents=True)
    profile.settings_path.write_text("{not json")
    assert profile.settings() == {}
    assert profile.default_site is None


def test_active_profile_follows_the_env_var(monkeypatch):
    monkeypatch.setenv("SEO_MCP_PROFILE", "client-b")
    assert config.active_profile().name == "client-b"


def test_list_profiles_finds_only_directories():
    root = config.config_root()
    (root / "alpha").mkdir(parents=True)
    (root / "beta").mkdir(parents=True)
    (root / "stray.txt").write_text("x")
    assert [p.name for p in config.list_profiles()] == ["alpha", "beta"]
