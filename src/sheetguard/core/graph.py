"""Dependency graph across all sheets (FR-4, FR-10, FR-13, FR-14).

Nodes are cells, multi-cell ranges and defined names. A formula that reads
``SUM(A1:A5000)`` gets one edge to a range node instead of 5,000 edges, and the
range node points at the cells that actually exist inside it. Whole-column and
whole-row references are clipped to the sheet's used range before they become
nodes, so ``A:A`` never means a million cells.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Literal, NamedTuple

from sheetguard.core.model import Workbook
from sheetguard.core.refs import (
    AreaRef,
    CellKey,
    ExternalRef,
    NameRef,
    RangeKey,
    Ref,
    StructRef,
    UnparsedRef,
    analyze_formula,
    format_cell,
    format_range,
)


class NameNode(NamedTuple):
    scope: str | None
    name: str


Node = CellKey | RangeKey | NameNode


NodeKind = Literal["cell", "range", "name"]


def node_kind(node: Node) -> NodeKind:
    if isinstance(node, NameNode):
        return "name"
    if isinstance(node, RangeKey):
        return "range"
    return "cell"


def format_node(node: Node) -> str:
    if isinstance(node, NameNode):
        return node.name if node.scope is None else f"{node.scope}!{node.name}"
    if isinstance(node, RangeKey):
        return format_range(node)
    return format_cell(node)


@dataclass(slots=True)
class NodeFlags:
    """Why the static picture of a formula may be incomplete."""

    external: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    dynamic: bool = False  # INDIRECT / OFFSET: real precedents unknown statically
    functions: list[str] = field(default_factory=list)
    parse_error: bool = False

    @property
    def partial(self) -> bool:
        return self.dynamic or bool(self.unresolved) or self.parse_error


class DepGraph:
    def __init__(self, wb: Workbook) -> None:
        self.wb = wb
        self._prec: dict[Node, tuple[Node, ...]] = {}
        self._deps: dict[Node, set[Node]] = {}
        self._range_buckets: dict[tuple[str, int], list[RangeKey]] = {}
        self.flags: dict[Node, NodeFlags] = {}
        self._sheet_order = {name: i for i, name in enumerate(wb.sheets)}
        self._cycles: list[list[CellKey]] | None = None
        self._build()

    # ------------------------------------------------------------------ build

    def _build(self) -> None:
        for key, cell in self.wb.iter_formula_cells():
            assert cell.formula is not None
            self._add_formula(key, cell.formula, key.sheet, key)
        # names are added lazily as they are referenced; resolve them now in order
        pending = [n for n in self._deps if isinstance(n, NameNode)]
        seen: set[NameNode] = set()
        while pending:
            name = pending.pop()
            if not isinstance(name, NameNode) or name in seen:
                continue
            seen.add(name)
            info = self.wb.defined_name(name.name, name.scope)
            if info is None:
                continue
            before = {n for n in self._deps if isinstance(n, NameNode)}
            self._add_formula(name, "=" + info.text, info.scope_sheet, None)
            pending.extend(n for n in self._deps if isinstance(n, NameNode) and n not in before)

    def _add_formula(
        self, source: Node, formula: str, sheet: str | None, origin: CellKey | None
    ) -> None:
        info = analyze_formula(formula)
        flags = NodeFlags(
            functions=list(info.functions), parse_error=not info.ok, dynamic=info.is_dynamic
        )
        targets: list[Node] = []
        for ref in info.refs:
            targets.extend(self._resolve(ref, sheet, origin, flags))
        if "#REF!" in info.errors:
            flags.unresolved.append("#REF!")
        unique = tuple(dict.fromkeys(targets))
        self._prec[source] = unique
        self.flags[source] = flags
        for t in unique:
            self._deps.setdefault(t, set()).add(source)
            if isinstance(t, RangeKey) and t not in self._prec:
                self._prec[t] = ()  # members are computed lazily
                for c in range(t.c1, t.c2 + 1):
                    self._range_buckets.setdefault((t.sheet, c), []).append(t)

    def _resolve(
        self, ref: Ref, sheet: str | None, origin: CellKey | None, flags: NodeFlags
    ) -> list[Node]:
        if isinstance(ref, AreaRef):
            sheet_names = self.area_sheets(ref, sheet)
            if not sheet_names:
                flags.unresolved.append("missing sheet")
                return []
            out: list[Node] = []
            for s in sheet_names:
                node = self._area_node(s, ref.r1, ref.c1, ref.r2, ref.c2)
                if node is not None:
                    out.append(node)
            return out
        if isinstance(ref, NameRef):
            scope_sheet = ref.sheet if ref.sheet is not None else sheet
            info = self.wb.defined_name(ref.name, scope_sheet)
            if info is None:
                if ref.name.upper() not in {"TRUE", "FALSE"}:
                    flags.unresolved.append(f"name {ref.name}")
                return []
            return [NameNode(info.scope_sheet, info.name)]
        if isinstance(ref, StructRef):
            return self._resolve_struct(ref, origin, flags)
        if isinstance(ref, ExternalRef):
            flags.external.append(ref.raw)
            return []
        if isinstance(ref, UnparsedRef):
            flags.unresolved.append(ref.raw)
        return []

    def _area_node(self, sheet: str, r1: int, c1: int, r2: int, c2: int) -> Node | None:
        if r1 == r2 and c1 == c2:
            return CellKey(sheet, r1, c1)
        used = self.wb.sheets[sheet].used
        if used is None:
            return None
        # FR-10: clip whole-column/whole-row (and any oversized) refs to the used range.
        cr1, cc1 = max(r1, used.r1), max(c1, used.c1)
        cr2, cc2 = min(r2, used.r2), min(c2, used.c2)
        if cr1 > cr2 or cc1 > cc2:
            return None
        if cr1 == cr2 and cc1 == cc2:
            return CellKey(sheet, cr1, cc1)
        return RangeKey(sheet, cr1, cc1, cr2, cc2)

    def _resolve_struct(
        self, ref: StructRef, origin: CellKey | None, flags: NodeFlags
    ) -> list[Node]:
        area = self.struct_area(ref, origin, flags)
        if area is None:
            return []
        node = self._area_node(*area)
        return [node] if node is not None else []

    def struct_area(
        self, ref: StructRef, origin: CellKey | None, flags: NodeFlags
    ) -> tuple[str, int, int, int, int] | None:
        """The exact rectangle a structured reference denotes (FR-4)."""
        table = (
            self.wb.table(ref.table)
            if ref.table
            else self.wb.table_at(origin.sheet, origin.row, origin.col)
            if origin
            else None
        )
        if table is None:
            flags.unresolved.append(f"table {ref.table or '[this table]'}")
            return None
        t = table.ref
        header_end = t.r1 + table.header_rows - 1
        data_r1, data_r2 = header_end + 1, t.r2 - table.totals_rows
        rows: list[tuple[int, int]] = []
        specials = ref.specials
        if "#all" in specials:
            rows.append((t.r1, t.r2))
        if "#headers" in specials and table.header_rows:
            rows.append((t.r1, header_end))
        if "#totals" in specials and table.totals_rows:
            rows.append((data_r2 + 1, t.r2))
        if "#data" in specials or not (specials - {"#this row"}):
            rows.append((data_r1, data_r2))
        if "#this row" in specials:
            if origin is None or not (data_r1 <= origin.row <= data_r2):
                flags.unresolved.append("[#This Row] outside table")
                return None
            rows = [(origin.row, origin.row)]
        c1, c2 = t.c1, t.c2
        if ref.col_start is not None:
            i = table.column_index(ref.col_start)
            j = table.column_index(ref.col_end or ref.col_start)
            if i is None or j is None:
                flags.unresolved.append(f"column {ref.col_start}")
                return None
            c1, c2 = t.c1 + min(i, j), t.c1 + max(i, j)
        r1, r2 = min(r for r, _ in rows), max(r for _, r in rows)
        if r1 > r2:
            return None
        return (table.sheet, r1, c1, r2, c2)

    def area_sheets(self, ref: AreaRef, sheet: str | None) -> list[str]:
        """Canonical sheet names an area reference points at (3D refs: several)."""
        if ref.sheet_end is not None and ref.sheet is not None:
            return self.wb.sheet_span(ref.sheet, ref.sheet_end)
        target = ref.sheet if ref.sheet is not None else sheet
        canonical = self.wb.sheet_name(target) if target else None
        return [canonical] if canonical else []

    def resolve_ref(
        self, ref: Ref, sheet: str | None, origin: CellKey | None
    ) -> tuple[list[Node], NodeFlags]:
        """Public form of reference resolution, used by the evaluator."""
        flags = NodeFlags()
        return self._resolve(ref, sheet, origin, flags), flags

    # ----------------------------------------------------------------- queries

    def precedents(self, node: Node) -> tuple[Node, ...]:
        if isinstance(node, RangeKey):
            sheet = self.wb.sheets[node.sheet]
            return tuple(
                CellKey(node.sheet, c.row, c.col)
                for c in sheet.cells_in(node.r1, node.c1, node.r2, node.c2)
                if not c.is_empty
            )
        return self._prec.get(node, ())

    def dependents(self, node: Node) -> tuple[Node, ...]:
        out = set(self._deps.get(node, ()))
        if isinstance(node, CellKey):
            for rng in self._range_buckets.get((node.sheet, node.col), ()):
                if rng.r1 <= node.row <= rng.r2:
                    out.add(rng)
        return tuple(sorted(out, key=self.sort_key))

    def has_dependents(self, node: Node) -> bool:
        if self._deps.get(node):
            return True
        if isinstance(node, CellKey):
            return any(
                r.r1 <= node.row <= r.r2
                for r in self._range_buckets.get((node.sheet, node.col), ())
            )
        return False

    def is_input(self, node: Node) -> bool:
        """A constant with no precedents (FR-11, FR-20). Names holding constants count."""
        if isinstance(node, CellKey):
            cell = self.wb.cell(node)
            return cell is not None and cell.formula is None and not cell.is_empty
        if isinstance(node, NameNode):
            return not self._prec.get(node) and not self.flags.get(node, NodeFlags()).partial
        return False

    def sort_key(self, node: Node) -> tuple[int, int, int, int, str]:
        if isinstance(node, NameNode):
            return (-1, 0, 0, 0, (node.scope or "") + "!" + node.name.lower())
        order = self._sheet_order.get(node.sheet, 10**6)
        if isinstance(node, RangeKey):
            return (order, node.r1, node.c1, 1, f"{node.r2}:{node.c2}")
        return (order, node.row, node.col, 0, "")

    def nodes_with_formulas(self) -> Iterator[Node]:
        yield from sorted((n for n in self._prec if not isinstance(n, RangeKey)), key=self.sort_key)

    # ------------------------------------------------------------------ cycles

    def _cycle_successors(self, node: Node) -> Iterable[Node]:
        if isinstance(node, RangeKey):
            sheet = self.wb.sheets[node.sheet]
            return [
                CellKey(node.sheet, c.row, c.col)
                for c in sheet.cells_in(node.r1, node.c1, node.r2, node.c2)
                if c.formula is not None
            ]
        return self._prec.get(node, ())

    def find_cycles(self) -> list[list[CellKey]]:
        """Strongly connected components that form cycles (FR-13). Cached."""
        if self._cycles is None:
            self._cycles = self._tarjan()
        return [list(c) for c in self._cycles]

    def _tarjan(self) -> list[list[CellKey]]:
        """Iterative Tarjan, so deep chains cannot overflow the Python stack."""
        index: dict[Node, int] = {}
        low: dict[Node, int] = {}
        on_stack: set[Node] = set()
        stack: list[Node] = []
        cycles: list[list[CellKey]] = []
        counter = 0
        roots = sorted(self._prec, key=self.sort_key)
        for root in roots:
            if root in index:
                continue
            work: list[tuple[Node, Iterator[Node]]] = [(root, iter(self._cycle_successors(root)))]
            index[root] = low[root] = counter
            counter += 1
            stack.append(root)
            on_stack.add(root)
            while work:
                node, it = work[-1]
                advanced = False
                for nxt in it:
                    if nxt not in index:
                        index[nxt] = low[nxt] = counter
                        counter += 1
                        stack.append(nxt)
                        on_stack.add(nxt)
                        work.append((nxt, iter(self._cycle_successors(nxt))))
                        advanced = True
                        break
                    if nxt in on_stack:
                        low[node] = min(low[node], index[nxt])
                if advanced:
                    continue
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    component: list[Node] = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        component.append(w)
                        if w == node:
                            break
                    cells = sorted(
                        (n for n in component if isinstance(n, CellKey)), key=self.sort_key
                    )
                    is_cycle = len(component) > 1 or node in self._cycle_successors(node)
                    if is_cycle and cells:
                        cycles.append(cells)
        cycles.sort(key=lambda cyc: self.sort_key(cyc[0]))
        return cycles

    # ----------------------------------------------------------- outputs (FR-14)

    def likely_outputs(self, limit: int = 10, cap: int = 5_000) -> list[tuple[CellKey, int]]:
        """Formula cells nothing depends on, ranked by how many cells feed them."""
        candidates = [
            n for n in self._prec if isinstance(n, CellKey) and not self.has_dependents(n)
        ]
        scored: list[tuple[CellKey, int]] = []
        for cell in candidates:
            scored.append((cell, self.count_upstream(cell, cap)))
        scored.sort(key=lambda kv: (-kv[1], self.sort_key(kv[0])))
        return [s for s in scored if s[1] > 1][:limit]

    def count_upstream(self, start: Node, cap: int) -> int:
        seen: set[Node] = {start}
        frontier = [start]
        count = 0
        while frontier and count < cap:
            node = frontier.pop()
            for p in self.precedents(node):
                if p not in seen:
                    seen.add(p)
                    frontier.append(p)
                    if isinstance(p, CellKey):
                        count += 1
        return count

    def upstream_inputs(self, start: Node, cap: int) -> tuple[list[Node], bool, NodeFlags]:
        """All input nodes feeding ``start`` (FR-20). Returns (inputs, capped, merged flags)."""
        seen: set[Node] = {start}
        frontier = [start]
        inputs: list[Node] = []
        merged = NodeFlags()
        capped = False
        while frontier:
            node = frontier.pop()
            f = self.flags.get(node)
            if f is not None:
                merged.dynamic = merged.dynamic or f.dynamic
                merged.parse_error = merged.parse_error or f.parse_error
                merged.unresolved.extend(f.unresolved)
                merged.external.extend(f.external)
                merged.functions.extend(f.functions)
            for p in self.precedents(node):
                if p in seen:
                    continue
                seen.add(p)
                if self.is_input(p):
                    if len(inputs) >= cap:
                        capped = True
                        continue
                    inputs.append(p)
                else:
                    frontier.append(p)
        inputs.sort(key=self.sort_key)
        merged.functions = sorted(set(merged.functions))
        return inputs, capped, merged
