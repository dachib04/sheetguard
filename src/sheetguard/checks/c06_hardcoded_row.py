"""C06 - hardcoded value in a formula row (critical).

Detection: in a contiguous run of numeric cells (row or column), at least 3
formulas share one relative R1C1 pattern (and at least 60% of the formulas),
and a numeric *constant* sits among them: a calculation was probably
overwritten with its value.

False-positive policy:
* requires 3+ formula neighbours with the same pattern (spec);
* the first cell is ignored when the pattern rolls forward from the previous
  cell (``C[-1]`` / ``R[-1]``): that is the opening value / seed input;
* year and period labels (runs of consecutive integers) are ignored;
* a constant at the edge of a run gets low confidence; one surrounded by
  pattern formulas on both sides gets high confidence;
* a run with more constants than one in four cells is a data block, not a
  formula row, and is skipped.

Fix (FR-30): the nearest consistent neighbour's formula for this position.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from sheetguard.checks.base import Check, CheckContext, cell_evidence, make_issue
from sheetguard.core.fixes import from_neighbour
from sheetguard.core.heuristics import is_index_like
from sheetguard.core.model import Cell, Sheet, ValueType
from sheetguard.core.refs import CellKey, a1, to_r1c1
from sheetguard.report.schema import Confidence, Issue, Severity
from sheetguard.security.envelope import wrap


def _numeric(cell: Cell) -> bool:
    return cell.formula is not None or cell.vtype is ValueType.NUMBER


def _runs(sheet: Sheet, axis: str) -> list[list[tuple[int, int]]]:
    positions = sorted(
        (pos for pos, c in sheet.cells.items() if _numeric(c)),
        key=(lambda p: (p[0], p[1])) if axis == "row" else (lambda p: (p[1], p[0])),
    )
    runs: list[list[tuple[int, int]]] = []
    for pos in positions:
        if runs:
            last = runs[-1][-1]
            same_line = last[0] == pos[0] if axis == "row" else last[1] == pos[1]
            step = pos[1] - last[1] if axis == "row" else pos[0] - last[0]
            if same_line and step == 1:
                runs[-1].append(pos)
                continue
        runs.append([pos])
    return runs


class HardcodedInFormulaRow(Check):
    id = "C06"
    title = "Hardcoded value in a formula row"
    default_severity = Severity.CRITICAL

    def run(self, ctx: CheckContext) -> Iterable[Issue]:
        found: dict[CellKey, tuple[Confidence, Issue]] = {}
        need = ctx.config.min_consistent_neighbours
        for sheet in ctx.wb.sheets.values():
            for axis in ("row", "column"):
                for run in _runs(sheet, axis):
                    self._judge(ctx, sheet, run, axis, need, found)
        yield from (issue for _, issue in found.values())

    def _judge(
        self,
        ctx: CheckContext,
        sheet: Sheet,
        run: list[tuple[int, int]],
        axis: str,
        need: int,
        found: dict[CellKey, tuple[Confidence, Issue]],
    ) -> None:
        if len(run) < need + 1:
            return
        cells = [sheet.cells[p] for p in run]
        patterns = {
            p: to_r1c1(str(c.formula), *p)
            for p, c in zip(run, cells, strict=True)
            if c.formula is not None
        }
        constants = [i for i, c in enumerate(cells) if c.formula is None]
        if not constants or not patterns:
            return
        majority, k = sorted(Counter(patterns.values()).items(), key=lambda kv: (-kv[1], kv[0]))[0]
        if k < need or k / len(patterns) < 0.6:
            return
        rolls = ("C[-1]" in majority) if axis == "row" else ("R[-1]" in majority)
        # The seed of a roll-forward row is an input, not a paste: leave it out of the
        # count, otherwise seed + one real paste in a 5-period row looks like a data block.
        if rolls and constants[0] == 0:
            constants = constants[1:]
        if not constants or len(constants) > max(1, len(run) // 4):
            return
        for i in constants:
            pos = run[i]
            key = CellKey(sheet.name, *pos)
            if is_index_like(ctx.wb, key):
                continue
            left = patterns.get(run[i - 1]) == majority if i > 0 else False
            right = patterns.get(run[i + 1]) == majority if i + 1 < len(run) else False
            if not (left or right):
                continue
            confidence = (
                Confidence.HIGH
                if left and right
                else (Confidence.LOW if i in (0, len(run) - 1) else Confidence.MEDIUM)
            )
            prev = found.get(key)
            if prev is not None and _rank(prev[0]) <= _rank(confidence):
                continue
            neighbour = next(
                (
                    run[j]
                    for d in range(1, len(run))
                    for j in (i - d, i + d)
                    if 0 <= j < len(run) and patterns.get(run[j]) == majority
                ),
                None,
            )
            fix = None
            if neighbour is not None:
                text = from_neighbour(str(sheet.cells[neighbour].formula), a1(*neighbour), a1(*pos))
                if text is not None:
                    fix = wrap(text, f"suggested formula for {ctx.wb.address(key)}")
            line = f"row {pos[0]}" if axis == "row" else f"column {pos[1]}"
            evidence = [cell_evidence(ctx, key, note="hardcoded value")]
            if neighbour is not None:
                evidence.append(
                    cell_evidence(ctx, CellKey(sheet.name, *neighbour), note="consistent neighbour")
                )
            issue = make_issue(
                self.id,
                self.default_severity,
                key,
                f"Constant {cells[i].value!r} sits among {k} formulas with the same pattern in "
                f"{line}; it may be an overwritten calculation.",
                evidence=evidence,
                confidence=confidence,
                suggested_fix=fix,
            )
            found[key] = (confidence, issue)


def _rank(c: Confidence) -> int:
    return {Confidence.HIGH: 0, Confidence.MEDIUM: 1, Confidence.LOW: 2}[c]
