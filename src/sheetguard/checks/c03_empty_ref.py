"""C03 - reference to an empty cell (warning).

Detection: a formula refers directly to a single cell that has no value and no
formula. (A cell holding "" is not empty.)

False-positive policy:
* references inside SUM/COUNT-style aggregates, IS* tests, COUNTBLANK, N, T,
  CONCAT/TEXTJOIN are ignored: empties are normal there;
* references through ranges are never flagged (ranges are expected to have gaps);
* references to missing sheets are left to the graph (reported as partial);
* the same formula pattern pointing at empty cells is one issue.
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
from sheetguard.core.refs import AreaRef, CellKey, format_cell, parse_operand, walk
from sheetguard.report.schema import Confidence, Issue, Severity

TOLERANT = frozenset(
    {
        "SUM",
        "SUMIF",
        "SUMIFS",
        "SUMPRODUCT",
        "COUNT",
        "COUNTA",
        "COUNTIF",
        "COUNTIFS",
        "COUNTBLANK",
        "AVERAGE",
        "AVERAGEA",
        "AVERAGEIF",
        "AVERAGEIFS",
        "MAX",
        "MAXA",
        "MIN",
        "MINA",
        "MAXIFS",
        "MINIFS",
        "PRODUCT",
        "SUBTOTAL",
        "AGGREGATE",
        "CONCAT",
        "CONCATENATE",
        "TEXTJOIN",
        "ISBLANK",
        "ISNUMBER",
        "ISTEXT",
        "ISERROR",
        "ISERR",
        "ISNA",
        "ISLOGICAL",
        "ISNONTEXT",
        "N",
        "T",
        "LEN",
        "MEDIAN",
        "STDEV",
        "STDEV.S",
        "STDEV.P",
        "VAR",
    }
)


class EmptyReference(Check):
    id = "C03"
    title = "Reference to an empty cell"
    default_severity = Severity.WARNING

    def run(self, ctx: CheckContext) -> Iterable[Issue]:
        targets: dict[CellKey, list[CellKey]] = {}
        items: list[tuple[CellKey, str]] = []
        for key, cell in ctx.wb.iter_formula_cells():
            assert cell.formula is not None
            ctxs = walk(cell.formula)
            if ctxs is None:
                continue
            empties: list[CellKey] = []
            for t in ctxs:
                tok = t.token
                if tok.type != "OPERAND" or tok.subtype != "RANGE" or t.inside(TOLERANT):
                    continue
                ref = parse_operand(tok.value)
                if not isinstance(ref, AreaRef) or not ref.is_cell or ref.sheet_end:
                    continue
                sheet = ctx.wb.sheet_name(ref.sheet) if ref.sheet else key.sheet
                if sheet is None:
                    continue
                target = CellKey(sheet, ref.r1, ref.c1)
                tc = ctx.wb.cell(target)
                if tc is None or (tc.formula is None and tc.value is None):
                    empties.append(target)
            if empties:
                targets[key] = list(dict.fromkeys(empties))
                items.append((key, formula_r1c1(key, cell.formula)))

        for group in group_by_signature(items, ctx.graph):
            key = group[0]
            empty = targets[key]
            where = ", ".join(format_cell(e) for e in empty[:3])
            more = (
                f" The same pattern appears in {len(group) - 1} more cell(s)."
                if len(group) > 1
                else ""
            )
            yield make_issue(
                self.id,
                self.default_severity,
                key,
                f"Formula refers to empty cell(s) {where}, which count as 0.{more}",
                evidence=[cell_evidence(ctx, key)]
                + [cell_evidence(ctx, e, note="empty") for e in empty[:3]],
                confidence=Confidence.HIGH,
                affected_count=len(group),
                discriminator=where,
            )
