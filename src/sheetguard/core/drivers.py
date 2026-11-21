"""Driver (sensitivity) analysis (FR-20..FR-24).

For a target cell: find every input that feeds it (constants with no
precedents, including constant defined names), perturb each by +delta and
-delta on a copy of the values, recompute only the cells between that input and
the target, and rank inputs by how much the target moves.

FR-22: if anything on the path cannot be evaluated (unsupported or dynamic
functions, cycles, 3D references) the result is marked partial and *no numbers
are returned*, because a number computed around a gap could be wrong. C13: the
baseline is always recomputed by the evaluator, never taken from cached values.
"""

from __future__ import annotations

import math

from sheetguard.core.evaluator import Evaluator
from sheetguard.core.graph import DepGraph, NameNode, Node, format_node
from sheetguard.core.heuristics import is_index_like
from sheetguard.core.model import ValueType
from sheetguard.core.refs import CellKey
from sheetguard.core.tracer import out_value
from sheetguard.errors import InvalidArgumentError
from sheetguard.report.schema import DriverEntry, DriversResult, OutValue


def _numeric(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def top_drivers(
    graph: DepGraph,
    target: CellKey,
    n: int = 10,
    delta_pct: float = 10.0,
    zero_delta: float = 1.0,
    max_inputs: int = 200,
    include_index_inputs: bool = False,
    evaluator: Evaluator | None = None,
) -> DriversResult:
    """Rank the inputs of ``target`` by how much a +/-``delta_pct`` change moves it.

    Year and period labels (runs of consecutive integers) are skipped unless
    ``include_index_inputs`` is set; they are counted in ``index_inputs_skipped``.
    """
    if not 0 < delta_pct <= 1000:
        raise InvalidArgumentError("delta_pct must be between 0 and 1000")
    if zero_delta <= 0:
        raise InvalidArgumentError("zero_delta must be positive")
    if not 1 <= n <= 200:
        raise InvalidArgumentError("n must be between 1 and 200")
    wb = graph.wb
    cell = wb.cell(target)
    if cell is None or cell.formula is None:
        raise InvalidArgumentError("Target must be a formula cell.")

    ev = evaluator or Evaluator(graph)
    inputs, capped, _ = graph.upstream_inputs(target, cap=max_inputs)
    total_found = len(inputs) + (1 if capped else 0)
    plan = ev.plan(target)
    # Fields shared by the partial and the full result: built once, typed by the model.
    header = DriversResult(
        target=format_node(target),
        cached_value=out_value(cell.value, cell.vtype, format_node(target)),
        baseline=None,
        delta_pct=delta_pct,
        zero_delta=zero_delta,
        inputs_found=total_found,
        capped=capped,
    )
    if plan.partial:
        return header.model_copy(
            update={
                "partial": True,
                "unsupported_functions": tuple(sorted(plan.unsupported)),
                "partial_reasons": tuple(plan.reasons),
            }
        )

    base = ev.evaluate(plan)
    baseline = ev.to_python(base[target])
    base_num = _numeric(baseline)

    entries: list[DriverEntry] = []
    non_numeric = 0
    skipped_index = 0
    for node in inputs:
        if not include_index_inputs and isinstance(node, CellKey) and is_index_like(wb, node):
            skipped_index += 1
            continue
        value = _input_value(graph, node)
        if value is None:
            non_numeric += 1
            continue
        zero = value == 0
        delta = zero_delta if zero else abs(value) * delta_pct / 100.0
        up = ev.to_python(ev.evaluate(plan, {node: value + delta}, base)[target])
        down = ev.to_python(ev.evaluate(plan, {node: value - delta}, base)[target])
        cu = _diff(up, base_num)
        cd = _diff(down, base_num)
        mag = max(abs(cu) if cu is not None else 0.0, abs(cd) if cd is not None else 0.0)
        pct = (mag / abs(base_num) * 100.0) if base_num else None
        entries.append(
            DriverEntry(
                input=format_node(node),
                kind="name" if isinstance(node, NameNode) else "cell",
                input_value=value,
                delta=delta,
                zero_input=zero,
                target_up=_out(up),
                target_down=_out(down),
                change_up=cu,
                change_down=cd,
                abs_change=mag,
                pct_of_baseline=pct,
            )
        )
    entries.sort(key=lambda e: (-e.abs_change, e.input))
    return header.model_copy(
        update={
            "baseline": _out(baseline),
            "drivers": tuple(entries[:n]),
            "inputs_analysed": len(entries),
            "non_numeric_inputs": non_numeric,
            "index_inputs_skipped": skipped_index,
        }
    )


def _input_value(graph: DepGraph, node: Node) -> float | None:
    if isinstance(node, CellKey):
        cell = graph.wb.cell(node)
        if cell is None or cell.vtype is not ValueType.NUMBER:
            return None
        return _numeric(cell.value)
    if isinstance(node, NameNode):
        info = graph.wb.defined_name(node.name, node.scope)
        if info is None:
            return None
        try:
            return _numeric(float(info.text.strip()))
        except ValueError:
            return None
    return None


def _diff(v: object, base: float | None) -> float | None:
    x = _numeric(v)
    if x is None or base is None:
        return None
    return x - base


def _out(v: object) -> OutValue:
    if isinstance(v, bool | int | float) or v is None:
        return v if not (isinstance(v, float) and not math.isfinite(v)) else None
    return out_value(
        str(v), ValueType.ERROR if str(v).startswith("#") else ValueType.TEXT, "evaluated"
    )
