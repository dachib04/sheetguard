"""C10 - possible unit or scale mismatch (info, low confidence by design).

Detection:
* a constant formatted as a percentage whose value is above 1 in magnitude
  (25 shown as 2500% usually means 25% was typed as a whole number);
* a SUM over constants whose magnitudes differ by 10^5 or more, or whose
  number formats use different scales (``#,##0,`` thousands vs ``#,##0`` units).

False-positive policy: always info and low confidence; one issue per column
run for percentages, one per SUM for scales.
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
from sheetguard.core.model import ValueType
from sheetguard.core.refs import AreaRef, CellKey, parse_operand, walk
from sheetguard.report.schema import Confidence, Issue, Severity

RATIO = 1e5


def _scale(fmt: str) -> int:
    """Number of trailing thousands commas in a number format (#,##0,, = millions)."""
    core = fmt.split(";")[0].replace('"', "")
    stripped = core.rstrip("_)%*- ")
    return len(stripped) - len(stripped.rstrip(","))


class UnitMismatch(Check):
    id = "C10"
    title = "Possible unit or scale mismatch"
    default_severity = Severity.INFO

    def run(self, ctx: CheckContext) -> Iterable[Issue]:
        pct: list[CellKey] = []
        for key, cell in ctx.wb.iter_cells():
            if (
                cell.formula is None
                and cell.vtype is ValueType.NUMBER
                and "%" in cell.number_format
                and isinstance(cell.value, int | float)
                and not isinstance(cell.value, bool)
                and abs(cell.value) > 1
            ):
                pct.append(key)
        for run in group_column_runs(pct, ctx.graph):
            yield make_issue(
                self.id,
                self.default_severity,
                run[0],
                "Percent-formatted value above 100%: it may have been typed as a whole number "
                "(25 instead of 25%).",
                evidence=[cell_evidence(ctx, k) for k in run[:3]],
                confidence=Confidence.LOW,
                affected_count=len(run),
                cell_label=span_label(run),
            )
        for key, cell in ctx.wb.iter_formula_cells():
            assert cell.formula is not None
            for t in walk(cell.formula) or []:
                if t.function != "SUM" or t.token.subtype != "RANGE":
                    continue
                ref = parse_operand(t.token.value)
                if not isinstance(ref, AreaRef) or ref.is_cell or ref.sheet_end:
                    continue
                sheet_name = ctx.wb.sheet_name(ref.sheet) if ref.sheet else key.sheet
                if sheet_name is None:
                    continue
                sheet = ctx.wb.sheets[sheet_name]
                consts = [
                    c
                    for c in sheet.cells_in(ref.r1, ref.c1, ref.r2, ref.c2)
                    if c.formula is None
                    and c.vtype is ValueType.NUMBER
                    and isinstance(c.value, int | float)
                    and not isinstance(c.value, bool)
                ]
                nums = [abs(float(c.value)) for c in consts if c.value]
                scales = {_scale(c.number_format) for c in consts}
                reason = None
                if len(nums) >= 3 and max(nums) / min(nums) >= RATIO:
                    reason = (
                        f"values in the summed range differ by a factor of "
                        f"{max(nums) / min(nums):,.0f}"
                    )
                elif len(scales) > 1:
                    reason = "summed cells use number formats with different scales"
                if reason:
                    yield make_issue(
                        self.id,
                        self.default_severity,
                        key,
                        f"Possible mixed units in a SUM: {reason}.",
                        evidence=[cell_evidence(ctx, key)],
                        confidence=Confidence.LOW,
                        discriminator=t.token.value,
                    )
