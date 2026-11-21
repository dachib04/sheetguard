"""Public, versioned output models (FR-11, FR-40, §5.3).

Anything that leaves the core library is one of these Pydantic models. Field
names are part of the public contract: bump ``SCHEMA_VERSION`` on any change.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from sheetguard.security.envelope import UntrustedText

SCHEMA_VERSION = "0.1.0"

JsonScalar = float | int | bool | None
OutValue = JsonScalar | UntrustedText


class Severity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"

    @property
    def rank(self) -> int:
        return {"critical": 0, "warning": 1, "info": 2}[self.value]


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Evidence(_Model):
    address: str
    formula: UntrustedText | None = None
    value: OutValue = None
    note: str | None = None


class Issue(_Model):
    id: str
    check_id: str
    severity: Severity
    sheet: str
    cell: str  # address of the anchor cell, or a range "Sheet!A1:B5"
    description: str
    evidence: tuple[Evidence, ...] = ()
    confidence: Confidence = Confidence.HIGH
    suggested_fix: UntrustedText | None = None
    affected_count: int = 1


class TraceNode(_Model):
    address: str
    kind: Literal["cell", "range", "name"]
    formula: UntrustedText | None = None
    value: OutValue = None
    type: str = "empty"
    is_input: bool = False
    partial: bool = False
    partial_reasons: tuple[str, ...] = ()
    note: str | None = None  # "shown above", "cycle", "N more not shown"
    children: tuple[TraceNode, ...] = ()


class TraceResult(_Model):
    schema_version: str = SCHEMA_VERSION
    root: TraceNode
    direction: Literal["precedents", "dependents"]
    node_count: int
    truncated: bool
    truncation_reasons: tuple[str, ...] = ()
    partial: bool = Field(default=False, description="Some dependencies are not statically known")


class DriverEntry(_Model):
    input: str
    kind: Literal["cell", "name"]
    input_value: float
    delta: float  # the absolute change applied in each direction
    zero_input: bool = Field(default=False, description="Input was 0: absolute delta used")
    target_up: OutValue
    target_down: OutValue
    change_up: float | None
    change_down: float | None
    abs_change: float  # max(|change_up|, |change_down|), ranking key
    pct_of_baseline: float | None = None


class DriversResult(_Model):
    schema_version: str = SCHEMA_VERSION
    target: str
    baseline: OutValue
    cached_value: OutValue
    delta_pct: float
    zero_delta: float
    drivers: tuple[DriverEntry, ...] = ()
    inputs_found: int = 0
    inputs_analysed: int = 0
    non_numeric_inputs: int = 0
    index_inputs_skipped: int = 0
    capped: bool = False
    partial: bool = False
    unsupported_functions: tuple[str, ...] = ()
    partial_reasons: tuple[str, ...] = ()


class SheetSummary(_Model):
    name: str
    index: int
    state: Literal["visible", "hidden", "veryHidden"]
    used_range: str | None
    cells: int
    formulas: int
    hidden_rows: int
    hidden_columns: int
    protected: bool
    tables: tuple[str, ...] = ()


class WorkbookFlags(_Model):
    macros_present: bool
    vba: bool
    xlm_macros: bool
    external_links: bool
    dde: bool
    ole_objects: bool
    activex: bool
    data_connections: bool
    hidden_sheets: int
    very_hidden_sheets: int
    cached_values: bool
    calc_mode: str
    iterative_calc: bool
    date_1904: bool
    workbook_protected: bool
    untrusted_content_found: bool = False


class WorkbookInfo(_Model):
    file_name: str
    sha256: str
    size_bytes: int
    sheet_count: int
    cell_count: int
    formula_count: int
    defined_names: int
    tables: int
    flags: WorkbookFlags
    sheets: tuple[SheetSummary, ...]


class OutputCandidate(_Model):
    address: str
    upstream_cells: int


class Summary(_Model):
    total: int
    by_severity: dict[str, int]
    by_check: dict[str, int]


class ReportOptions(_Model):
    checks: tuple[str, ...]
    min_severity: Severity


class UntrustedFinding(_Model):
    """One flagged text item (§6 list_untrusted_content, §7.2)."""

    id: str
    vector: str
    location: str
    risk: Literal["low", "medium", "high"]
    score: int
    signals: tuple[str, ...]
    reasons: tuple[str, ...]
    text: UntrustedText


class UntrustedSummary(_Model):
    total: int
    by_risk: dict[str, int]
    by_vector: dict[str, int]


class Report(_Model):
    schema_version: str = SCHEMA_VERSION
    tool: str = "sheetguard"
    tool_version: str
    workbook: WorkbookInfo
    options: ReportOptions
    summary: Summary
    likely_outputs: tuple[OutputCandidate, ...] = ()
    issues: tuple[Issue, ...] = ()
    untrusted_summary: UntrustedSummary | None = None
    untrusted_content: tuple[UntrustedFinding, ...] = ()
