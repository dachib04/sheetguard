"""C01 - hardcoded constant inside a formula (warning).

Detection: numeric literals in a formula, other than an allow-list (0, 1, 2, -1,
12, 100, 1000 and common date/time parts such as 7, 24, 52, 60, 360, 365).

False-positive policy:
* the allow-list is configurable (``CheckConfig.allowed_constants``);
* structural arguments are ignored: ROUND digits, VLOOKUP/HLOOKUP column index
  and match mode, INDEX/MATCH/XLOOKUP positions and modes, OFFSET sizes,
  LEFT/RIGHT/MID lengths, LARGE/SMALL k, SUBTOTAL/AGGREGATE function numbers,
  DATE/TIME parts, EDATE/EOMONTH months, WEEKDAY/WEEKNUM types;
* cells targeted by a defined name (named inputs) are ignored;
* formulas with no references at all (``=250000``, ``=12*1000``) are inputs
  written as formulas, not hardcodes inside logic, and are ignored;
* the same formula pattern repeated across cells is one issue.

Fix (FR-31): move the constant to a named input cell; the proposed formula is
shown with the literal replaced by the suggested name.
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
from sheetguard.core.fixes import input_name_for
from sheetguard.core.refs import AreaRef, CellKey, analyze_formula, render, walk
from sheetguard.report.schema import Confidence, Issue, Severity
from sheetguard.security.envelope import wrap

DATE_PARTS = frozenset(
    {"4", "7", "24", "30", "31", "52", "60", "360", "365", "366", "3600", "86400"}
)

# function -> argument positions (0-based) that are structural, None = all arguments
STRUCTURAL: dict[str, frozenset[int] | None] = {
    "ROUND": frozenset({1}),
    "ROUNDUP": frozenset({1}),
    "ROUNDDOWN": frozenset({1}),
    "TRUNC": frozenset({1}),
    "FIXED": frozenset({1}),
    "VLOOKUP": frozenset({2, 3}),
    "HLOOKUP": frozenset({2, 3}),
    "INDEX": frozenset({1, 2, 3}),
    "MATCH": frozenset({2}),
    "XMATCH": frozenset({2, 3}),
    "XLOOKUP": frozenset({4, 5}),
    "OFFSET": frozenset({1, 2, 3, 4}),
    "CHOOSE": frozenset({0}),
    "LEFT": frozenset({1}),
    "RIGHT": frozenset({1}),
    "MID": frozenset({1, 2}),
    "LARGE": frozenset({1}),
    "SMALL": frozenset({1}),
    "RANK": frozenset({2}),
    "PERCENTILE": frozenset({1}),
    "QUARTILE": frozenset({1}),
    "SUBTOTAL": frozenset({0}),
    "AGGREGATE": frozenset({0, 1}),
    "DATE": None,
    "TIME": None,
    "EDATE": frozenset({1}),
    "EOMONTH": frozenset({1}),
    "WEEKDAY": frozenset({1}),
    "WEEKNUM": frozenset({1}),
    "YEARFRAC": frozenset({2}),
    "DAYS360": frozenset({2}),
    "NETWORKDAYS.INTL": frozenset({2}),
    "WORKDAY.INTL": frozenset({2}),
    "TEXT": None,
    "SEQUENCE": None,
    "RANDBETWEEN": None,
    "ADDRESS": None,
    "CEILING": frozenset({1}),
    "FLOOR": frozenset({1}),
    "MROUND": frozenset({1}),
}


def _normalise(literal: str) -> str:
    try:
        f = float(literal.rstrip("%"))
    except ValueError:
        return literal
    text = str(int(f)) if f == int(f) else repr(f)
    return text + ("%" if literal.endswith("%") else "")


class HardcodedConstant(Check):
    id = "C01"
    title = "Hardcoded constant inside a formula"
    default_severity = Severity.WARNING

    def run(self, ctx: CheckContext) -> Iterable[Issue]:
        allowed = {_normalise(c) for c in ctx.config.allowed_constants} | DATE_PARTS
        named_cells = _named_cells(ctx)
        hits: dict[CellKey, tuple[list[str], int, bool]] = {}
        items: list[tuple[CellKey, str]] = []
        for key, cell in ctx.wb.iter_formula_cells():
            assert cell.formula is not None
            if key in named_cells:
                continue
            ctxs = walk(cell.formula)
            if ctxs is None:
                continue
            if not any(t.token.type == "OPERAND" and t.token.subtype == "RANGE" for t in ctxs):
                continue  # an input written as a formula
            found: list[str] = []
            first_index = -1
            first_pct = False
            for i, t in enumerate(ctxs):
                tok = t.token
                if tok.type != "OPERAND" or tok.subtype != "NUMBER":
                    continue
                fn, arg = t.function, t.arg
                if fn is not None and fn in STRUCTURAL:
                    positions = STRUCTURAL[fn]
                    if positions is None or arg in positions:
                        continue
                pct = i + 1 < len(ctxs) and ctxs[i + 1].token.value == "%"
                lit = _normalise(tok.value + ("%" if pct else ""))
                if lit.lstrip("-") in allowed or lit in allowed:
                    continue
                if first_index < 0:
                    first_index, first_pct = i, pct
                found.append(lit)
            if found:
                hits[key] = (found, first_index, first_pct)
                items.append((key, formula_r1c1(key, cell.formula) + "|" + ",".join(found)))

        for group in group_by_signature(items, ctx.graph):
            key = group[0]
            first = ctx.wb.cell(key)
            assert first is not None
            assert first.formula is not None
            found, idx, pct = hits[key]
            name = input_name_for(found[0])
            tokens = [c.token for c in (walk(first.formula) or [])]
            replace = {idx: name, **({idx + 1: ""} if pct else {})}
            fix = (
                wrap(
                    render(tokens, replace),
                    f"suggested formula for {ctx.wb.address(key)} (after creating a named "
                    f"input {name} = {found[0]})",
                )
                if tokens
                else None
            )
            more = (
                f" The same pattern appears in {len(group) - 1} more cell(s)."
                if len(group) > 1
                else ""
            )
            nums = ", ".join(dict.fromkeys(found))
            yield make_issue(
                self.id,
                self.default_severity,
                key,
                f"Formula contains hardcoded number(s) {nums}; move them to labelled input "
                f"cells.{more}",
                evidence=[cell_evidence(ctx, k) for k in group[:3]],
                confidence=Confidence.MEDIUM,
                suggested_fix=fix,
                affected_count=len(group),
                discriminator=nums,
            )


def _named_cells(ctx: CheckContext) -> set[CellKey]:
    """Cells that a defined name points at directly (named inputs)."""
    out: set[CellKey] = set()
    for n in ctx.wb.names:
        for ref in analyze_formula("=" + n.text).refs:
            if isinstance(ref, AreaRef) and ref.is_cell:
                sheet = ctx.wb.sheet_name(ref.sheet) if ref.sheet else n.scope_sheet
                if sheet:
                    out.add(CellKey(sheet, ref.r1, ref.c1))
    return out
