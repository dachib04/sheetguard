"""Property tests (§10.1): on generated workbooks the graph never crashes, every
edge has a matching reverse edge, cycle detection agrees with a reference
implementation, and traces always terminate within their limits."""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sheetguard.core.graph import DepGraph, Node
from sheetguard.core.refs import CellKey, RangeKey, a1
from sheetguard.core.tracer import trace
from tests.helpers import build

COLS, ROWS = 5, 6
SHEETS = ["S", "T"]

cell_ref = st.tuples(st.sampled_from(SHEETS), st.integers(1, ROWS), st.integers(1, COLS))


@st.composite
def ref_text(draw: st.DrawFn, here: str) -> str:
    sheet, r, c = draw(cell_ref)
    prefix = "" if sheet == here else f"{sheet}!"
    if draw(st.booleans()):
        r2, c2 = draw(st.integers(r, ROWS)), draw(st.integers(c, COLS))
        return f"SUM({prefix}{a1(r, c)}:{a1(r2, c2)})"
    return prefix + a1(r, c)


@st.composite
def workbooks(draw: st.DrawFn) -> dict[str, dict[str, object]]:
    sheets: dict[str, dict[str, object]] = {}
    for s in SHEETS:
        cells: dict[str, object] = {}
        for r in range(1, ROWS + 1):
            for c in range(1, COLS + 1):
                kind = draw(st.sampled_from(["empty", "const", "formula", "formula"]))
                if kind == "const":
                    cells[a1(r, c)] = draw(st.integers(-5, 5))
                elif kind == "formula":
                    n = draw(st.integers(1, 3))
                    cells[a1(r, c)] = "=" + "+".join(draw(ref_text(s)) for _ in range(n))
        sheets[s] = cells
    return sheets


def _cell_successors(g: DepGraph, node: Node) -> set[CellKey]:
    """Formula-cell successors, looking through ranges (reference for cycle check)."""
    out: set[CellKey] = set()
    for p in g.precedents(node):
        if isinstance(p, RangeKey):
            out |= {k for k in g.precedents(p) if isinstance(k, CellKey)}
        elif isinstance(p, CellKey):
            out.add(p)
    return {k for k in out if (cell := g.wb.cell(k)) is not None and cell.formula}


def _reaches(g: DepGraph, start: CellKey, goal: CellKey) -> bool:
    seen, stack = set(), list(_cell_successors(g, start))
    while stack:
        n = stack.pop()
        if n == goal:
            return True
        if n not in seen:
            seen.add(n)
            stack.extend(_cell_successors(g, n))
    return False


SETTINGS = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@SETTINGS
@given(workbooks())
def test_reverse_edges_match(sheets):
    g = DepGraph(build(sheets))
    for node in list(g.nodes_with_formulas()):
        for p in g.precedents(node):
            assert node in g.dependents(p)


@SETTINGS
@given(workbooks())
def test_cycles_match_reference(sheets):
    g = DepGraph(build(sheets))
    in_cycle = {k for cyc in g.find_cycles() for k in cyc}
    for key, _ in g.wb.iter_formula_cells():
        assert (key in in_cycle) == _reaches(g, key, key), key


@SETTINGS
@given(
    workbooks(),
    st.integers(1, 8),
    st.integers(1, 60),
    st.sampled_from(["precedents", "dependents"]),
)
def test_trace_terminates_within_limits(sheets, depth, nodes, direction):
    g = DepGraph(build(sheets))
    for key, _ in list(g.wb.iter_cells())[:5]:
        r = trace(g, key, direction, max_depth=depth, max_nodes=nodes)
        assert r.node_count <= nodes
        if r.node_count == nodes and nodes > 1:
            pass  # may or may not be truncated, but must have stopped
        assert r.root.address


@SETTINGS
@given(workbooks())
def test_upstream_inputs_are_constants(sheets):
    g = DepGraph(build(sheets))
    for key, _ in list(g.wb.iter_formula_cells())[:5]:
        inputs, _, _ = g.upstream_inputs(key, cap=500)
        for i in inputs:
            assert g.is_input(i)
