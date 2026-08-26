"""Mechanical enforcement of the import rules.

RULE 2 is the one a contributor will break: import auth.flow from a tool and a tool
call can block on a consent screen, which is the failure the whole design exists to
prevent. An AST walk is cheap and catches it in CI.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "seo_mcp"
LOWER_LAYERS = sorted((SRC / "clients").glob("*.py")) + sorted((SRC / "tools").glob("*.py"))
LEAVES = ["shaping.py", "config.py", "errors.py", "auth/scopes.py"]


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = ("." * node.level) + (node.module or "")
            found.add(base)
            found.update(f"{base}.{a.name}" for a in node.names)
    return found


@pytest.mark.parametrize("path", LOWER_LAYERS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_client_or_tool_may_reach_the_browser_flow(path):
    offenders = {
        m for m in imported_modules(path) if "flow" in m.split(".")[-1] or m.endswith("auth.flow")
    }
    assert not offenders, (
        f"{path.name} imports {offenders}: only cli.py may reach auth/flow.py, or a tool "
        "call can block on a browser consent screen."
    )


def test_only_the_cli_imports_the_browser_flow():
    reachers = {
        p.relative_to(SRC).as_posix()
        for p in SRC.rglob("*.py")
        if p != SRC / "auth" / "flow.py"  # the module itself, obviously
        and any(
            m.endswith("auth.flow") or m.rstrip(".").endswith(".flow") for m in imported_modules(p)
        )
    }
    assert reachers == {"cli.py"}, f"auth/flow.py is reachable from {reachers}"


@pytest.mark.parametrize("leaf", LEAVES)
def test_a_leaf_module_imports_nothing_else_from_the_project(leaf):
    """Leaves are importable by anyone and import nobody, which is what keeps the
    dependency graph pointing downhill."""
    modules = imported_modules(SRC / leaf)
    relative = {m for m in modules if m.startswith(".")}
    assert not relative, f"{leaf} imports project modules {relative}"


def test_shaping_imports_no_google_library():
    """RULE 4 - this is what keeps the row logic unit-testable with no credentials."""
    modules = imported_modules(SRC / "shaping.py")
    assert not [m for m in modules if "google" in m], modules


def test_only_clients_speak_http():
    """RULE 3 - a tool that builds a request body is a layering violation."""
    http_libs = {"httpx", "requests", "urllib.request", "http.client", "googleapiclient"}
    for path in sorted((SRC / "tools").glob("*.py")):
        offenders = {m for m in imported_modules(path) if m.split(".")[0] in http_libs}
        assert not offenders, f"tools/{path.name} imports {offenders}; put it in clients/"


def test_the_server_module_never_writes_to_stdout():
    """stdout carries JSON-RPC on a stdio transport: one print() corrupts the stream."""
    for name in ("server.py", "shaping.py", "cache.py", "errors.py"):
        tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
        prints = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print"
        ]
        assert not prints, f"{name} calls print(); log to stderr instead"
    for path in sorted((SRC / "tools").glob("*.py")) + sorted((SRC / "clients").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        prints = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print"
        ]
        assert not prints, f"{path.name} calls print(); log to stderr instead"


def test_the_client_secret_path_tolerates_a_missing_json_suffix(tmp_path):
    """Shell tab-completion stops at the '.com' in Google's filename."""
    import sys

    sys.path.insert(0, str(SRC.parent))
    from seo_mcp.auth.flow import resolve_client_secret
    from seo_mcp.errors import ClientSecretMissing

    real = tmp_path / "client_secret_1.apps.googleusercontent.com.json"
    real.write_text("{}")
    short = tmp_path / "client_secret_1.apps.googleusercontent.com"
    assert resolve_client_secret(short) == real
    assert resolve_client_secret(real) == real

    with pytest.raises(ClientSecretMissing):
        resolve_client_secret(tmp_path / "absent.json")
