"""The social integrations are read-only, and must stay that way.

Mailchimp and LinkedIn were both added on the explicit condition that they only fetch
statistics: no create, no update, no delete. A promise in a docstring does not survive
the next person in a hurry, so the guarantee is checked here against the source itself.

Google is deliberately out of scope: gsc_submit_sitemap and indexnow_submit write on
purpose, are annotated destructive, and are unregistered entirely in the hosted
deployment.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from seo_mcp.clients import linkedin, mailchimp, youtube
from seo_mcp.server import mcp

SRC = Path(__file__).resolve().parents[1] / "src" / "seo_mcp"
READ_ONLY_CLIENTS = ("mailchimp.py", "linkedin.py")
READ_ONLY_TOOLS = ("mailchimp_tools.py", "linkedin_tools.py", "youtube_tools.py")
# Google clients speak through googleapiclient, not httpx, so the verb check below
# does not reach them. These are the method names that would change something.
GOOGLE_CLIENTS = ("youtube.py",)
GOOGLE_WRITING_METHODS = {
    "insert",
    "update",
    "patch",
    "delete",
    "rate",
    "upload",
    "bind",
    "unbind",
    "ban",
    "unban",
    "markAsSpam",
    "setModerationStatus",
    "reportAbuse",
    "setThumbnail",
    "set",
}

# httpx verbs that change something at the other end.
WRITING_VERBS = {"post", "put", "patch", "delete", "request", "stream", "send"}


def _calls(path: Path) -> list[ast.Call]:
    return [n for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.Call)]


@pytest.mark.parametrize("name", READ_ONLY_CLIENTS)
def test_the_client_never_issues_a_writing_request(name):
    """Catches `client.post(...)` and `httpx.delete(...)` wherever they appear.

    `request` and `send` are included because both can carry any verb, so either would
    be a way around this check rather than an exception to it.
    """
    offenders = [
        node.func.attr
        for node in _calls(SRC / "clients" / name)
        if isinstance(node.func, ast.Attribute) and node.func.attr in WRITING_VERBS
    ]
    assert not offenders, f"clients/{name} issues {sorted(set(offenders))}; it must only GET"


@pytest.mark.parametrize("name", READ_ONLY_CLIENTS)
def test_the_client_does_issue_gets(name):
    """Guards the guard: a client that stopped making requests would pass the test
    above for the wrong reason."""
    gets = [
        node
        for node in _calls(SRC / "clients" / name)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get"
    ]
    assert gets, f"clients/{name} makes no GET calls - has the check above gone stale?"


@pytest.mark.parametrize("name", READ_ONLY_TOOLS)
def test_no_tool_module_speaks_http_directly(name):
    """Layering: a write could otherwise be smuggled in above the client."""
    tree = ast.parse((SRC / "tools" / name).read_text())
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "httpx" not in imported and "requests" not in imported


@pytest.mark.parametrize("prefix", ["mailchimp_", "linkedin_", "youtube_"])
async def test_every_tool_is_annotated_read_only(prefix):
    """The annotation is what a client's approval UI actually reads."""
    tools = [t for t in await mcp.list_tools() if t.name.startswith(prefix)]
    assert tools, f"no {prefix} tools are registered"
    for tool in tools:
        assert tool.annotations is not None, f"{tool.name} carries no annotations"
        assert tool.annotations.read_only_hint is True, f"{tool.name} is not read-only"
        assert not tool.annotations.destructive_hint, f"{tool.name} is marked destructive"


@pytest.mark.parametrize("prefix", ["mailchimp_", "linkedin_", "youtube_"])
async def test_no_tool_name_suggests_a_write(prefix):
    """A fetch-only integration has no business owning a verb like create or delete."""
    forbidden = ("create", "update", "delete", "post_", "send", "publish", "remove", "submit")
    for tool in await mcp.list_tools():
        if tool.name.startswith(prefix):
            assert not any(word in tool.name for word in forbidden), tool.name


@pytest.mark.parametrize("module", [mailchimp, linkedin, youtube])
def test_no_public_client_function_is_named_for_a_write(module):
    forbidden = ("create", "update", "delete", "publish", "remove", "send", "post")
    for name, _ in inspect.getmembers(module, inspect.isfunction):
        if not name.startswith("_"):
            assert not any(word in name for word in forbidden), f"{module.__name__}.{name}"


@pytest.mark.parametrize("prefix", ["mailchimp_", "linkedin_", "youtube_"])
async def test_no_credential_is_exposed_as_a_tool_argument(prefix):
    """Tool arguments appear in the transcript and may be logged by the client."""
    secret = ("key", "token", "secret", "password", "credential", "auth")
    for tool in await mcp.list_tools():
        if tool.name.startswith(prefix):
            for arg in tool.input_schema.get("properties") or {}:
                assert not any(word in arg.lower() for word in secret), f"{tool.name}.{arg}"


@pytest.mark.parametrize("name", GOOGLE_CLIENTS)
def test_the_google_client_only_calls_reading_methods(name):
    """googleapiclient builds its verbs dynamically, so nothing stops `.insert(...)`
    compiling. The names are checked here instead."""
    offenders = [
        node.func.attr
        for node in _calls(SRC / "clients" / name)
        if isinstance(node.func, ast.Attribute)
        and node.func.attr in GOOGLE_WRITING_METHODS
        # googleapiclient always reads `service.videos().insert(...)`, so the receiver
        # is itself a call. That excludes `some_dict.update(...)`, which is not an API
        # write and was the first thing this test flagged.
        and isinstance(node.func.value, ast.Call)
    ]
    assert not offenders, f"clients/{name} calls {sorted(set(offenders))}"


@pytest.mark.parametrize("name", GOOGLE_CLIENTS)
def test_the_google_client_does_call_reading_methods(name):
    """Guards the guard, as above."""
    reads = {
        node.func.attr
        for node in _calls(SRC / "clients" / name)
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"list", "query", "execute"}
    }
    assert reads, f"clients/{name} makes no read calls - has the check gone stale?"


def test_the_youtube_client_never_refreshes_a_credential_itself():
    """Refreshing posts to Google's token endpoint. That belongs in youtube_auth, where
    google-auth owns it, not in a module whose whole claim is that it only reads."""
    source = (SRC / "clients" / "youtube.py").read_text()
    assert "oauth2.googleapis.com" not in source
    assert ".refresh(" not in source
