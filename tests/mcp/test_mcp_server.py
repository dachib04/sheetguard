"""MCP integration (§10.1): every tool through the real server, schemas validated,
pagination and response-size limits respected, structured safe errors (FR-50..53)."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.client.stdio import StdioServerParameters

from mcp import Client
from sheetguard.mcp.models import (
    AuditResult,
    CloseResult,
    ExplainIssueResult,
    GetRangeResult,
    OpenWorkbookResult,
    TopDriversResult,
    TraceCellResult,
    UntrustedListResult,
)
from sheetguard.mcp.server import build_server, main
from sheetguard.mcp.tools import SheetGuardTools, WorkbookStore, fit, json_size, paginate
from sheetguard.security.envelope import wrap
from sheetguard.security.limits import DEFAULT_LIMITS

FIXTURES = Path(__file__).parents[1] / "fixtures"
TOOLS = {
    "open_workbook",
    "audit_workbook",
    "trace_cell",
    "top_drivers",
    "explain_issue",
    "get_range",
    "list_untrusted_content",
    "close_workbook",
}


def run(coro_fn: Any) -> Any:
    return anyio.run(coro_fn)


async def call(c: Client, name: str, args: dict[str, Any]) -> dict[str, Any]:
    r = await c.call_tool(name, args)
    assert not r.is_error, r.content
    assert r.structured_content is not None
    return r.structured_content


async def error(c: Client, name: str, args: dict[str, Any]) -> dict[str, str]:
    r = await c.call_tool(name, args)
    assert r.is_error
    text = r.content[0].text  # type: ignore[union-attr]
    payload = json.loads(text[text.index("{") :])
    assert set(payload) == {"code", "message"}
    assert "Traceback" not in text and "/home/" not in text
    return payload


# --------------------------------------------------------------- every tool


def test_all_tools_end_to_end_in_process():
    async def scenario() -> None:
        async with Client(build_server(FIXTURES)) as c:
            listed = await c.list_tools()
            assert {t.name for t in listed.tools} == TOOLS
            for t in listed.tools:  # FR-50: short, factual, read-only
                assert t.description and len(t.description) < 300
                assert t.annotations is not None and t.annotations.read_only_hint
                assert t.output_schema

            o = OpenWorkbookResult.model_validate(
                await call(c, "open_workbook", {"path": "reference/errors.xlsx"})
            )
            wid = o.workbook_id
            assert o.sheet_count == 1 and not o.flags.untrusted_content_found

            a = AuditResult.model_validate(
                await call(c, "audit_workbook", {"workbook_id": wid, "limit": 3})
            )
            assert a.total_matching == 8 and len(a.issues) == 3 and a.next_cursor == "3"
            seen = [i.id for i in a.issues]
            cursor = a.next_cursor
            while cursor:
                page = AuditResult.model_validate(
                    await call(
                        c, "audit_workbook", {"workbook_id": wid, "limit": 3, "cursor": cursor}
                    )
                )
                seen += [i.id for i in page.issues]
                cursor = page.next_cursor
            assert len(seen) == len(set(seen)) == 8

            crit = AuditResult.model_validate(
                await call(
                    c,
                    "audit_workbook",
                    {"workbook_id": wid, "checks": ["C05"], "min_severity": "critical"},
                )
            )
            assert {i.check_id for i in crit.issues} == {"C05"}

            t = TraceCellResult.model_validate(
                await call(c, "trace_cell", {"workbook_id": wid, "cell": "Calc!H4", "max_depth": 2})
            )
            assert t.trace.root.address == "Calc!H4"

            e = ExplainIssueResult.model_validate(
                await call(c, "explain_issue", {"workbook_id": wid, "issue_id": a.issues[0].id})
            )
            assert e.issue.cell == "Calc!E4" and e.issue.suggested_fix is not None
            assert "False-positive policy" in e.check_policy and e.context

            g = GetRangeResult.model_validate(
                await call(c, "get_range", {"workbook_id": wid, "range": "Calc!A1:C4"})
            )
            assert g.cells[0].address == "Calc!A1" and g.cells[0].value.kind == "untrusted_text"

            u = UntrustedListResult.model_validate(
                await call(c, "list_untrusted_content", {"workbook_id": wid})
            )
            assert u.summary.total == 0

            dcf = OpenWorkbookResult.model_validate(
                await call(c, "open_workbook", {"path": "reference/dcf.xlsx"})
            )
            d = TopDriversResult.model_validate(
                await call(
                    c,
                    "top_drivers",
                    {"workbook_id": dcf.workbook_id, "target_cell": "Model!B12", "n": 3},
                )
            )
            assert d.result.drivers[0].input == "Assumptions!B7"

            closed = CloseResult.model_validate(
                await call(c, "close_workbook", {"workbook_id": wid})
            )
            assert closed.status == "closed"
            err = await error(c, "audit_workbook", {"workbook_id": wid})
            assert err["code"] == "unknown_workbook"

    run(scenario)


def test_untrusted_content_and_envelopes():
    async def scenario() -> None:
        async with Client(build_server(FIXTURES)) as c:
            o = await call(c, "open_workbook", {"path": "injection/traps.xlsx"})
            assert o["flags"]["untrusted_content_found"] is True
            assert o["untrusted_summary"]["by_risk"]["high"] >= 15
            wid = o["workbook_id"]
            high = await call(
                c, "list_untrusted_content", {"workbook_id": wid, "min_risk": "high", "limit": 100}
            )
            assert all(i["risk"] == "high" for i in high["items"])
            for item in high["items"]:
                assert item["text"]["kind"] == "untrusted_text"
                assert item["reasons"] and item["vector"]
            r = await call(c, "get_range", {"workbook_id": wid, "range": "Summary!H20:H22"})
            assert {cell["value"]["risk"] for cell in r["cells"]} == {"high"}

    run(scenario)


# ---------------------------------------------------------------- errors


def test_structured_errors():
    async def scenario() -> None:
        async with Client(build_server(FIXTURES)) as c:
            cases = [
                ("open_workbook", {"path": "../../etc/passwd"}, "path_not_allowed"),
                ("open_workbook", {"path": "/etc/passwd"}, "path_not_allowed"),
                ("open_workbook", {"path": "missing.xlsx"}, "not_found"),
                ("open_workbook", {"path": "build_fixtures.py"}, "unsupported_format"),
                ("audit_workbook", {"workbook_id": "wb_nope"}, "unknown_workbook"),
            ]
            for tool, args, code in cases:
                assert (await error(c, tool, args))["code"] == code, (tool, args)
            wid = (await call(c, "open_workbook", {"path": "reference/dcf.xlsx"}))["workbook_id"]
            bad = [
                ("trace_cell", {"workbook_id": wid, "cell": "Nope!A1"}),
                ("trace_cell", {"workbook_id": wid, "cell": "garbage"}),
                ("get_range", {"workbook_id": wid, "range": "Model!A1:Z100"}),
                ("get_range", {"workbook_id": wid, "range": "Nope!A1:B2"}),
                ("get_range", {"workbook_id": wid, "range": "???"}),
                ("audit_workbook", {"workbook_id": wid, "cursor": "999"}),
                ("audit_workbook", {"workbook_id": wid, "cursor": "abc"}),
                ("audit_workbook", {"workbook_id": wid, "checks": ["C99"]}),
                ("explain_issue", {"workbook_id": wid, "issue_id": "C01-0000000000"}),
                ("top_drivers", {"workbook_id": wid, "target_cell": "Assumptions!B2"}),
            ]
            for tool, args in bad:
                assert (await error(c, tool, args))["code"] == "invalid_argument", (tool, args)
            # schema-level validation by the SDK
            r = await c.call_tool("audit_workbook", {"workbook_id": wid, "limit": 1000})
            assert r.is_error

    run(scenario)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlink_escape_over_mcp(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy(FIXTURES / "reference" / "dcf.xlsx", tmp_path / "outside.xlsx")
    (root / "link.xlsx").symlink_to(tmp_path / "outside.xlsx")

    async def scenario() -> None:
        async with Client(build_server(root)) as c:
            assert (await error(c, "open_workbook", {"path": "link.xlsx"}))[
                "code"
            ] == "path_not_allowed"

    run(scenario)


def test_timeout_is_a_structured_error():
    limits = DEFAULT_LIMITS.model_copy(update={"call_timeout_s": 0.001})

    async def scenario() -> None:
        async with Client(build_server(FIXTURES, limits)) as c:
            err = await error(c, "open_workbook", {"path": "reference/loan.xlsx"})
            assert err["code"] == "timeout"

    run(scenario)


# ------------------------------------------------------------ size limits


def test_every_response_respects_size_cap():
    cap = 4_000
    limits = DEFAULT_LIMITS.model_copy(update={"max_response_bytes": cap})

    async def scenario() -> None:
        async with Client(build_server(FIXTURES, limits)) as c:
            sizes: list[int] = []

            async def sized(tool: str, args: dict[str, Any]) -> dict[str, Any]:
                out = await call(c, tool, args)
                sizes.append(len(json.dumps(out).encode()))
                return out

            o = await sized("open_workbook", {"path": "injection/traps.xlsx"})
            wid = o["workbook_id"]
            page = await sized("list_untrusted_content", {"workbook_id": wid, "limit": 100})
            assert page["next_cursor"] is not None  # had to paginate to fit
            await sized("get_range", {"workbook_id": wid, "range": "Summary!A1:H22"})
            loan = (await sized("open_workbook", {"path": "reference/loan.xlsx"}))["workbook_id"]
            t = await sized(
                "trace_cell", {"workbook_id": loan, "cell": "Schedule!F61", "max_depth": 20}
            )
            assert "response_size" in t["trace"]["truncation_reasons"]
            await sized("audit_workbook", {"workbook_id": wid, "limit": 100})
            assert max(sizes) <= cap * 1.05  # JSON spacing may differ slightly from Pydantic

    run(scenario)


# --------------------------------------------------------------- store unit


def test_store_lru_and_idle_timeout():
    now = [0.0]
    store = WorkbookStore(root=FIXTURES, max_open=2, idle_timeout_s=60, clock=lambda: now[0])
    a, _ = store.open("reference/dcf.xlsx")
    b, _ = store.open("reference/loan.xlsx")
    again, _ = store.open("reference/dcf.xlsx")
    assert again == a and len(store) == 2  # same content reuses the entry
    store.get(a)  # touch a; b is now least recently used
    c, _ = store.open("reference/budget.xlsx")
    assert len(store) == 2
    with pytest.raises(Exception, match="Unknown or expired"):
        store.get(b)
    now[0] = 1_000.0
    with pytest.raises(Exception, match="Unknown or expired"):
        store.get(c)  # idle timeout released it
    assert len(store) == 0
    assert store.close("missing") is False


def test_fit_and_paginate_helpers():
    big = wrap("x" * 5000, "S!A1 (cell text)")
    assert big.truncated  # envelope already caps at 2000
    small = fit(GetRangeResult(workbook_id="w", range="S!A1", cells=(), empty_cells_omitted=0), 100)
    assert json_size(small) < 200
    items = list(range(50))
    page = paginate(
        items,
        None,
        10,
        10_000,
        lambda p, n: CloseResult(workbook_id=str(p), ok=True, status="closed"),
    )
    assert page.workbook_id.startswith("(0, 1")
    with pytest.raises(Exception, match="Invalid cursor"):
        paginate(
            items,
            "-1",
            10,
            10_000,
            lambda p, n: CloseResult(workbook_id="x", ok=True, status="closed"),
        )
    tools = SheetGuardTools(WorkbookStore(root=FIXTURES))
    with pytest.raises(Exception, match="limit"):
        tools.list_untrusted_content("x", limit=0)


# ------------------------------------------------------------------ stdio


def test_real_stdio_session():
    """The same transport Claude Desktop uses: spawn the server, speak MCP over stdio."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "sheetguard.mcp.server", "--root", str(FIXTURES)],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
    )

    async def scenario() -> None:
        async with Client(params) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert names == TOOLS
            o = await call(c, "open_workbook", {"path": "injection/traps.xlsx"})
            wid = o["workbook_id"]
            for tool, args in [
                ("audit_workbook", {"workbook_id": wid}),
                ("trace_cell", {"workbook_id": wid, "cell": "Summary!F4"}),
                ("get_range", {"workbook_id": wid, "range": "Summary!A1:F4"}),
                ("list_untrusted_content", {"workbook_id": wid, "min_risk": "high"}),
            ]:
                await call(c, tool, args)
            issues = await call(c, "audit_workbook", {"workbook_id": wid})
            await call(
                c, "explain_issue", {"workbook_id": wid, "issue_id": issues["issues"][0]["id"]}
            )
            dcf = (await call(c, "open_workbook", {"path": "reference/dcf.xlsx"}))["workbook_id"]
            await call(c, "top_drivers", {"workbook_id": dcf, "target_cell": "Model!B12"})
            assert (await call(c, "close_workbook", {"workbook_id": wid}))["status"] == "closed"

    run(scenario)


def test_main_requires_root(capsys):
    with pytest.raises(SystemExit):
        main([])
    with pytest.raises(SystemExit):
        main(["--root", "/definitely/not/here"])
    assert "--root" in capsys.readouterr().err
