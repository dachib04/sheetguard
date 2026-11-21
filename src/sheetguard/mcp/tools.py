"""The eight MCP tools as plain, synchronous, testable methods (§6, FR-50..53).

``server.py`` only adds the MCP wiring (async, timeouts, error mapping). Nothing
here reads anything outside the allowed root or writes anything at all.
"""

from __future__ import annotations

import json
import logging
import secrets
import sys
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import BaseModel

from sheetguard.api import Analysis, analyze
from sheetguard.checks import REGISTRY
from sheetguard.checks.base import CheckConfig, CheckContext, cell_evidence
from sheetguard.core.graph import NameNode, format_node
from sheetguard.core.refs import (
    CellKey,
    RangeKey,
    format_cell,
    parse_cell_address,
    parse_range_address,
)
from sheetguard.core.tracer import out_value
from sheetguard.errors import InvalidArgumentError, UnknownWorkbookError
from sheetguard.mcp.models import (
    AuditResult,
    CloseResult,
    ExplainIssueResult,
    GetRangeResult,
    OpenWorkbookResult,
    RangeCell,
    SheetBrief,
    TopDriversResult,
    TraceCellResult,
    UntrustedListResult,
)
from sheetguard.report.json_report import build_report, to_model
from sheetguard.report.schema import Evidence, Issue, Severity, Summary, UntrustedSummary
from sheetguard.security.envelope import RISK_RANK, UntrustedText, annotate, wrap
from sheetguard.security.limits import DEFAULT_LIMITS, Limits
from sheetguard.security.paths import resolve_inside_root

log = logging.getLogger("sheetguard.mcp")
M = TypeVar("M", bound=BaseModel)
MAX_RANGE_CELLS = 200
MAX_PAGE = 100


# ------------------------------------------------------------------- store


@dataclass(slots=True)
class _Entry:
    analysis: Analysis
    last_used: float
    issues: list[Issue] | None = None


@dataclass
class WorkbookStore:
    """Open workbooks: LRU with a maximum count and an idle timeout (FR-52)."""

    root: Path
    limits: Limits = DEFAULT_LIMITS
    max_open: int = 8
    idle_timeout_s: float = 900.0
    clock: Callable[[], float] = time.monotonic
    _items: OrderedDict[str, _Entry] = field(default_factory=OrderedDict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def open(self, user_path: str) -> tuple[str, Analysis]:
        real = resolve_inside_root(user_path, self.root)
        analysis = analyze(real, self.limits)
        # Same content -> same id; a random suffix keeps ids unguessable across sessions.
        wid = f"wb_{analysis.workbook.sha256[:12]}_{secrets.token_hex(3)}"
        with self._lock:
            self._evict_idle()
            for existing, entry in self._items.items():
                if entry.analysis.workbook.sha256 == analysis.workbook.sha256:
                    entry.last_used = self.clock()
                    self._items.move_to_end(existing)
                    return existing, entry.analysis
            self._items[wid] = _Entry(analysis, self.clock())
            while len(self._items) > self.max_open:
                old, _ = self._items.popitem(last=False)
                log.info("evicted workbook %s (LRU)", old)
        return wid, analysis

    def get(self, workbook_id: str) -> _Entry:
        with self._lock:
            self._evict_idle()
            entry = self._items.get(workbook_id)
            if entry is None:
                raise UnknownWorkbookError(
                    "Unknown or expired workbook_id. Call open_workbook again."
                )
            entry.last_used = self.clock()
            self._items.move_to_end(workbook_id)
            return entry

    def close(self, workbook_id: str) -> bool:
        with self._lock:
            return self._items.pop(workbook_id, None) is not None

    def _evict_idle(self) -> None:
        now = self.clock()
        for wid in [w for w, e in self._items.items() if now - e.last_used > self.idle_timeout_s]:
            del self._items[wid]
            log.info("evicted workbook %s (idle)", wid)

    def __len__(self) -> int:
        return len(self._items)


# ------------------------------------------------------------ size fitting


def json_size(model: BaseModel) -> int:
    return len(model.model_dump_json().encode("utf-8"))


def shrink_texts(obj: Any, max_len: int) -> Any:
    """Copy with every UntrustedText cut to ``max_len`` characters (marked truncated)."""
    if isinstance(obj, UntrustedText):
        if len(obj.text) <= max_len:
            return obj
        return obj.model_copy(
            update={
                "text": obj.text[:max_len],
                "truncated": True,
                "original_length": obj.original_length or len(obj.text),
            }
        )
    if isinstance(obj, BaseModel):
        updates = {n: shrink_texts(getattr(obj, n), max_len) for n in type(obj).model_fields}
        changed = {k: v for k, v in updates.items() if v is not getattr(obj, k)}
        return obj.model_copy(update=changed) if changed else obj
    if isinstance(obj, tuple):
        return tuple(shrink_texts(v, max_len) for v in obj)
    if isinstance(obj, dict):
        return {k: shrink_texts(v, max_len) for k, v in obj.items()}
    return obj


def fit(model: M, max_bytes: int) -> M:
    """Shrink workbook text inside ``model`` until it fits (FR-51)."""
    for max_len in (2000, 500, 200, 80):
        if json_size(model) <= max_bytes:
            return model
        model = shrink_texts(model, max_len)
    return model


T = TypeVar("T")


def paginate(
    items: Sequence[T],
    cursor: str | None,
    limit: int,
    max_bytes: int,
    build: Callable[[tuple[T, ...], str | None], M],
) -> M:
    """Largest page starting at ``cursor`` (<= ``limit``) whose JSON fits ``max_bytes``."""
    start = _offset(cursor, len(items))
    n = max(1, min(limit, len(items) - start)) if len(items) > start else 0
    while True:
        page = tuple(items[start : start + n])
        nxt = str(start + n) if start + n < len(items) else None
        model = fit(build(page, nxt), max_bytes)
        if json_size(model) <= max_bytes or n <= 1:
            return model
        n = max(1, int(n * 0.6))


def _offset(cursor: str | None, total: int) -> int:
    if cursor in (None, ""):
        return 0
    assert cursor is not None
    if not cursor.isdigit() or int(cursor) > total:
        raise InvalidArgumentError("Invalid cursor; use next_cursor from the previous page.")
    return int(cursor)


# ------------------------------------------------------------------- tools


Direction = Literal["precedents", "dependents"]


class SheetGuardTools:
    def __init__(self, store: WorkbookStore) -> None:
        self.store = store
        self.max_bytes = store.limits.max_response_bytes

    # 1
    def open_workbook(self, path: str) -> OpenWorkbookResult:
        wid, a = self.store.open(path)
        report = build_report(a, [], [], Severity.INFO)
        info = report.workbook
        us = report.untrusted_summary or UntrustedSummary(total=0, by_risk={}, by_vector={})
        sheets = tuple(
            SheetBrief(
                name=s.name,
                state=s.state,
                used_range=s.used_range,
                cells=s.cells,
                formulas=s.formulas,
            )
            for s in info.sheets
        )

        def build(n: int) -> OpenWorkbookResult:
            return OpenWorkbookResult(
                workbook_id=wid,
                file_name=info.file_name,
                sha256=info.sha256,
                sheet_count=info.sheet_count,
                cell_count=info.cell_count,
                formula_count=info.formula_count,
                defined_names=info.defined_names,
                tables=info.tables,
                flags=info.flags,
                untrusted_summary=us,
                sheets=sheets[:n],
                sheets_truncated=n < len(sheets),
                likely_outputs=report.likely_outputs[:5],
            )

        n = len(sheets)
        result = build(n)
        while json_size(result) > self.max_bytes and n > 1:
            n = max(1, n // 2)
            result = build(n)
        return result

    # 2
    def audit_workbook(
        self,
        workbook_id: str,
        checks: list[str] | None = None,
        min_severity: Severity = Severity.INFO,
        limit: int = 20,
        cursor: str | None = None,
    ) -> AuditResult:
        _check_limit(limit)
        entry = self.store.get(workbook_id)
        if entry.issues is None:
            entry.issues = entry.analysis.audit()
        wanted = set(checks) if checks else set(REGISTRY)
        unknown = wanted - set(REGISTRY)
        if unknown:
            raise InvalidArgumentError(f"Unknown check id(s): {', '.join(sorted(unknown))}")
        sev = Severity(min_severity)
        matching = [i for i in entry.issues if i.check_id in wanted and i.severity.rank <= sev.rank]
        by_sev = Counter(i.severity.value for i in matching)
        summary = Summary(
            total=len(matching),
            by_severity={s.value: by_sev.get(s.value, 0) for s in Severity},
            by_check=dict(sorted(Counter(i.check_id for i in matching).items())),
        )
        return paginate(
            matching,
            cursor,
            limit,
            self.max_bytes,
            lambda page, nxt: AuditResult(
                workbook_id=workbook_id,
                summary=summary,
                issues=page,
                total_matching=len(matching),
                next_cursor=nxt,
            ),
        )

    # 3
    def trace_cell(
        self, workbook_id: str, cell: str, direction: Direction = "precedents", max_depth: int = 6
    ) -> TraceCellResult:
        a = self.store.get(workbook_id).analysis
        nodes = a.limits.trace_max_nodes
        while True:
            t = a.trace(cell, direction, max_depth, nodes)
            result = fit(TraceCellResult(workbook_id=workbook_id, trace=t), self.max_bytes)
            if json_size(result) <= self.max_bytes or nodes <= 5:
                break
            nodes = max(5, nodes // 2)
        if nodes < a.limits.trace_max_nodes:
            reasons = tuple(sorted({*result.trace.truncation_reasons, "response_size"}))
            result = result.model_copy(
                update={
                    "trace": result.trace.model_copy(
                        update={"truncated": True, "truncation_reasons": reasons}
                    )
                }
            )
        return result

    # 4
    def top_drivers(
        self, workbook_id: str, target_cell: str, n: int = 10, delta_pct: float = 10.0
    ) -> TopDriversResult:
        a = self.store.get(workbook_id).analysis
        result = a.drivers(target_cell, n=n, delta_pct=delta_pct)
        return fit(TopDriversResult(workbook_id=workbook_id, result=result), self.max_bytes)

    # 5
    def explain_issue(self, workbook_id: str, issue_id: str) -> ExplainIssueResult:
        entry = self.store.get(workbook_id)
        a = entry.analysis
        if entry.issues is None:
            entry.issues = a.audit()
        issue = next((i for i in entry.issues if i.id == issue_id), None)
        if issue is None:
            raise InvalidArgumentError("Unknown issue_id for this workbook.")
        cls = REGISTRY[issue.check_id]
        policy = getattr(sys.modules.get(cls.__module__), "__doc__", None) or ""
        ctx = CheckContext(a.workbook, a.graph, CheckConfig())
        context: list[Any] = []
        precedents: list[Any] = []
        anchor = _anchor(issue)
        if anchor is not None:
            sheet = a.workbook.sheet(anchor.sheet)
            if sheet is not None:
                for r in range(max(1, anchor.row - 1), anchor.row + 2):
                    for c in range(max(1, anchor.col - 2), anchor.col + 3):
                        if (r, c) in sheet.cells:
                            key = CellKey(sheet.name, r, c)
                            context.append(
                                cell_evidence(ctx, key, "anchor" if key == anchor else None)
                            )
                for p in a.graph.precedents(anchor)[:10]:
                    if isinstance(p, CellKey):
                        precedents.append(cell_evidence(ctx, p))
                    elif isinstance(p, RangeKey | NameNode):
                        precedents.append(
                            Evidence(
                                address=format_node(p),
                                note="range" if isinstance(p, RangeKey) else "defined name",
                            )
                        )
        result = ExplainIssueResult(
            workbook_id=workbook_id,
            issue=issue,
            check_title=cls.title,
            check_policy=policy.strip(),
            context=tuple(context),
            precedents=tuple(precedents),
        )
        return fit(annotate(result, a.untrusted_index), self.max_bytes)

    # 6
    def get_range(
        self, workbook_id: str, cell_range: str, cursor: str | None = None
    ) -> GetRangeResult:
        a = self.store.get(workbook_id).analysis
        try:
            rk = parse_range_address(cell_range)
        except ValueError:
            try:
                k = parse_cell_address(cell_range)
                rk = RangeKey(k.sheet, k.row, k.col, k.row, k.col)
            except ValueError as exc:
                raise InvalidArgumentError("Expected a range like Sheet1!A1:C10.") from exc
        sheet = a.workbook.sheet(rk.sheet)
        if sheet is None:
            raise InvalidArgumentError("Unknown sheet in range.")
        if rk.size > MAX_RANGE_CELLS:
            raise InvalidArgumentError(
                f"Range has {rk.size:,} cells; the maximum is "
                f"{MAX_RANGE_CELLS}. Ask for a smaller range."
            )
        cells: list[RangeCell] = []
        for r in range(rk.r1, rk.r2 + 1):
            for c in range(rk.c1, rk.c2 + 1):
                cell = sheet.cells.get((r, c))
                if cell is None or cell.is_empty:
                    continue
                addr = format_cell(CellKey(sheet.name, r, c))
                cells.append(
                    RangeCell(
                        address=addr,
                        type=cell.vtype.value,
                        formula=wrap(cell.formula, f"{addr} (formula)") if cell.formula else None,
                        value=out_value(cell.value, cell.vtype, addr),
                    )
                )
        label = (
            f"{format_cell(CellKey(sheet.name, rk.r1, rk.c1))}:"
            f"{format_cell(CellKey(sheet.name, rk.r2, rk.c2)).split('!')[-1]}"
        )
        omitted = rk.size - len(cells)
        index = a.untrusted_index
        return paginate(
            cells,
            cursor,
            MAX_RANGE_CELLS,
            self.max_bytes,
            lambda page, nxt: annotate(
                GetRangeResult(
                    workbook_id=workbook_id,
                    range=label,
                    cells=page,
                    empty_cells_omitted=omitted,
                    next_cursor=nxt,
                ),
                index,
            ),
        )

    # 7
    def list_untrusted_content(
        self,
        workbook_id: str,
        min_risk: Literal["low", "medium", "high"] = "low",
        limit: int = 50,
        cursor: str | None = None,
    ) -> UntrustedListResult:
        _check_limit(limit)
        a = self.store.get(workbook_id).analysis
        floor = RISK_RANK[min_risk]
        items = [to_model(f) for f in a.untrusted if RISK_RANK[f.risk] >= floor]
        summary = UntrustedSummary(
            total=len(items),
            by_risk={r: sum(1 for f in items if f.risk == r) for r in ("high", "medium", "low")},
            by_vector=dict(sorted(Counter(f.vector for f in items).items())),
        )
        return paginate(
            items,
            cursor,
            limit,
            self.max_bytes,
            lambda page, nxt: UntrustedListResult(
                workbook_id=workbook_id, summary=summary, items=page, next_cursor=nxt
            ),
        )

    # 8
    def close_workbook(self, workbook_id: str) -> CloseResult:
        ok = self.store.close(workbook_id)
        return CloseResult(workbook_id=workbook_id, ok=True, status="closed" if ok else "not_open")


def _check_limit(limit: int) -> None:
    if not 1 <= limit <= MAX_PAGE:
        raise InvalidArgumentError(f"limit must be between 1 and {MAX_PAGE}")


def _anchor(issue: Issue) -> CellKey | None:
    try:
        return parse_cell_address(issue.cell.split(":")[0].split(" ")[0])
    except ValueError:
        return None


def error_payload(code: str, message: str) -> str:
    return json.dumps({"code": code, "message": message}, sort_keys=True)
