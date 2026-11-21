"""C04 - error values (critical).

A cached #REF!, #DIV/0!, #N/A, #VALUE!, #NAME?, #NUM! or #NULL! is reported
once at its *root* (the cell whose precedents are error-free); the cells it
propagates to are counted in ``affected_count`` instead of becoming hundreds of
separate issues.

False-positive policy:
* #N/A that every dependent absorbs with IFERROR/IFNA/ISNA, or a deliberate
  ``NA()`` with no dependents (chart gaps), is ``info``;
* other errors fully absorbed downstream are ``warning``;
* a literal ``#REF!`` inside a formula is always critical, even when the cached
  value is masked or missing;
* error values on cells that are part of a circular reference, or that only flow
  out of one, are left to C05.
"""

from __future__ import annotations

from collections.abc import Iterable

from sheetguard.checks.base import (
    Check,
    CheckContext,
    cell_evidence,
    dependent_cells,
    make_issue,
    precedent_cells,
)
from sheetguard.core.model import ValueType
from sheetguard.core.refs import CellKey, analyze_formula, format_cell
from sheetguard.report.schema import Confidence, Evidence, Issue, Severity

_ABSORBERS = frozenset({"IFERROR", "IFNA", "ISNA", "ISERROR", "ISERR", "ERROR.TYPE"})
_SAMPLE = 5


class ErrorValues(Check):
    id = "C04"
    title = "Error values"
    default_severity = Severity.CRITICAL

    def run(self, ctx: CheckContext) -> Iterable[Issue]:
        wb, graph = ctx.wb, ctx.graph
        # Cells inside a cycle, and errors flowing out of one, exist *because* of the
        # cycle; C05 reports it. Only errors with a non-cyclic origin are C04 roots.
        in_cycle = {k for cycle in graph.find_cycles() for k in cycle}
        all_errors = {k for k, c in wb.iter_cells() if c.vtype is ValueType.ERROR}
        downstream_of_cycle: set[CellKey] = set()
        stack = [k for k in in_cycle]
        while stack:
            k = stack.pop()
            for d in dependent_cells(graph, k):
                if d in all_errors and d not in in_cycle and d not in downstream_of_cycle:
                    downstream_of_cycle.add(d)
                    stack.append(d)
        errors = all_errors - in_cycle - downstream_of_cycle

        roots = [
            k
            for k in sorted(errors, key=graph.sort_key)
            if not any(p in all_errors for p in precedent_cells(graph, k))
        ]
        assigned: dict[CellKey, CellKey] = {}
        for root in roots:
            stack = [root]
            while stack:
                k = stack.pop()
                if k in assigned:
                    continue
                assigned[k] = root
                stack.extend(d for d in dependent_cells(graph, k) if d in errors)
        # Errors whose precedents are errors but which no root reaches (e.g. fed by an
        # error constant on another path) are still reported, as their own roots.
        for k in sorted(errors - set(assigned), key=graph.sort_key):
            roots.append(k)
            assigned[k] = k

        flagged: set[CellKey] = set()
        for root in roots:
            cell = wb.cell(root)
            assert cell is not None
            code = str(cell.value)
            affected = sorted(
                (k for k, r in assigned.items() if r == root and k != root), key=graph.sort_key
            )
            severity, why = self._severity(ctx, root, code)
            evidence: list[Evidence] = [cell_evidence(ctx, root, note="root error")]
            evidence += [cell_evidence(ctx, k, note="propagated") for k in affected[:_SAMPLE]]
            spread = f" It propagates to {len(affected)} other cell(s)." if affected else ""
            yield make_issue(
                self.id,
                severity,
                root,
                f"Cell shows {code}{why}.{spread}",
                evidence=evidence,
                confidence=Confidence.HIGH,
                affected_count=1 + len(affected),
                discriminator=code,
            )
            flagged.add(root)

        for key, cell in wb.iter_formula_cells():
            if key in flagged or cell.formula is None:
                continue
            if "#REF!" in analyze_formula(cell.formula).errors:
                yield make_issue(
                    self.id,
                    Severity.CRITICAL,
                    key,
                    "Formula contains a broken reference (#REF!); a referenced cell or sheet "
                    "was deleted.",
                    evidence=[cell_evidence(ctx, key)],
                    discriminator="literal#REF!",
                )

    def _severity(self, ctx: CheckContext, root: CellKey, code: str) -> tuple[Severity, str]:
        graph, wb = ctx.graph, ctx.wb
        cell = wb.cell(root)
        deps = dependent_cells(graph, root)
        if not deps:
            if (
                code == "#N/A"
                and cell is not None
                and cell.formula is not None
                and "NA" in analyze_formula(cell.formula).functions
            ):
                return Severity.INFO, " (deliberate NA(), nothing depends on it)"
            return Severity.CRITICAL, ""
        absorbed = True
        for d in deps:
            dc = wb.cell(d)
            if dc is None or dc.vtype is ValueType.ERROR or dc.formula is None:
                absorbed = False
                break
            if not (set(analyze_formula(dc.formula).functions) & _ABSORBERS):
                absorbed = False
                break
        if absorbed:
            where = ", ".join(format_cell(d) for d in deps[:3])
            if code == "#N/A":
                return Severity.INFO, f" (handled by IFERROR/IFNA in {where})"
            return Severity.WARNING, f" (masked by IFERROR in {where})"
        return Severity.CRITICAL, ""
