"""C09 - volatile or fragile functions (info).

Detection: NOW, TODAY, RAND, RANDBETWEEN (results change on every recalculation)
and INDIRECT, OFFSET (references built at run time, so the static dependency
graph is incomplete; traces through these cells are marked partial).

False-positive policy: informational by design; one issue per formula pattern.
"""

from __future__ import annotations

from collections.abc import Iterable

from sheetguard.checks.base import (
    Check,
    CheckContext,
    cell_evidence,
    formula_r1c1,
    group_by_signature,
    make_issue,
)
from sheetguard.core.refs import CellKey, analyze_formula
from sheetguard.report.schema import Confidence, Issue, Severity


class VolatileFunctions(Check):
    id = "C09"
    title = "Volatile or fragile functions"
    default_severity = Severity.INFO

    def run(self, ctx: CheckContext) -> Iterable[Issue]:
        items: list[tuple[CellKey, str]] = []
        funcs: dict[CellKey, list[str]] = {}
        for key, cell in ctx.wb.iter_formula_cells():
            assert cell.formula is not None
            vol = analyze_formula(cell.formula).volatile_functions
            if vol:
                funcs[key] = vol
                items.append((key, formula_r1c1(key, cell.formula)))
        for group in group_by_signature(items, ctx.graph):
            key = group[0]
            names = funcs[key]
            dynamic = [f for f in names if f in ("INDIRECT", "OFFSET")]
            why = (
                " Its references are built at run time, so traces through it are partial."
                if dynamic
                else " Its result changes every time the workbook recalculates."
            )
            more = f" Same pattern in {len(group) - 1} more cell(s)." if len(group) > 1 else ""
            yield make_issue(
                self.id,
                self.default_severity,
                key,
                f"Uses {', '.join(names)}.{why}{more}",
                evidence=[cell_evidence(ctx, k) for k in group[:3]],
                confidence=Confidence.HIGH,
                affected_count=len(group),
                discriminator=",".join(names),
            )
