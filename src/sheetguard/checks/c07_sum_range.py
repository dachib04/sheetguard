"""C07 - incomplete SUM range (warning).

Detection: a one-dimensional range inside SUM stops exactly one cell short of
the block it is summing: the cell just past either end is the same kind as
the block (numeric constants, or formulas with the block's R1C1 pattern) and
the block ends right after it.

False-positive policy:
* only adjacent, same-type blocks count (spec);
* the extra cell must not be referenced elsewhere in the same formula
  (``=SUM(B2:B9)+B10`` is complete);
* the extra cell must not be the formula cell itself or a total;
* a year/period label just before the range start is ignored.

Fix: the same formula with the range widened to include the missed cell.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from sheetguard.checks.base import Check, CheckContext, cell_evidence, make_issue
from sheetguard.core.fixes import with_extended_range
from sheetguard.core.heuristics import is_index_like
from sheetguard.core.model import Cell, Sheet, ValueType
from sheetguard.core.refs import (
    MAX_COL,
    MAX_ROW,
    AreaRef,
    CellKey,
    format_cell,
    parse_operand,
    split_sheet_prefix,
    to_r1c1,
    walk,
)
from sheetguard.report.schema import Confidence, Issue, Severity
from sheetguard.security.envelope import wrap


def _kind(cell: Cell | None, row: int, col: int) -> str | None:
    if cell is None:
        return None
    if cell.formula is not None:
        return "f:" + to_r1c1(cell.formula, row, col)
    if cell.vtype is ValueType.NUMBER:
        return "n"
    return None


class IncompleteSum(Check):
    id = "C07"
    title = "Incomplete SUM range"
    default_severity = Severity.WARNING

    def run(self, ctx: CheckContext) -> Iterable[Issue]:
        for key, cell in ctx.wb.iter_formula_cells():
            assert cell.formula is not None
            ctxs = walk(cell.formula)
            if ctxs is None or "SUM" not in cell.formula.upper():
                continue
            direct: set[CellKey] = set()
            for t in ctxs:
                if t.token.type == "OPERAND" and t.token.subtype == "RANGE":
                    r = parse_operand(t.token.value)
                    if isinstance(r, AreaRef) and r.is_cell:
                        s = ctx.wb.sheet_name(r.sheet) if r.sheet else key.sheet
                        if s:
                            direct.add(CellKey(s, r.r1, r.c1))
            for t in ctxs:
                tok = t.token
                if tok.type != "OPERAND" or tok.subtype != "RANGE" or t.function != "SUM":
                    continue
                ref = parse_operand(tok.value)
                if not isinstance(ref, AreaRef) or ref.is_cell or ref.sheet_end:
                    continue
                if ref.r2 == MAX_ROW or ref.c2 == MAX_COL:
                    continue
                vertical = ref.c1 == ref.c2
                if not vertical and ref.r1 != ref.r2:
                    continue
                sheet_name = ctx.wb.sheet_name(ref.sheet) if ref.sheet else key.sheet
                if sheet_name is None:
                    continue
                issue = self._judge(
                    ctx,
                    key,
                    cell.formula,
                    t.index,
                    tok.value,
                    ctx.wb.sheets[sheet_name],
                    ref,
                    vertical,
                    direct,
                )
                if issue is not None:
                    yield issue

    def _judge(
        self,
        ctx: CheckContext,
        key: CellKey,
        formula: str,
        token_index: int,
        token_text: str,
        sheet: Sheet,
        ref: AreaRef,
        vertical: bool,
        direct: set[CellKey],
    ) -> Issue | None:
        members = (
            [(r, ref.c1) for r in range(ref.r1, ref.r2 + 1)]
            if vertical
            else [(ref.r1, c) for c in range(ref.c1, ref.c2 + 1)]
        )
        kinds = [_kind(sheet.cells.get(p), *p) for p in members]
        present = [k for k in kinds if k is not None]
        if len(present) < 2:
            return None
        block, count = Counter(present).most_common(1)[0]
        if count / len(members) < 0.6:
            return None

        def step(pos: tuple[int, int], d: int) -> tuple[int, int]:
            return (pos[0] + d, pos[1]) if vertical else (pos[0], pos[1] + d)

        for side, edge, d in (("end", members[-1], 1), ("start", members[0], -1)):
            extra = step(edge, d)
            beyond = step(extra, d)
            if min(extra) < 1 or (sheet.name, *extra) == tuple(key):
                continue
            ekey = CellKey(sheet.name, *extra)
            if ekey in direct or (sheet.name == key.sheet and extra == (key.row, key.col)):
                continue
            if _kind(sheet.cells.get(extra), *extra) != block:
                continue
            if min(beyond) >= 1 and _kind(sheet.cells.get(beyond), *beyond) == block:
                continue  # block continues: a deliberate partial sum, not "one short"
            if beyond == (key.row, key.col) and sheet.name == key.sheet:
                pass  # the SUM sits right after the missed cell: the classic case
            if side == "start" and is_index_like(ctx.wb, ekey):
                continue
            new_area = (
                min(ref.r1, extra[0]),
                min(ref.c1, extra[1]),
                max(ref.r2, extra[0]),
                max(ref.c2, extra[1]),
            )
            prefix, _ = split_sheet_prefix(token_text)
            keep = token_text[: len(token_text) - len(_)] if prefix is not None else ""
            text = with_extended_range(formula, token_index, new_area, keep)
            fix = wrap(text, f"suggested formula for {ctx.wb.address(key)}") if text else None
            confidence = Confidence.HIGH if side == "end" else Confidence.MEDIUM
            return make_issue(
                self.id,
                self.default_severity,
                key,
                f"SUM range stops one cell short: {format_cell(ekey)} continues the same "
                f"block but is not included.",
                evidence=[cell_evidence(ctx, key), cell_evidence(ctx, ekey, note="left out")],
                confidence=confidence,
                suggested_fix=fix,
                discriminator=format_cell(ekey),
            )
        return None
