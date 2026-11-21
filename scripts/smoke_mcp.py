"""End-to-end smoke test of a running SheetGuard MCP server over stdio.

Usage:
    python scripts/smoke_mcp.py -- sheetguard-mcp --root tests/fixtures
    python scripts/smoke_mcp.py -- docker run -i --rm --read-only --cap-drop ALL \\
        --network none -v "$PWD/tests/fixtures:/data:ro" sheetguard:ci

The data folder must contain reference/dcf.xlsx and injection/traps.xlsx (the test
fixtures). Exits non-zero on any failure. Used by CI against the Docker image.
"""

from __future__ import annotations

import sys
from typing import Any

import anyio
from mcp.client.stdio import StdioServerParameters

from mcp import Client

EXPECTED_TOOLS = {
    "open_workbook",
    "audit_workbook",
    "trace_cell",
    "top_drivers",
    "explain_issue",
    "get_range",
    "list_untrusted_content",
    "close_workbook",
}


async def run(command: list[str]) -> None:
    params = StdioServerParameters(command=command[0], args=command[1:])
    async with Client(params) as c:

        async def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
            r = await c.call_tool(name, args)
            if r.is_error or r.structured_content is None:
                raise SystemExit(f"FAIL {name}: {r.content}")
            print(f"ok   {name}")
            return r.structured_content

        tools = {t.name for t in (await c.list_tools()).tools}
        assert tools == EXPECTED_TOOLS, tools
        print("ok   list_tools (8)")
        dcf = (await call("open_workbook", {"path": "reference/dcf.xlsx"}))["workbook_id"]
        await call("audit_workbook", {"workbook_id": dcf})
        await call("trace_cell", {"workbook_id": dcf, "cell": "Model!B12", "max_depth": 2})
        drivers = await call("top_drivers", {"workbook_id": dcf, "target_cell": "Model!B12"})
        assert drivers["result"]["drivers"][0]["input"] == "Assumptions!B7"
        await call("get_range", {"workbook_id": dcf, "range": "Assumptions!A1:B8"})
        traps = await call("open_workbook", {"path": "injection/traps.xlsx"})
        assert traps["flags"]["untrusted_content_found"]
        audit = await call("audit_workbook", {"workbook_id": traps["workbook_id"]})
        await call(
            "explain_issue",
            {"workbook_id": traps["workbook_id"], "issue_id": audit["issues"][0]["id"]},
        )
        high = await call(
            "list_untrusted_content", {"workbook_id": traps["workbook_id"], "min_risk": "high"}
        )
        assert high["summary"]["total"] >= 15
        await call("close_workbook", {"workbook_id": dcf})
        bad = await c.call_tool("open_workbook", {"path": "../../etc/passwd"})
        assert bad.is_error
        assert "path_not_allowed" in bad.content[0].text  # type: ignore[union-attr]
        print("ok   path traversal refused")
    print("SMOKE TEST PASSED")


def main() -> None:
    argv = sys.argv[1:]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        raise SystemExit(__doc__)
    anyio.run(run, argv)


if __name__ == "__main__":
    main()
