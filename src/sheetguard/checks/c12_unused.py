"""C12 - unused input (info).

Detection: numeric constants that no formula or defined name depends on.

False-positive policy:
* labels and text cells are ignored (spec);
* year and period labels (consecutive integers) are ignored;
* constants in sheets without a single formula are reported once per sheet
  (raw data tabs), and only if some formula elsewhere exists;
* elsewhere, one issue per vertical run of unused constants.
"""

from __future__ import annotations

from collections.abc import Iterable

from sheetguard.checks.base import (
    Check,
    CheckContext,
    cell_evidence,
    group_column_runs,
    make_issue,
    span_label,
)
from sheetguard.core.heuristics import is_index_like, row_label
from sheetguard.core.model import ValueType
from sheetguard.core.refs import CellKey
from sheetguard.report.schema import Confidence, Issue, Severity


class UnusedInput(Check):
    id = "C12"
    title = "Unused input"
    default_severity = Severity.INFO

    def run(self, ctx: CheckContext) -> Iterable[Issue]:
        if ctx.wb.formula_count == 0:
            return
        g = ctx.graph
        for sheet in ctx.wb.sheets.values():
            has_formulas = any(c.formula for c in sheet.cells.values())
            unused: list[CellKey] = []
            for (r, c), cell in sorted(sheet.cells.items()):
                if cell.formula is not None or cell.vtype is not ValueType.NUMBER:
                    continue
                key = CellKey(sheet.name, r, c)
                if g.has_dependents(key) or is_index_like(ctx.wb, key):
                    continue
                unused.append(key)
            if not unused:
                continue
            if not has_formulas:
                referenced = any(
                    g.has_dependents(CellKey(sheet.name, r, c)) for r, c in sheet.cells
                )
                if referenced:
                    continue  # a data tab that is used; leftovers there are normal
                yield make_issue(
                    self.id,
                    self.default_severity,
                    unused[0],
                    f"Sheet has {len(unused)} numeric value(s) and nothing in the workbook "
                    f"refers to it.",
                    evidence=[cell_evidence(ctx, k) for k in unused[:3]],
                    confidence=Confidence.MEDIUM,
                    affected_count=len(unused),
                    cell_label=f"{span_label(unused[:1])} (sheet)",
                    discriminator="sheet",
                )
                continue
            for run in group_column_runs(unused, g):
                label = row_label(ctx.wb, run[0])
                what = f" ('{label[:40]}')" if label and len(run) == 1 else ""
                yield make_issue(
                    self.id,
                    self.default_severity,
                    run[0],
                    f"{len(run)} numeric input(s){what} not used by any formula.",
                    evidence=[cell_evidence(ctx, k) for k in run[:3]],
                    confidence=Confidence.MEDIUM,
                    affected_count=len(run),
                    cell_label=span_label(run),
                )
