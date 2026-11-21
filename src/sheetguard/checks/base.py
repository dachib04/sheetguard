"""Check framework (§5.3).

Each check is a small class with an ``id`` and a ``run`` method that yields
``Issue`` objects. Issue IDs are a hash of (check, location, discriminator), so
the same input file always produces the same IDs (FR-40, NF-3).
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import ClassVar

from sheetguard.core.graph import DepGraph, NameNode, Node
from sheetguard.core.model import Workbook
from sheetguard.core.refs import CellKey, RangeKey, format_cell
from sheetguard.core.tracer import out_value
from sheetguard.report.schema import Confidence, Evidence, Issue, Severity
from sheetguard.security.envelope import UntrustedText, wrap


@dataclass(frozen=True, slots=True)
class CheckConfig:
    # C01: numeric literals that are never "magic numbers"
    allowed_constants: frozenset[str] = frozenset({"0", "1", "12", "100", "1000", "-1", "2"})
    min_consistent_neighbours: int = 3
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class CheckContext:
    wb: Workbook
    graph: DepGraph
    config: CheckConfig


def issue_id(check_id: str, location: str, discriminator: str = "") -> str:
    digest = hashlib.sha1(
        f"{check_id}|{location}|{discriminator}".encode(), usedforsecurity=False
    ).hexdigest()
    return f"{check_id}-{digest[:10]}"


def cell_evidence(ctx: CheckContext, key: CellKey, note: str | None = None) -> Evidence:
    address = format_cell(key)
    cell = ctx.wb.cell(key)
    if cell is None:
        return Evidence(address=address, note=note or "empty cell")
    formula: UntrustedText | None = None
    if cell.formula is not None:
        formula = wrap(cell.formula, f"{address} (formula)")
    return Evidence(
        address=address,
        formula=formula,
        value=out_value(cell.value, cell.vtype, address),
        note=note,
    )


def make_issue(
    check_id: str,
    severity: Severity,
    key: CellKey,
    description: str,
    evidence: Sequence[Evidence] = (),
    confidence: Confidence = Confidence.HIGH,
    suggested_fix: UntrustedText | None = None,
    affected_count: int = 1,
    discriminator: str = "",
    cell_label: str | None = None,
) -> Issue:
    address = cell_label or format_cell(key)
    return Issue(
        id=issue_id(check_id, address, discriminator),
        check_id=check_id,
        severity=severity,
        sheet=key.sheet,
        cell=address,
        description=description,
        evidence=tuple(evidence),
        confidence=confidence,
        suggested_fix=suggested_fix,
        affected_count=affected_count,
    )


def precedent_cells(graph: DepGraph, node: Node, max_depth: int = 8) -> list[CellKey]:
    """Direct precedent *cells*, looking through range and name nodes."""
    out: list[CellKey] = []
    seen: set[Node] = set()
    stack: list[tuple[Node, int]] = [(p, 0) for p in graph.precedents(node)]
    while stack:
        n, depth = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        if isinstance(n, CellKey):
            out.append(n)
        elif depth < max_depth and isinstance(n, RangeKey | NameNode):
            stack.extend((p, depth + 1) for p in graph.precedents(n))
    return out


def dependent_cells(graph: DepGraph, node: Node, max_depth: int = 8) -> list[CellKey]:
    """Direct dependent *cells*, looking through range and name nodes."""
    out: list[CellKey] = []
    seen: set[Node] = set()
    stack: list[tuple[Node, int]] = [(d, 0) for d in graph.dependents(node)]
    while stack:
        n, depth = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        if isinstance(n, CellKey):
            out.append(n)
        elif depth < max_depth:
            stack.extend((d, depth + 1) for d in graph.dependents(n))
    return out


class Check(ABC):
    id: ClassVar[str]
    title: ClassVar[str]
    default_severity: ClassVar[Severity]

    @abstractmethod
    def run(self, ctx: CheckContext) -> Iterable[Issue]:
        """Yield issues. Must be deterministic for a given workbook."""


def group_by_signature(
    items: Iterable[tuple[CellKey, str]], graph: DepGraph
) -> list[list[CellKey]]:
    """Group cells that share a (sheet, signature), e.g. the same R1C1 formula.

    Used so a pattern repeated in 500 cells is one issue with ``affected_count``
    500, not 500 issues. Groups and their members are in workbook order.
    """
    groups: dict[tuple[str, str], list[CellKey]] = {}
    for key, sig in sorted(items, key=lambda kv: graph.sort_key(kv[0])):
        groups.setdefault((key.sheet, sig), []).append(key)
    return sorted(groups.values(), key=lambda g: graph.sort_key(g[0]))


def group_column_runs(keys: Iterable[CellKey], graph: DepGraph) -> list[list[CellKey]]:
    """Group cells into vertical runs of consecutive rows in the same column."""
    runs: list[list[CellKey]] = []
    for key in sorted(keys, key=lambda k: (graph.sort_key(k)[0], k.col, k.row)):
        last = runs[-1][-1] if runs else None
        if (
            last is not None
            and last.sheet == key.sheet
            and last.col == key.col
            and key.row == last.row + 1
        ):
            runs[-1].append(key)
        else:
            runs.append([key])
    return sorted(runs, key=lambda g: graph.sort_key(g[0]))


def span_label(keys: list[CellKey]) -> str:
    """'S!B2' for one cell, 'S!B2:B9' for a run (bounding box)."""
    from sheetguard.core.refs import a1, quote_sheet

    first = keys[0]
    if len(keys) == 1:
        return format_cell(first)
    r1 = min(k.row for k in keys)
    r2 = max(k.row for k in keys)
    c1 = min(k.col for k in keys)
    c2 = max(k.col for k in keys)
    return f"{quote_sheet(first.sheet)}!{a1(r1, c1)}:{a1(r2, c2)}"


WORKBOOK_LABEL = "(workbook)"


def workbook_key(ctx: CheckContext) -> CellKey:
    """Anchor for workbook-level issues: first sheet, row 0 sorts before every cell."""
    first = next(iter(ctx.wb.sheets), "")
    return CellKey(first, 0, 0)


def formula_r1c1(key: CellKey, formula: str) -> str:
    from sheetguard.core.refs import to_r1c1

    return to_r1c1(formula, key.row, key.col)
