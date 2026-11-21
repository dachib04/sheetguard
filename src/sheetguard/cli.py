"""Command-line interface (Typer). Thin: all logic lives in ``sheetguard.api``.

Exit codes (FR-43): 0 no issues at or above the threshold, 1 issues found,
2 input error, 3 internal error.
"""

from __future__ import annotations

import json
import logging
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from sheetguard import __version__
from sheetguard.api import analyze
from sheetguard.errors import InvalidArgumentError, SheetGuardError
from sheetguard.report.html_report import to_html
from sheetguard.report.json_report import report_json_schema, to_json
from sheetguard.report.schema import Severity

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Read-only auditing of Excel models. Never modifies a workbook.",
)

EXIT_OK, EXIT_ISSUES, EXIT_INPUT, EXIT_INTERNAL = 0, 1, 2, 3
log = logging.getLogger("sheetguard.cli")


class Format(StrEnum):
    TEXT = "text"
    JSON = "json"
    HTML = "html"


def _fail(exc: Exception) -> None:
    if isinstance(exc, SheetGuardError):
        typer.echo(f"error [{exc.code.value}]: {exc.message}", err=True)
        raise typer.Exit(EXIT_INPUT)
    log.debug("internal error", exc_info=exc)
    typer.echo("error [internal]: unexpected failure (run with --debug for details)", err=True)
    raise typer.Exit(EXIT_INTERNAL)


def _emit(text: str, out: Path | None, source: Path) -> None:
    if out is None:
        typer.echo(text, nl=False)
        return
    # S-1: the CLI writes reports only, never workbooks, and never over its input.
    if out.suffix.lower() in {".xlsx", ".xlsm", ".xls", ".xlsb"}:
        raise InvalidArgumentError("Refusing to write a report to a workbook file name.")
    if out.exists() and source.exists() and out.resolve() == source.resolve():
        raise InvalidArgumentError("Refusing to overwrite the input file.")
    out.write_text(text, encoding="utf-8")
    typer.echo(f"wrote {out}", err=True)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"sheetguard {__version__}")
        raise typer.Exit(EXIT_OK)


@app.callback()
def main(
    debug: Annotated[bool, typer.Option(help="Verbose logs (may include cell contents).")] = False,
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version, is_eager=True, help="Show the version and exit."
        ),
    ] = False,
) -> None:
    logging.basicConfig(level=logging.DEBUG if debug else logging.WARNING, stream=sys.stderr)


@app.command()
def audit(
    file: Annotated[Path, typer.Argument(help=".xlsx or .xlsm workbook")],
    fmt: Annotated[Format, typer.Option("--format", "-f", help="Output format")] = Format.TEXT,
    as_json: Annotated[bool, typer.Option("--json", help="Shortcut for --format json")] = False,
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Write the report here")] = None,
    min_severity: Annotated[
        Severity, typer.Option(help="Lowest severity to report")
    ] = Severity.INFO,
    fail_on: Annotated[
        Severity, typer.Option(help="Exit 1 if issues at/above this")
    ] = Severity.CRITICAL,
    check: Annotated[list[str] | None, typer.Option(help="Run only these check ids")] = None,
) -> None:
    """Audit a workbook and report the issues found."""
    try:
        analysis = analyze(file)
        report = analysis.report(checks=check, min_severity=min_severity)
        fmt = Format.JSON if as_json else fmt
        if fmt is Format.JSON:
            _emit(to_json(report), out, file)
        elif fmt is Format.HTML:
            _emit(to_html(report), out, file)
        else:
            _emit(_text(report), out, file)
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(exc)
        return
    blocking = [i for i in report.issues if i.severity.rank <= fail_on.rank]
    raise typer.Exit(EXIT_ISSUES if blocking else EXIT_OK)


def _text(report: object) -> str:
    from sheetguard.report.schema import Report

    assert isinstance(report, Report)
    lines: list[str] = []
    s = report.summary.by_severity
    lines.append(
        f"{report.workbook.file_name}: {report.summary.total} issue(s) - "
        f"{s['critical']} critical, {s['warning']} warning, {s['info']} info"
    )
    for i in report.issues:
        lines.append(f"[{i.severity.value:8}] {i.check_id} {i.cell}: {i.description}")
        if i.suggested_fix is not None:
            lines.append(f"           suggested: {i.suggested_fix.text}")
    return "\n".join(lines) + "\n"


@app.command(name="trace")
def trace_cmd(
    file: Annotated[Path, typer.Argument(help=".xlsx or .xlsm workbook")],
    cell: Annotated[str, typer.Argument(help="Cell such as Sheet1!B7 or 'My Sheet'!C3")],
    dependents: Annotated[bool, typer.Option(help="Trace dependents instead")] = False,
    max_depth: Annotated[int, typer.Option(min=1, max=50)] = 6,
    max_nodes: Annotated[int, typer.Option(min=1, max=10_000)] = 500,
) -> None:
    """Show the precedents (or dependents) of a cell as JSON."""
    try:
        analysis = analyze(file)
        result = analysis.trace(
            cell, "dependents" if dependents else "precedents", max_depth, max_nodes
        )
    except Exception as exc:
        _fail(exc)
        return
    typer.echo(
        json.dumps(
            result.model_dump(mode="json", exclude_none=True),
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
        )
    )


@app.command()
def drivers(
    file: Annotated[Path, typer.Argument(help=".xlsx or .xlsm workbook")],
    target: Annotated[str, typer.Argument(help="Output cell, e.g. Model!B12")],
    n: Annotated[
        int, typer.Option("--top", "-n", min=1, max=200, help="How many drivers to show")
    ] = 10,
    delta_pct: Annotated[float, typer.Option(help="Perturbation in percent")] = 10.0,
    zero_delta: Annotated[float, typer.Option(help="Absolute delta for inputs equal to 0")] = 1.0,
    include_index: Annotated[bool, typer.Option(help="Also perturb year/period labels")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Rank the inputs that move TARGET the most (+/- delta, recalculated)."""
    try:
        analysis = analyze(file)
        result = analysis.drivers(target, n, delta_pct, zero_delta, include_index)
    except Exception as exc:
        _fail(exc)
        return
    if as_json:
        typer.echo(
            json.dumps(result.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=False)
        )
        return
    if result.partial:
        funcs = ", ".join(result.unsupported_functions) or "-"
        typer.echo(
            f"{result.target}: partial result, no numbers returned. Unsupported: "
            f"{funcs}. {'; '.join(result.partial_reasons)}"
        )
        return
    typer.echo(
        f"{result.target} = {result.baseline} (recalculated; {result.inputs_analysed} "
        f"inputs, +/-{result.delta_pct:g}%)"
    )
    for rank, d in enumerate(result.drivers, start=1):
        pct = f"{d.pct_of_baseline:.2f}%" if d.pct_of_baseline is not None else "-"
        zero = " (input was 0, absolute delta)" if d.zero_input else ""
        typer.echo(
            f"{rank:3}. {d.input:<28} {d.input_value:>14g}  moves target by "
            f"{d.abs_change:,.4g} ({pct}){zero}"
        )
    if result.index_inputs_skipped:
        typer.echo(
            f"     ({result.index_inputs_skipped} year/period labels skipped; "
            f"use --include-index to analyse them)"
        )
    if result.capped:
        typer.echo("     (input cap reached; not every input was analysed)")


@app.command()
def outputs(
    file: Annotated[Path, typer.Argument(help=".xlsx or .xlsm workbook")],
    limit: Annotated[int, typer.Option(min=1, max=100)] = 10,
) -> None:
    """List likely model outputs (good targets for `drivers`)."""
    try:
        analysis = analyze(file)
    except Exception as exc:
        _fail(exc)
        return
    for address, count in analysis.likely_outputs(limit):
        typer.echo(f"{address}\t{count} cells feed it")


@app.command()
def mcp(
    root: Annotated[Path, typer.Option(help="Folder the server may read workbooks from")],
    max_open: Annotated[int, typer.Option(min=1, max=100)] = 8,
    idle_timeout: Annotated[
        float, typer.Option(help="Seconds before an idle workbook is released")
    ] = 900.0,
) -> None:
    """Run the MCP server over stdio (for Claude Desktop, Claude Code and other clients)."""
    try:
        from sheetguard.mcp.server import main as mcp_main
    except ImportError:  # pragma: no cover - only without the [mcp] extra
        typer.echo("error: the MCP server needs: pip install 'sheetguard[mcp]'", err=True)
        raise typer.Exit(EXIT_INPUT) from None
    mcp_main(
        ["--root", str(root), "--max-open", str(max_open), "--idle-timeout", str(idle_timeout)]
    )


@app.command()
def schema() -> None:
    """Print the JSON Schema of the audit report."""
    typer.echo(report_json_schema(), nl=False)


if __name__ == "__main__":  # pragma: no cover
    app()
