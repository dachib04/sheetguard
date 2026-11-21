"""Public Python API. The CLI and MCP server are thin wrappers around this."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from sheetguard.checks import CheckConfig, run_checks
from sheetguard.core.drivers import top_drivers
from sheetguard.core.evaluator import Evaluator
from sheetguard.core.graph import DepGraph
from sheetguard.core.loader import load_workbook
from sheetguard.core.model import Workbook
from sheetguard.core.refs import CellKey, format_cell, parse_cell_address
from sheetguard.core.tracer import Direction, trace
from sheetguard.errors import InvalidArgumentError
from sheetguard.report.json_report import build_report
from sheetguard.report.schema import DriversResult, Issue, Report, Severity, TraceResult
from sheetguard.security.envelope import Risk, annotate
from sheetguard.security.injection import Finding, cell_index, scan
from sheetguard.security.limits import DEFAULT_LIMITS, Limits


@dataclass(slots=True)
class Analysis:
    workbook: Workbook
    graph: DepGraph
    limits: Limits
    _evaluator: Evaluator | None = field(default=None, repr=False)
    _untrusted: list[Finding] | None = field(default=None, repr=False)
    _index: dict[str, tuple[Risk, tuple[str, ...]]] | None = field(default=None, repr=False)

    @property
    def untrusted(self) -> list[Finding]:
        """Injection-scanner findings (§7.2), computed once per workbook."""
        if self._untrusted is None:
            self._untrusted = scan(self.workbook)
        return self._untrusted

    @property
    def untrusted_index(self) -> dict[str, tuple[Risk, tuple[str, ...]]]:
        if self._index is None:
            self._index = cell_index(self.untrusted)
        return self._index

    def audit(
        self,
        checks: Iterable[str] | None = None,
        min_severity: Severity = Severity.INFO,
        config: CheckConfig | None = None,
    ) -> list[Issue]:
        issues = run_checks(self.workbook, self.graph, checks, Severity(min_severity), config)
        return [annotate(i, self.untrusted_index) for i in issues]

    def resolve_cell(self, address: str) -> CellKey:
        try:
            key = parse_cell_address(address)
        except ValueError as exc:
            raise InvalidArgumentError(str(exc)) from exc
        canonical = self.workbook.sheet_name(key.sheet)
        if canonical is None:
            raise InvalidArgumentError("Unknown sheet in cell address.")
        return CellKey(canonical, key.row, key.col)

    def trace(
        self,
        address: str,
        direction: Direction = "precedents",
        max_depth: int | None = None,
        max_nodes: int | None = None,
    ) -> TraceResult:
        if direction not in ("precedents", "dependents"):
            raise InvalidArgumentError("direction must be 'precedents' or 'dependents'")
        depth = max_depth or self.limits.trace_max_depth
        nodes = max_nodes or self.limits.trace_max_nodes
        if depth > 50 or nodes > 10_000:
            raise InvalidArgumentError("max_depth must be <= 50 and max_nodes <= 10000")
        result = trace(self.graph, self.resolve_cell(address), direction, depth, nodes)
        return annotate(result, self.untrusted_index)

    def likely_outputs(self, limit: int = 10) -> list[tuple[str, int]]:
        return [(format_cell(k), n) for k, n in self.graph.likely_outputs(limit)]

    def drivers(
        self,
        target: str,
        n: int = 10,
        delta_pct: float = 10.0,
        zero_delta: float = 1.0,
        include_index_inputs: bool = False,
    ) -> DriversResult:
        """FR-20..24: rank the inputs of ``target`` by their effect on it."""
        if self._evaluator is None:
            self._evaluator = Evaluator(self.graph)
        result = top_drivers(
            self.graph,
            self.resolve_cell(target),
            n=n,
            delta_pct=delta_pct,
            zero_delta=zero_delta,
            max_inputs=self.limits.max_driver_inputs,
            include_index_inputs=include_index_inputs,
            evaluator=self._evaluator,
        )
        return annotate(result, self.untrusted_index)

    def report(
        self,
        checks: Iterable[str] | None = None,
        min_severity: Severity = Severity.INFO,
        config: CheckConfig | None = None,
    ) -> Report:
        selected = list(checks) if checks is not None else None
        min_severity = Severity(min_severity)
        issues = self.audit(selected, min_severity, config)
        return build_report(self, issues, selected, min_severity)


def analyze(path: str | Path, limits: Limits = DEFAULT_LIMITS) -> Analysis:
    """Load a workbook safely and build its dependency graph."""
    wb = load_workbook(path, limits)
    return Analysis(workbook=wb, graph=DepGraph(wb), limits=limits)
