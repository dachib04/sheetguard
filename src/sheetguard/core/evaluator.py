"""Evaluator: recalculate a target on a copy of the model (FR-21, FR-22, ADR 0001).

SheetGuard's own graph decides *what* to compute and in which order; the
``formulas`` package only evaluates one formula at a time. Every reference in a
formula is replaced by a placeholder name (``_sg0``, ``_sg1``...) before
compiling, so all cells that share a shape (e.g. 10,000 rows of ``=B2*C2``)
compile once. Placeholders can never be confused with cell addresses because
they start with an underscore.

Nothing here touches the workbook: values live in a dict owned by the caller.
Unsupported functions, dynamic references (INDIRECT/OFFSET), 3D references,
cycles and unparseable formulas make the result *partial*; we never guess.
"""

from __future__ import annotations

import datetime as dt
import warnings
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sheetguard.core.graph import DepGraph, NameNode, Node
from sheetguard.core.model import CellValue, ValueType
from sheetguard.core.refs import (
    AreaRef,
    CellKey,
    RangeKey,
    StructRef,
    analyze_formula,
    format_cell,
    parse_operand,
    tokenize,
)
from sheetguard.errors import MissingDependencyError

#: Functions that cannot be evaluated statically, whatever the engine supports.
DYNAMIC_FUNCTIONS = frozenset({"INDIRECT", "OFFSET"})
#: Handled by the formulas parser itself rather than its function table.
_PARSER_FUNCTIONS = frozenset({"LET"})
#: Need the *shape or position* of a reference; we resolve them statically
#: (ROWS(A1:A9) -> 9) and treat any other use as unsupported.
SHAPE_FUNCTIONS = frozenset({"ROW", "ROWS", "COLUMN", "COLUMNS"})
_REFERENCE_FUNCTIONS = frozenset({"CELL", "AREAS", "ISREF", "FORMULATEXT", "ISFORMULA"})


class EvaluatorUnavailableError(MissingDependencyError):
    """The optional ``formulas`` dependency is not installed."""


def _load_engine() -> tuple[Any, Any, Any, Any]:
    # `formulas` imports some submodules lazily, after this function returns, and on
    # Python 3.11+ those trigger SyntaxWarnings ('"is" with a literal') whenever bytecode
    # is not precompiled (uv installs, PYTHONDONTWRITEBYTECODE in Docker). Silence exactly
    # those, process-wide, so CLI output and MCP logs stay clean.
    warnings.filterwarnings(
        "ignore", category=SyntaxWarning, message=r'"is( not)?" with (a|\'str\') literal'
    )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import formulas
            import numpy as np
            import schedula as sh
            from formulas.tokens.operand import XlError
    except ImportError as exc:
        raise EvaluatorUnavailableError(
            "Driver analysis needs the evaluator: pip install 'sheetguard[eval]'"
        ) from exc
    return formulas, np, sh, XlError


@dataclass(slots=True)
class _Compiled:
    fn: Callable[..., Any]
    inputs: tuple[str, ...]  # upper-case placeholder names, in call order


@dataclass(slots=True)
class _Binding:
    """What one placeholder stands for."""

    kind: str  # "cell" | "area" | "name" | "error"
    cell: CellKey | None = None
    area: tuple[str, int, int, int, int] | None = None
    name: NameNode | None = None
    error: str = "#REF!"


@dataclass(slots=True)
class _Step:
    node: Node
    compiled: _Compiled | None
    bindings: dict[str, _Binding]
    deps: tuple[Node, ...]  # formula nodes this step reads


@dataclass(slots=True)
class Plan:
    """Everything needed to (re)compute ``target``, in dependency order."""

    target: Node
    steps: list[_Step] = field(default_factory=list)
    index: dict[Node, int] = field(default_factory=dict)
    unsupported: set[str] = field(default_factory=set)
    reasons: list[str] = field(default_factory=list)
    _downstream: dict[Node, set[Node]] = field(default_factory=dict)

    @property
    def partial(self) -> bool:
        return bool(self.unsupported or self.reasons)


Values = dict[Node, Any]


class Evaluator:
    def __init__(self, graph: DepGraph) -> None:
        self.graph = graph
        self.wb = graph.wb
        self._formulas, self._np, self._sh, self._xlerror = _load_engine()
        self._parser = self._formulas.Parser()
        self._cache: dict[str, _Compiled | None] = {}
        table = self._formulas.get_functions()
        self._implemented = {
            name.upper().removeprefix("_XLFN.")
            for name, fn in table.items()
            if "not_implemented" not in repr(fn)
        } | _PARSER_FUNCTIONS

    # ------------------------------------------------------------------ planning

    def plan(self, target: Node) -> Plan:
        plan = Plan(target=target)
        order: list[Node] = []
        state: dict[Node, int] = {}  # 1 = visiting, 2 = done
        stack: list[tuple[Node, Iterable[Node]]] = []

        def formula_deps(node: Node) -> list[Node]:
            out: list[Node] = []
            for p in self.graph.precedents(node):
                if isinstance(p, RangeKey):
                    out.extend(c for c in self.graph.precedents(p) if self._has_formula(c))
                elif self._has_formula(p):
                    out.append(p)
            return list(dict.fromkeys(out))

        if not self._has_formula(target):
            return plan
        deps_of: dict[Node, list[Node]] = {}
        deps_of[target] = formula_deps(target)
        stack.append((target, iter(deps_of[target])))
        state[target] = 1
        while stack:
            node, it = stack[-1]
            for nxt in it:
                st = state.get(nxt)
                if st == 1:
                    plan.reasons.append("circular reference on the path")
                    continue
                if st is None:
                    state[nxt] = 1
                    deps_of[nxt] = formula_deps(nxt)
                    stack.append((nxt, iter(deps_of[nxt])))
                    break
            else:
                stack.pop()
                state[node] = 2
                order.append(node)

        for node in order:
            step = self._prepare(node, plan)
            step.deps = tuple(deps_of.get(node, ()))
            plan.index[node] = len(plan.steps)
            plan.steps.append(step)
            for d in step.deps:
                plan._downstream.setdefault(d, set()).add(node)
        plan.reasons = sorted(set(plan.reasons))
        return plan

    def _has_formula(self, node: Node) -> bool:
        if isinstance(node, NameNode):
            return True
        if isinstance(node, CellKey):
            cell = self.wb.cell(node)
            return cell is not None and cell.formula is not None
        return False

    def _formula_text(self, node: Node) -> tuple[str, str | None, CellKey | None]:
        if isinstance(node, NameNode):
            info = self.wb.defined_name(node.name, node.scope)
            return ("=" + info.text if info else "=#NAME?"), node.scope, None
        assert isinstance(node, CellKey)
        cell = self.wb.cell(node)
        assert cell is not None
        assert cell.formula is not None
        return cell.formula, node.sheet, node

    def _prepare(self, node: Node, plan: Plan) -> _Step:
        formula, sheet, origin = self._formula_text(node)
        where = format_cell(node) if isinstance(node, CellKey) else f"name {node}"
        info = analyze_formula(formula)
        if not info.ok:
            plan.reasons.append(f"{where}: formula could not be parsed")
            return _Step(node, None, {}, ())
        for fn in info.functions:
            if (
                fn in DYNAMIC_FUNCTIONS
                or fn in _REFERENCE_FUNCTIONS
                or (fn not in self._implemented and fn not in SHAPE_FUNCTIONS)
            ):
                plan.unsupported.add(fn)
        if node in self.graph.flags and self.graph.flags[node].external:
            plan.reasons.append(f"{where}: external workbook reference")
        tokens = tokenize(formula)
        assert tokens is not None
        parts: list[str] = []
        bindings: dict[str, _Binding] = {}
        skip = 0
        for i, tok in enumerate(tokens):
            if skip:
                skip -= 1
                continue
            if (
                tok.type == "FUNC"
                and tok.subtype == "OPEN"
                and tok.value[:-1].upper() in SHAPE_FUNCTIONS
            ):
                literal, used = self._shape_literal(tokens, i, sheet, origin, plan, where)
                if literal is None:
                    plan.unsupported.add(tok.value[:-1].upper())
                    parts.append(tok.value)
                    continue
                parts.append(literal)
                skip = used
                continue
            if tok.type == "OPERAND" and tok.subtype == "RANGE":
                name = f"_sg{len(bindings)}"
                bindings[name.upper()] = self._bind(tok.value, sheet, origin, plan, where)
                parts.append(name)
            elif tok.type == "WHITE-SPACE":
                parts.append(" ")
            else:
                parts.append(tok.value)
        text = "".join(parts)
        if not text.startswith("="):
            text = "=" + text
        compiled = self._compile(text)
        if compiled is None:
            plan.reasons.append(f"{where}: formula could not be compiled")
        return _Step(node, compiled, bindings, ())

    def _shape_literal(
        self,
        tokens: Sequence[Any],
        i: int,
        sheet: str | None,
        origin: CellKey | None,
        plan: Plan,
        where: str,
    ) -> tuple[str | None, int]:
        """ROW/ROWS/COLUMN/COLUMNS over a static reference -> a number literal."""
        fn = str(tokens[i].value[:-1]).upper()
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if nxt is not None and nxt.type == "FUNC" and nxt.subtype == "CLOSE":
            if origin is None or fn in ("ROWS", "COLUMNS"):
                return None, 0
            return str(origin.row if fn == "ROW" else origin.col), 1
        close = tokens[i + 2] if i + 2 < len(tokens) else None
        if nxt is None or close is None or nxt.subtype != "RANGE" or close.subtype != "CLOSE":
            return None, 0
        b = self._bind(nxt.value, sheet, origin, plan, where)
        if b.kind == "cell" and b.cell is not None:
            r1 = r2 = b.cell.row
            c1 = c2 = b.cell.col
        elif b.kind == "area" and b.area is not None:
            ref = parse_operand(nxt.value)
            _, r1, c1, r2, c2 = b.area
            if isinstance(ref, AreaRef):  # full extent, not clipped to the used range
                r2, c2 = ref.r2, ref.c2
        else:
            return None, 0
        value = {"ROW": r1, "COLUMN": c1, "ROWS": r2 - r1 + 1, "COLUMNS": c2 - c1 + 1}[fn]
        return str(value), 2

    def _bind(
        self, operand: str, sheet: str | None, origin: CellKey | None, plan: Plan, where: str
    ) -> _Binding:
        ref = parse_operand(operand)
        if isinstance(ref, AreaRef):
            sheets = self.graph.area_sheets(ref, sheet)
            if len(sheets) != 1:
                if len(sheets) > 1:
                    plan.reasons.append(f"{where}: 3D references are not evaluated")
                return _Binding("error", error="#REF!")
            s = sheets[0]
            if ref.is_cell:
                return _Binding("cell", cell=CellKey(s, ref.r1, ref.c1))
            used = self.wb.sheets[s].used
            r2, c2 = ref.r2, ref.c2
            if used is not None:  # keep the top-left anchor, clip only the far edge
                r2, c2 = min(r2, max(used.r2, ref.r1)), min(c2, max(used.c2, ref.c1))
            else:
                r2, c2 = ref.r1, ref.c1
            return _Binding("area", area=(s, ref.r1, ref.c1, r2, c2))
        nodes, flags = self.graph.resolve_ref(ref, sheet, origin)
        if isinstance(ref, StructRef):
            area = self.graph.struct_area(ref, origin, flags)
            if area is None:
                plan.reasons.append(f"{where}: unresolved table reference")
                return _Binding("error", error="#REF!")
            s, r1, c1, r2, c2 = area
            if r1 == r2 and c1 == c2:
                return _Binding("cell", cell=CellKey(s, r1, c1))
            return _Binding("area", area=area)
        if nodes and isinstance(nodes[0], NameNode):
            return _Binding("name", name=nodes[0])
        plan.reasons.append(f"{where}: unresolved reference {operand}")
        return _Binding("error", error="#NAME?")

    def _compile(self, text: str) -> _Compiled | None:
        if text in self._cache:
            return self._cache[text]
        compiled: _Compiled | None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                fn = self._parser.ast(text)[1].compile()
            compiled = _Compiled(fn=fn, inputs=tuple(str(i).upper() for i in fn.inputs))
        except Exception:
            compiled = None
        self._cache[text] = compiled
        return compiled

    # ---------------------------------------------------------------- evaluation

    def evaluate(
        self, plan: Plan, overrides: dict[Node, CellValue] | None = None, base: Values | None = None
    ) -> Values:
        """Compute every step of ``plan``. With ``base``, only recompute what
        ``overrides`` can affect and reuse ``base`` for everything else."""
        overrides = overrides or {}
        values: Values = dict(base) if base is not None else {}
        if base is None:
            todo = list(range(len(plan.steps)))
        else:
            dirty: set[Node] = set()
            frontier: list[Node] = list(overrides)
            for key in overrides:
                if isinstance(key, CellKey):
                    # constants are read through cells and ranges: find the steps reading them
                    frontier.extend(self._readers(plan, key))
            while frontier:
                n = frontier.pop()
                if n in dirty:
                    continue
                dirty.add(n)
                frontier.extend(plan._downstream.get(n, ()))
            todo = sorted(plan.index[n] for n in dirty if n in plan.index)
        for i in todo:
            step = plan.steps[i]
            if step.node in overrides:
                values[step.node] = self._to_engine(overrides[step.node], ValueType.NUMBER)
                continue
            values[step.node] = self._run(step, values, overrides)
        return values

    def _readers(self, plan: Plan, key: CellKey) -> list[Node]:
        out: list[Node] = []
        for d in self.graph.dependents(key):
            if isinstance(d, RangeKey):
                out.extend(self.graph.dependents(d))
            else:
                out.append(d)
        return [n for n in out if n in plan.index]

    def _run(self, step: _Step, values: Values, overrides: dict[Node, CellValue]) -> Any:
        if step.compiled is None:
            return self._xlerror("#VALUE!")
        args = [
            self._resolve(step.bindings.get(name), values, overrides)
            for name in step.compiled.inputs
        ]
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                result = step.compiled.fn(*args)
        except Exception:
            return self._xlerror("#VALUE!")
        np = self._np
        if isinstance(result, np.ndarray):
            if result.size == 0:
                return self._sh.EMPTY
            # Multi-cell results (a name that is a range, a spilled array) are kept whole;
            # a cell reference to them reads the anchor value via _scalar.
            return result if result.size > 1 else result.ravel()[0]
        return result

    def _resolve(self, b: _Binding | None, values: Values, overrides: dict[Node, CellValue]) -> Any:
        if b is None:
            return self._xlerror("#NAME?")
        if b.kind == "cell":
            assert b.cell is not None
            return self._scalar(self._cell_value(b.cell, values, overrides))
        if b.kind == "area":
            assert b.area is not None
            s, r1, c1, r2, c2 = b.area
            arr = self._np.empty((r2 - r1 + 1, c2 - c1 + 1), dtype=object)
            for r in range(r1, r2 + 1):
                for c in range(c1, c2 + 1):
                    arr[r - r1, c - c1] = self._scalar(
                        self._cell_value(CellKey(s, r, c), values, overrides)
                    )
            return arr
        if b.kind == "name":
            assert b.name is not None
            if b.name in overrides:
                return self._to_engine(overrides[b.name], ValueType.NUMBER)
            return values.get(b.name, self._xlerror("#NAME?"))
        return self._xlerror(b.error)

    def _scalar(self, v: Any) -> Any:
        if isinstance(v, self._np.ndarray):
            return v.ravel()[0] if v.size else self._sh.EMPTY
        return v

    def _cell_value(self, key: CellKey, values: Values, overrides: dict[Node, CellValue]) -> Any:
        if key in overrides:
            return self._to_engine(overrides[key], ValueType.NUMBER)
        if key in values:
            return values[key]
        cell = self.wb.cell(key)
        if cell is None:
            return self._sh.EMPTY
        # Constants, or (never on a well-formed plan) a formula outside it: last known value.
        return self._to_engine(cell.value, cell.vtype)

    def _to_engine(self, value: CellValue, vtype: ValueType) -> Any:
        if value is None:
            return self._sh.EMPTY
        if vtype is ValueType.ERROR and isinstance(value, str):
            return self._xlerror(value)
        if vtype is ValueType.DATE and isinstance(value, str):
            return iso_to_serial(value, self.wb.date_1904)
        return value

    def to_python(self, value: Any) -> CellValue:
        """Engine result to a JSON-friendly value (errors become their code)."""
        np = self._np
        if value is self._sh.EMPTY:
            return 0
        if isinstance(value, self._xlerror):
            return str(value)
        if isinstance(value, np.ndarray):
            return self.to_python(value.ravel()[0]) if value.size else None
        if isinstance(value, np.bool_ | bool):
            return bool(value)
        if isinstance(value, np.integer):
            return int(value)
        if isinstance(value, np.floating | float):
            return float(value)
        if isinstance(value, int | str):
            return value
        return str(value)


def iso_to_serial(text: str, date_1904: bool) -> float | str:
    """ISO date/time back to an Excel serial number (1900 system keeps the leap bug)."""
    try:
        if "T" in text or len(text) == 10:
            d = dt.datetime.fromisoformat(text)
        else:
            t = dt.time.fromisoformat(text)
            return (t.hour * 3600 + t.minute * 60 + t.second) / 86400.0
    except ValueError:
        return text
    epoch = dt.datetime(1904, 1, 1) if date_1904 else dt.datetime(1899, 12, 30)
    delta = d - epoch
    serial = delta.days + delta.seconds / 86400.0
    if not date_1904 and serial < 61:
        serial -= 1  # Excel pretends 1900-02-29 existed; serials before March are one lower
    return serial
