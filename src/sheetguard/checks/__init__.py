"""Check registry and runner."""

from __future__ import annotations

from collections.abc import Iterable

from sheetguard.checks.base import Check, CheckConfig, CheckContext
from sheetguard.checks.c01_hardcoded import HardcodedConstant
from sheetguard.checks.c02_inconsistent import InconsistentFormula
from sheetguard.checks.c03_empty_ref import EmptyReference
from sheetguard.checks.c04_errors import ErrorValues
from sheetguard.checks.c05_circular import CircularReference
from sheetguard.checks.c06_hardcoded_row import HardcodedInFormulaRow
from sheetguard.checks.c07_sum_range import IncompleteSum
from sheetguard.checks.c08_external import ExternalLink
from sheetguard.checks.c09_volatile import VolatileFunctions
from sheetguard.checks.c10_units import UnitMismatch
from sheetguard.checks.c11_hidden import HiddenLogic
from sheetguard.checks.c12_unused import UnusedInput
from sheetguard.checks.c13_cached import StaleCachedValues
from sheetguard.checks.c14_text_numbers import NumbersAsText
from sheetguard.core.graph import DepGraph
from sheetguard.core.model import Workbook
from sheetguard.core.refs import parse_area, split_sheet_prefix
from sheetguard.errors import InvalidArgumentError
from sheetguard.report.schema import Issue, Severity

REGISTRY: dict[str, type[Check]] = {
    cls.id: cls
    for cls in (
        HardcodedConstant,
        InconsistentFormula,
        EmptyReference,
        ErrorValues,
        CircularReference,
        HardcodedInFormulaRow,
        IncompleteSum,
        ExternalLink,
        VolatileFunctions,
        UnitMismatch,
        HiddenLogic,
        UnusedInput,
        StaleCachedValues,
        NumbersAsText,
    )
}


def run_checks(
    wb: Workbook,
    graph: DepGraph,
    checks: Iterable[str] | None = None,
    min_severity: Severity = Severity.INFO,
    config: CheckConfig | None = None,
) -> list[Issue]:
    """Run the selected checks and return issues in a stable order (FR-40, NF-3)."""
    selected = sorted(set(checks)) if checks is not None else sorted(REGISTRY)
    unknown = [c for c in selected if c not in REGISTRY]
    if unknown:
        raise InvalidArgumentError(f"Unknown check id(s): {', '.join(unknown)}")
    ctx = CheckContext(wb=wb, graph=graph, config=config or CheckConfig())
    issues: dict[str, Issue] = {}
    for check_id in selected:
        for issue in REGISTRY[check_id]().run(ctx):
            if issue.severity.rank <= min_severity.rank:
                issues.setdefault(issue.id, issue)
    order = {name: i for i, name in enumerate(wb.sheets)}
    return sorted(
        issues.values(),
        key=lambda i: (
            i.severity.rank,
            order.get(i.sheet, 10**6),
            _cell_sort(i.cell),
            i.check_id,
            i.id,
        ),
    )


def _cell_sort(address: str) -> tuple[int, int]:
    _, addr = split_sheet_prefix(address)
    area = parse_area(addr)
    return (area[0], area[1]) if area else (0, 0)


__all__ = ["REGISTRY", "Check", "CheckConfig", "CheckContext", "run_checks"]
