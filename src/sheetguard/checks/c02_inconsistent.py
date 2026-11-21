"""C02 - inconsistent formula in a row or column (critical).

Approach: within each contiguous run of formula cells in a row (and in a
column), render every formula in relative R1C1 form. If one pattern is shared
by at least 3 cells and by at least 60% of the run, every cell with a different
pattern is an outlier.

False-positive policy (documented in docs/checks.md):
* the first and last cell of a run are ignored when they are totals
  (they aggregate a range that covers other cells of the same run);
* the first cell is ignored when the majority pattern refers to the previous
  cell along the run (``C[-1]`` / ``R[-1]``): a roll-forward pattern cannot
  apply to the opening period, so opening balances are not outliers;
* outliers at the edge of a run get low confidence (opening balances and
  first-period formulas often differ on purpose);
* a cell flagged in both directions is reported once.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

from sheetguard.checks.base import Check, CheckContext, cell_evidence, make_issue
from sheetguard.core.fixes import from_neighbour
from sheetguard.core.refs import AreaRef, CellKey, a1, analyze_formula, to_r1c1
from sheetguard.report.schema import Confidence, Issue, Severity
from sheetguard.security.envelope import wrap

_AGGREGATES = re.compile(r"\b(SUM|SUBTOTAL|AGGREGATE|AVERAGE|COUNT|COUNTA|MIN|MAX|SUMPRODUCT)\(")
MIN_RUN = 4
MIN_SHARE = 0.6  # rows: time series, expected to be homogeneous
MIN_SHARE_COLUMN = 0.75  # columns: stacked line items, naturally more varied


def _runs(positions: list[int]) -> list[list[int]]:
    runs: list[list[int]] = []
    for p in positions:
        if runs and p == runs[-1][-1] + 1:
            runs[-1].append(p)
        else:
            runs.append([p])
    return runs


def suggest_from_neighbour(formula: str, origin: str, dest: str) -> str | None:
    """Kept for backwards compatibility; see ``core.fixes.from_neighbour``."""
    return from_neighbour(formula, origin, dest)


class InconsistentFormula(Check):
    id = "C02"
    title = "Inconsistent formula in a row or column"
    default_severity = Severity.CRITICAL

    def run(self, ctx: CheckContext) -> Iterable[Issue]:
        found: dict[CellKey, Issue] = {}
        for sheet in ctx.wb.sheets.values():
            formulas = {pos: c.formula for pos, c in sheet.cells.items() if c.formula is not None}
            by_row: dict[int, list[int]] = {}
            by_col: dict[int, list[int]] = {}
            for r, c in formulas:
                by_row.setdefault(r, []).append(c)
                by_col.setdefault(c, []).append(r)
            for r, cols in sorted(by_row.items()):
                for run in _runs(sorted(cols)):
                    cells = [(r, c) for c in run]
                    self._judge(ctx, sheet.name, cells, formulas, "row", found)
            for c, rows in sorted(by_col.items()):
                for run in _runs(sorted(rows)):
                    cells = [(r, c) for r in run]
                    self._judge(ctx, sheet.name, cells, formulas, "column", found)
        yield from found.values()

    def _judge(
        self,
        ctx: CheckContext,
        sheet: str,
        cells: list[tuple[int, int]],
        formulas: dict[tuple[int, int], str],
        axis: str,
        found: dict[CellKey, Issue],
    ) -> None:
        # Totals at either end are not part of the pattern being compared.
        if len(cells) >= 2 and _is_total(str(formulas[cells[-1]]), cells):
            cells = cells[:-1]
        if len(cells) >= 2 and _is_total(str(formulas[cells[0]]), cells):
            cells = cells[1:]
        if len(cells) < MIN_RUN:
            return
        patterns = {pos: to_r1c1(str(formulas[pos]), pos[0], pos[1]) for pos in cells}
        counts = Counter(patterns.values())
        majority, k = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        need = ctx.config.min_consistent_neighbours
        share = MIN_SHARE if axis == "row" else MIN_SHARE_COLUMN
        if k < need or k / len(cells) < share or k == len(cells):
            return
        rolls_forward = ("C[-1]" in majority) if axis == "row" else ("R[-1]" in majority)
        for i, pos in enumerate(cells):
            pattern = patterns[pos]
            if pattern == majority:
                continue
            edge = i in (0, len(cells) - 1)
            if i == 0 and rolls_forward:
                continue  # opening period of a roll-forward (prior-period) pattern
            key = CellKey(sheet, *pos)
            confidence = (
                Confidence.LOW
                if edge
                else (Confidence.HIGH if k / len(cells) >= 0.8 else Confidence.MEDIUM)
            )
            prev = found.get(key)
            if prev is not None and _rank(prev.confidence) <= _rank(confidence):
                continue
            neighbour = _nearest(cells, i, patterns, majority)
            fix = None
            if neighbour is not None:
                text = suggest_from_neighbour(str(formulas[neighbour]), a1(*neighbour), a1(*pos))
                if text is not None:
                    fix = wrap(text, f"suggested formula for {sheet}!{a1(*pos)}")
            evidence = [cell_evidence(ctx, key, note="outlier")]
            examples = [p for p in cells if patterns[p] == majority][:2]
            evidence += [
                cell_evidence(ctx, CellKey(sheet, *p), note="consistent neighbour")
                for p in examples
            ]
            line = f"row {pos[0]}" if axis == "row" else f"column {key.col}"
            found[key] = make_issue(
                self.id,
                self.default_severity,
                key,
                f"Formula differs from the pattern shared by {k} of {len(cells)} cells in {line}.",
                evidence=evidence,
                confidence=confidence,
                suggested_fix=fix,
            )


def _is_total(formula: str, run: list[tuple[int, int]]) -> bool:
    """True if the formula aggregates a range that covers 2+ other cells of the run."""
    if not _AGGREGATES.search(formula.upper()):
        return False
    members = set(run)
    for ref in analyze_formula(formula).refs:
        if isinstance(ref, AreaRef) and ref.sheet is None and not ref.is_cell:
            covered = sum(1 for r, c in members if ref.r1 <= r <= ref.r2 and ref.c1 <= c <= ref.c2)
            if covered >= 2:
                return True
    return False


def _rank(c: Confidence) -> int:
    return {Confidence.HIGH: 0, Confidence.MEDIUM: 1, Confidence.LOW: 2}[c]


def _nearest(
    cells: list[tuple[int, int]], i: int, patterns: dict[tuple[int, int], str], majority: str
) -> tuple[int, int] | None:
    for dist in range(1, len(cells)):
        for j in (i - dist, i + dist):
            if 0 <= j < len(cells) and patterns[cells[j]] == majority:
                return cells[j]
    return None
