"""C05 - circular reference (critical).

Each strongly connected component of the dependency graph is one issue.

False-positive policy: when the workbook has iterative calculation enabled the
cycle is probably intentional (interest on average balance, for example), so
it is reported as ``warning`` instead of ``critical``.
"""

from __future__ import annotations

from collections.abc import Iterable

from sheetguard.checks.base import Check, CheckContext, cell_evidence, make_issue
from sheetguard.core.refs import format_cell
from sheetguard.report.schema import Confidence, Issue, Severity

_SHOW = 10


class CircularReference(Check):
    id = "C05"
    title = "Circular reference"
    default_severity = Severity.CRITICAL

    def run(self, ctx: CheckContext) -> Iterable[Issue]:
        iterative = ctx.wb.iterate
        for cycle in ctx.graph.find_cycles():
            first = cycle[0]
            path = " -> ".join(format_cell(k) for k in cycle[:_SHOW])
            more = f" (+{len(cycle) - _SHOW} more)" if len(cycle) > _SHOW else ""
            if len(cycle) == 1:
                what = "Cell refers to itself"
            else:
                what = f"Circular reference through {len(cycle)} cells: {path}{more}"
            note = (
                " Iterative calculation is enabled, so this may be intentional."
                if iterative
                else ""
            )
            yield make_issue(
                self.id,
                Severity.WARNING if iterative else Severity.CRITICAL,
                first,
                f"{what}.{note}",
                evidence=[cell_evidence(ctx, k) for k in cycle[:_SHOW]],
                confidence=Confidence.HIGH,
                affected_count=len(cycle),
                discriminator="|".join(format_cell(k) for k in cycle),
            )
