#!/usr/bin/env python3
"""Drive the server over real stdio JSON-RPC, the way an MCP client does.

This is the fastest end-to-end check that the server starts, speaks the protocol,
registers its tools, and returns actionable errors. It uses a throwaway profile
directory, so it never touches your real token.

    python scripts/smoke.py                     # uses `seo-mcp` from PATH
    python scripts/smoke.py ./.venv/bin/seo-mcp # or an explicit binary
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = 13


async def main(binary: str) -> int:
    home = tempfile.mkdtemp(prefix="seo-mcp-smoke-")
    env = {
        **os.environ,
        "SEO_MCP_HOME": home,  # a throwaway profile: your real token is untouched
        "SEO_MCP_PROFILE": "default",
        "SEO_MCP_LOG_LEVEL": "CRITICAL",
    }
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}{f' - {detail}' if detail else ''}")
        if not ok:
            failures.append(label)

    print(f"Driving {binary} over stdio (profile dir {home})\n")
    async with stdio_client(StdioServerParameters(command=binary, args=[], env=env)) as (r, w):
        async with ClientSession(r, w) as session:
            init = await session.initialize()
            check(
                "initialize",
                init.server_info.name == "seo",
                f"{init.server_info.name} {init.server_info.version}",
            )
            check("instructions sent", bool(init.instructions))

            tools = (await session.list_tools()).tools
            check("tools/list", len(tools) == EXPECTED_TOOLS, f"{len(tools)} tools")

            destructive = sorted(
                t.name for t in tools if t.annotations and t.annotations.destructive_hint
            )
            check(
                "write tools marked destructive",
                destructive == ["gsc_submit_sitemap", "indexnow_submit"],
                str(destructive),
            )

            result = await session.call_tool("auth_status", {})
            payload = json.loads(result.content[0].text)
            check(
                "auth_status answers unauthenticated",
                result.is_error is not True and payload["authorised"] is False,
            )
            check(
                "auth_status names the next command",
                "seo-mcp auth --client-secret" in payload["next_step"],
            )

            result = await session.call_tool("gsc_list_sites", {})
            text = result.content[0].text
            check("a tool with no token errors", result.is_error is True)
            check("the error names the fix", "seo-mcp auth" in text)
            check("no traceback reaches the model", "Traceback" not in text)

            result = await session.call_tool(
                "gsc_search_analytics", {"site_url": "sc-domain:example.com", "start_date": "soon"}
            )
            check(
                "a bad argument is rejected before auth",
                result.is_error is True and "YYYY-MM-DD" in result.content[0].text,
            )

    shutil.rmtree(home, ignore_errors=True)
    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("All smoke checks passed.")
    return 0


if __name__ == "__main__":
    binary = sys.argv[1] if len(sys.argv) > 1 else "seo-mcp"
    resolved = shutil.which(binary) or binary
    raise SystemExit(asyncio.run(main(resolved)))
