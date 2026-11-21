"""C14 - numbers stored as text (warning).

Detection: text constants that read as numbers ("1,234.50", "$99", "(250)",
"12%") and that a formula uses, so SUM silently skips them and arithmetic may
fail.

False-positive policy:
* ID and code columns are ignored by header heuristic (id, code, zip, phone,
  account, sku, ref, number, no., #, iban, isin...);
* strings with leading zeros ("00123") are codes, not numbers;
* only cells some formula depends on are reported;
* one issue per vertical run.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from sheetguard.checks.base import (
    Check,
    CheckContext,
    cell_evidence,
    group_column_runs,
    make_issue,
    span_label,
)
from sheetguard.core.heuristics import is_id_column
from sheetguard.core.model import ValueType
from sheetguard.core.refs import CellKey
from sheetguard.report.schema import Confidence, Issue, Severity
from sheetguard.security.envelope import wrap

_NUMBER_TEXT = re.compile(
    r"^\s*[-+(]?\s*[$€£¥₾]?\s*[-+]?(?:\d{1,3}(?:[,\s]\d{3})+|\d+)(?:\.\d+)?\s*%?\s*\)?\s*"
    r"(?:[$€£¥₾]|EUR|USD|GBP|GEL)?\s*$",
    re.IGNORECASE,
)


def parse_number_text(text: str) -> float | None:
    """The number a numeric-looking string stands for, or None."""
    if not _NUMBER_TEXT.match(text):
        return None
    digits = text.strip()
    if re.match(r"^0\d", digits):
        return None  # leading zero: a code
    negative = (digits.startswith("(") and digits.endswith(")")) or digits.startswith("-")
    pct = "%" in digits
    core = re.sub(r"[^\d.]", "", digits)
    if not core or core.count(".") > 1:
        return None
    value = float(core)
    value = -value if negative else value
    return value / 100 if pct else value


class NumbersAsText(Check):
    id = "C14"
    title = "Numbers stored as text"
    default_severity = Severity.WARNING

    def run(self, ctx: CheckContext) -> Iterable[Issue]:
        hits: list[CellKey] = []
        parsed: dict[CellKey, float] = {}
        for key, cell in ctx.wb.iter_cells():
            if cell.formula is not None or cell.vtype is not ValueType.TEXT:
                continue
            value = parse_number_text(str(cell.value))
            if value is None or not ctx.graph.has_dependents(key) or is_id_column(ctx.wb, key):
                continue
            hits.append(key)
            parsed[key] = value
        for run in group_column_runs(hits, ctx.graph):
            first = run[0]
            yield make_issue(
                self.id,
                self.default_severity,
                first,
                f"{len(run)} number(s) stored as text are used by formulas; SUM and similar "
                f"functions ignore them.",
                evidence=[cell_evidence(ctx, k) for k in run[:3]],
                confidence=Confidence.HIGH,
                affected_count=len(run),
                suggested_fix=wrap(
                    f"{parsed[first]:g}", f"numeric value for {ctx.wb.address(first)}"
                ),
                cell_label=span_label(run),
            )
