"""Precedent / dependent trees (FR-11, FR-12, FR-13).

Breadth-first, so a truncated trace still shows the nearest cells first. Every
node is expanded at most once (later occurrences are marked "shown above"),
which also guarantees termination on cyclic graphs. Truncation is always
reported, never silent.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Literal

from sheetguard.core.graph import DepGraph, NameNode, Node, format_node, node_kind
from sheetguard.core.model import CellValue, ValueType
from sheetguard.core.refs import CellKey, RangeKey
from sheetguard.report.schema import OutValue, TraceNode, TraceResult
from sheetguard.security.envelope import UntrustedText, wrap

Direction = Literal["precedents", "dependents"]


def out_value(value: CellValue, vtype: ValueType, source: str) -> OutValue:
    """Workbook text is always wrapped; numbers and booleans pass through."""
    if isinstance(value, str):
        return wrap(value, f"{source} (cell {vtype.value})")
    return value


@dataclass(slots=True)
class _Entry:
    node: Node
    depth: int
    children: list[int] = field(default_factory=list)
    note: str | None = None


def trace(
    graph: DepGraph,
    start: Node,
    direction: Direction = "precedents",
    max_depth: int = 6,
    max_nodes: int = 500,
    max_children: int = 50,
) -> TraceResult:
    entries: list[_Entry] = [_Entry(start, 0)]
    expanded: set[Node] = set()
    queue: deque[int] = deque([0])
    reasons: set[str] = set()

    while queue:
        idx = queue.popleft()
        entry = entries[idx]
        if entry.node in expanded:
            entry.note = "shown above"
            continue
        expanded.add(entry.node)
        nexts = (
            graph.precedents(entry.node)
            if direction == "precedents"
            else graph.dependents(entry.node)
        )
        if not nexts:
            continue
        if entry.depth >= max_depth:
            entry.note = f"{len(nexts)} more not shown (max_depth {max_depth})"
            reasons.add("max_depth")
            continue
        shown = nexts[:max_children]
        if len(nexts) > max_children:
            entry.note = f"{len(nexts) - max_children} more not shown (fan-out limit)"
            reasons.add("max_children")
        for i, child in enumerate(shown):
            if len(entries) >= max_nodes:
                left = len(shown) - i
                entry.note = f"{left} more not shown (max_nodes {max_nodes})"
                reasons.add("max_nodes")
                queue.clear()
                break
            entries.append(_Entry(child, entry.depth + 1))
            entry.children.append(len(entries) - 1)
            queue.append(len(entries) - 1)

    partial_any = False
    built: dict[int, TraceNode] = {}
    for idx in range(len(entries) - 1, -1, -1):
        entry = entries[idx]
        node_out = _make_node(graph, entry, tuple(built.pop(c) for c in entry.children))
        partial_any = partial_any or node_out.partial
        built[idx] = node_out

    return TraceResult(
        root=built[0],
        direction=direction,
        node_count=len(entries),
        truncated=bool(reasons),
        truncation_reasons=tuple(sorted(reasons)),
        partial=partial_any,
    )


def _make_node(graph: DepGraph, entry: _Entry, children: tuple[TraceNode, ...]) -> TraceNode:
    node = entry.node
    address = format_node(node)
    kind = node_kind(node)
    flags = graph.flags.get(node)
    reasons: list[str] = []
    if flags is not None:
        if flags.dynamic:
            reasons.append("INDIRECT/OFFSET: dependencies not statically known")
        reasons.extend(f"unresolved {u}" for u in flags.unresolved)
        if flags.parse_error:
            reasons.append("formula could not be parsed")
        reasons.extend(f"external link {e}" for e in flags.external)

    formula: UntrustedText | None = None
    value: OutValue = None
    vtype = "empty"
    if isinstance(node, CellKey):
        cell = graph.wb.cell(node)
        if cell is not None:
            if cell.formula is not None:
                formula = wrap(cell.formula, f"{address} (formula)")
            value = out_value(cell.value, cell.vtype, address)
            vtype = cell.vtype.value
    elif isinstance(node, NameNode):
        info = graph.wb.defined_name(node.name, node.scope)
        if info is not None:
            formula = wrap("=" + info.text, f"defined name {address}")
        vtype = "name"
    elif isinstance(node, RangeKey):
        vtype = "range"

    return TraceNode(
        address=address,
        kind=kind,
        formula=formula,
        value=value,
        type=vtype,
        is_input=graph.is_input(node),
        partial=bool(flags and flags.partial),
        partial_reasons=tuple(reasons),
        note=entry.note,
        children=children,
    )
