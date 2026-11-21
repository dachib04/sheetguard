"""C11 - hidden logic (warning).

Detection: formulas or numeric inputs that live in hidden sheets (hidden or
very hidden), hidden rows or hidden columns, and that other cells depend on.

False-positive policy: report, do not judge intent. Hidden helper rows are a
common, legitimate practice; the issue just makes them visible to the reviewer.
One issue per hidden sheet, and per hidden row or column elsewhere.
"""

from __future__ import annotations

from collections.abc import Iterable

from sheetguard.checks.base import Check, CheckContext, cell_evidence, make_issue
from sheetguard.core.model import ValueType
from sheetguard.core.refs import CellKey, col_letter
from sheetguard.report.schema import Confidence, Issue, Severity


class HiddenLogic(Check):
    id = "C11"
    title = "Hidden logic"
    default_severity = Severity.WARNING

    def run(self, ctx: CheckContext) -> Iterable[Issue]:
        g = ctx.graph
        groups: dict[tuple[str, str], list[CellKey]] = {}
        for key, cell in ctx.wb.iter_cells():
            if cell.formula is None and cell.vtype is not ValueType.NUMBER:
                continue
            sheet = ctx.wb.sheets[key.sheet]
            if not sheet.is_cell_hidden(key.row, key.col) or not g.has_dependents(key):
                continue
            if sheet.is_hidden:
                where = f"{'very hidden' if sheet.state == 'veryHidden' else 'hidden'} sheet"
            elif key.row in sheet.hidden_rows:
                where = f"hidden row {key.row}"
            else:
                where = f"hidden column {col_letter(key.col)}"
            groups.setdefault((key.sheet, where), []).append(key)
        for (_, where), keys in sorted(groups.items(), key=lambda kv: g.sort_key(kv[1][0])):
            n_formulas = sum(1 for k in keys if (c := ctx.wb.cell(k)) and c.formula)
            yield make_issue(
                self.id,
                self.default_severity,
                keys[0],
                f"{len(keys)} cell(s) in a {where} feed other calculations "
                f"({n_formulas} formula(s), {len(keys) - n_formulas} input(s)).",
                evidence=[cell_evidence(ctx, k) for k in keys[:3]],
                confidence=Confidence.HIGH,
                affected_count=len(keys),
                discriminator=where,
            )
