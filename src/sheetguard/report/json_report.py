"""JSON report (FR-40, NF-3).

Deterministic by construction: no timestamps or absolute paths, issues in the
stable order produced by ``run_checks``, keys sorted, UTF-8, trailing newline.
The same file and options always give byte-identical output.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal, cast

from sheetguard import __version__
from sheetguard.checks import REGISTRY
from sheetguard.core.refs import format_range
from sheetguard.report.schema import (
    Issue,
    OutputCandidate,
    Report,
    ReportOptions,
    Severity,
    SheetSummary,
    Summary,
    UntrustedFinding,
    UntrustedSummary,
    WorkbookFlags,
    WorkbookInfo,
)
from sheetguard.security.envelope import annotate
from sheetguard.security.injection import Finding

if TYPE_CHECKING:
    from sheetguard.api import Analysis

SheetState = Literal["visible", "hidden", "veryHidden"]


def build_report(
    analysis: Analysis,
    issues: Sequence[Issue],
    checks: Sequence[str] | None = None,
    min_severity: Severity = Severity.INFO,
) -> Report:
    wb = analysis.workbook
    ac = wb.active_content
    sheets = []
    for s in wb.sheets.values():
        state = s.state if s.state in ("visible", "hidden", "veryHidden") else "hidden"
        sheets.append(
            SheetSummary(
                name=s.name,
                index=s.index,
                state=cast(SheetState, state),
                used_range=format_range(s.used) if s.used else None,
                cells=len(s.cells),
                formulas=sum(1 for c in s.cells.values() if c.formula),
                hidden_rows=len(s.hidden_rows),
                hidden_columns=len(s.hidden_cols),
                protected=s.protected,
                tables=tuple(t.display_name for t in wb.tables if t.sheet == s.name),
            )
        )
    flags = WorkbookFlags(
        macros_present=ac.has_vba or ac.has_xlm_macros,
        vba=ac.has_vba,
        xlm_macros=ac.has_xlm_macros,
        external_links=ac.has_external_links
        or any(f.external for f in analysis.graph.flags.values()),
        dde=ac.has_dde,
        ole_objects=ac.has_ole_objects,
        activex=ac.has_activex,
        data_connections=ac.has_data_connections,
        hidden_sheets=sum(1 for s in wb.sheets.values() if s.state == "hidden"),
        very_hidden_sheets=sum(1 for s in wb.sheets.values() if s.state == "veryHidden"),
        cached_values=wb.has_cached_values,
        calc_mode=wb.calc_mode,
        iterative_calc=wb.iterate,
        date_1904=wb.date_1904,
        workbook_protected=wb.workbook_protected,
        untrusted_content_found=any(f.risk in ("medium", "high") for f in analysis.untrusted),
    )
    info = WorkbookInfo(
        file_name=wb.file_name,
        sha256=wb.sha256,
        size_bytes=wb.size_bytes,
        sheet_count=len(wb.sheets),
        cell_count=wb.cell_count,
        formula_count=wb.formula_count,
        defined_names=len(wb.names),
        tables=len(wb.tables),
        flags=flags,
        sheets=tuple(sheets),
    )
    by_sev = Counter(i.severity.value for i in issues)
    by_check = Counter(i.check_id for i in issues)
    summary = Summary(
        total=len(issues),
        by_severity={s.value: by_sev.get(s.value, 0) for s in Severity},
        by_check=dict(sorted(by_check.items())),
    )
    outputs = tuple(
        OutputCandidate(address=a, upstream_cells=n) for a, n in analysis.likely_outputs(10)
    )
    findings = tuple(to_model(f) for f in analysis.untrusted)
    untrusted_summary = UntrustedSummary(
        total=len(findings),
        by_risk={r: sum(1 for f in findings if f.risk == r) for r in ("high", "medium", "low")},
        by_vector=dict(sorted(Counter(f.vector for f in findings).items())),
    )
    report = Report(
        tool_version=__version__,
        workbook=info,
        untrusted_summary=untrusted_summary,
        untrusted_content=findings,
        options=ReportOptions(
            checks=tuple(sorted(checks) if checks else sorted(REGISTRY)), min_severity=min_severity
        ),
        summary=summary,
        likely_outputs=outputs,
        issues=tuple(issues),
    )
    return annotate(report, analysis.untrusted_index)


def to_model(f: Finding, max_len: int = 500) -> UntrustedFinding:
    risk = f.risk if f.risk != "none" else "low"
    return UntrustedFinding(
        id=f.id,
        vector=f.vector,
        location=f.location,
        risk=risk,
        score=f.score,
        signals=tuple(f.signals),
        reasons=tuple(f.reasons),
        text=f.envelope(max_len),
    )


def to_json(report: Report) -> str:
    data = report.model_dump(mode="json")
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def report_json_schema() -> str:
    return json.dumps(Report.model_json_schema(), sort_keys=True, indent=2) + "\n"
