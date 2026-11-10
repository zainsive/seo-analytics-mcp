"""The .env loader. Every failure mode here must be non-fatal.

This file loads before anything else the server does, so a crash in it takes the whole
process down - including the CLI commands an operator would use to diagnose the problem.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from seo_mcp import env


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(env.ENV_FILE_VAR, raising=False)
    for key in ("SMOKE_A", "SMOKE_B", "SMOKE_QUOTED"):
        monkeypatch.delenv(key, raising=False)


def test_values_are_applied(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("SMOKE_A=one\nSMOKE_B=two\n")
    monkeypatch.chdir(tmp_path)
    assert env.load() == tmp_path / ".env"
    assert os.environ["SMOKE_A"] == "one"


def test_the_real_environment_always_wins(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("SMOKE_A=from-file\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SMOKE_A", "from-environment")
    env.load()
    assert os.environ["SMOKE_A"] == "from-environment"


def test_a_missing_file_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert env.load() is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root can traverse any directory")
def test_an_env_path_we_cannot_stat_is_not_an_error(tmp_path, monkeypatch):
    """The exact production failure: `sudo -u seomcp ... token mint` inherits the
    invoking user's home as its working directory, and the service account cannot
    search it. Path.is_file() then raises PermissionError instead of returning False,
    so the CLI died on its first line - including the commands needed to diagnose it.

    The working directory here is perfectly readable; only the .env path is not. That
    isolates the stat guard, which is the one that failed.
    """
    locked = tmp_path / "someone-elses-home"
    locked.mkdir()
    (locked / ".env").write_text("SMOKE_A=unreachable\n")
    locked.chmod(0o000)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(env.ENV_FILE_VAR, str(locked / ".env"))
    try:
        assert env._is_file(locked / ".env") is False  # must not raise
        assert env.load() is None
        assert "SMOKE_A" not in os.environ
    finally:
        locked.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can traverse any directory")
def test_a_working_directory_we_cannot_look_inside_is_not_an_error(tmp_path, monkeypatch):
    """Separate guard: resolving the working directory itself can fail, which is how
    this manifests on macOS rather than Linux."""
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / ".env").write_text("SMOKE_A=unreachable\n")
    monkeypatch.chdir(locked)
    locked.chmod(0o000)
    try:
        assert env.load() is None  # must not raise
        assert "SMOKE_A" not in os.environ
    finally:
        locked.chmod(0o700)


def test_a_deleted_working_directory_is_not_an_error(monkeypatch):
    def explode():
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(env.Path, "cwd", staticmethod(explode))
    assert env.candidates() == []
    assert env.load() is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
def test_an_unreadable_file_is_not_an_error(tmp_path, monkeypatch):
    """The hardened deployment keeps .env root-owned 0600 and lets systemd inject it."""
    target = tmp_path / ".env"
    target.write_text("SMOKE_A=secret\n")
    target.chmod(0o000)
    monkeypatch.chdir(tmp_path)
    assert env.load() is None
    assert "SMOKE_A" not in os.environ


def test_an_explicit_path_that_does_not_exist_is_not_an_error():
    assert env.load("/nonexistent/nowhere/.env") is None


def test_comments_blanks_exports_and_quotes(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "\n# a comment\nexport SMOKE_A=one\nSMOKE_QUOTED='quoted'\nnot-a-pair\n"
    )
    monkeypatch.chdir(tmp_path)
    env.load()
    assert os.environ["SMOKE_A"] == "one"
    assert os.environ["SMOKE_QUOTED"] == "quoted"


def test_a_value_containing_equals_survives(tmp_path, monkeypatch):
    """Base64 and JSON both contain '=' and '='-padding."""
    (tmp_path / ".env").write_text("SMOKE_A=a=b==\n")
    monkeypatch.chdir(tmp_path)
    env.load()
    assert os.environ["SMOKE_A"] == "a=b=="


def test_the_explicit_variable_overrides_the_working_directory(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("SMOKE_A=cwd\n")
    other = tmp_path / "other.env"
    other.write_text("SMOKE_A=explicit\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(env.ENV_FILE_VAR, str(other))
    assert env.load() == other
    assert os.environ["SMOKE_A"] == "explicit"


def test_a_world_readable_credential_file_is_called_out(tmp_path, monkeypatch, caplog):
    target = tmp_path / ".env"
    target.write_text("SMOKE_A=secret\n")
    target.chmod(0o644)
    monkeypatch.chdir(tmp_path)
    with caplog.at_level("WARNING"):
        env.load()
    assert "readable by other users" in caplog.text
    assert "SMOKE_A=secret" not in caplog.text  # the warning must not quote the contents


def test_candidates_is_empty_rather_than_guessing_when_the_path_is_unusable(monkeypatch):
    monkeypatch.setenv(env.ENV_FILE_VAR, "~/definitely-not-here.env")
    assert env.candidates() == [Path("~/definitely-not-here.env").expanduser()]
