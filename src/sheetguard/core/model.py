"""Immutable in-memory workbook model (FR-3, FR-4, FR-5).

The model is produced once by ``SafeLoader`` and never mutated afterwards. The
evaluator works on its own copy of the values it needs. Cells use frozen,
slotted dataclasses (not Pydantic) because a large model holds millions of them;
every *public* input and output is a Pydantic model (see ``report.schema``).
"""

from __future__ import annotations

import bisect
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum

from sheetguard.core.refs import CellKey, RangeKey, format_cell

CellValue = float | int | str | bool | None


class ValueType(StrEnum):
    NUMBER = "number"
    TEXT = "text"
    BOOL = "bool"
    DATE = "date"
    ERROR = "error"
    EMPTY = "empty"


ERROR_VALUES = frozenset(
    {
        "#REF!",
        "#DIV/0!",
        "#N/A",
        "#VALUE!",
        "#NAME?",
        "#NUM!",
        "#NULL!",
        "#SPILL!",
        "#CALC!",
        "#GETTING_DATA",
        "#FIELD!",
        "#BLOCKED!",
        "#CONNECT!",
        "#BUSY!",
        "#UNKNOWN!",
        "#EXTERNAL!",
    }
)


@dataclass(frozen=True, slots=True)
class Style:
    """Only the formatting the injection scanner needs (FR-5)."""

    font_color: str | None = None  # "rgb:FFFFFFFF" | "theme:0:0.0" | "indexed:9"
    fill_color: str | None = None
    font_size: float | None = None


NO_STYLE = Style()


@dataclass(frozen=True, slots=True)
class Cell:
    row: int
    col: int
    formula: str | None  # full text including the leading "="
    value: CellValue  # constant value, or the cached value for a formula cell
    vtype: ValueType
    number_format: str = "General"
    style: Style = NO_STYLE
    is_array: bool = False
    array_ref: str | None = None  # spill / CSE range of an array formula anchor
    comment: str | None = None
    hyperlink: str | None = None

    @property
    def is_formula(self) -> bool:
        return self.formula is not None

    @property
    def is_empty(self) -> bool:
        return self.formula is None and (self.value is None or self.value == "")


@dataclass(frozen=True, slots=True)
class DataValidationText:
    sqref: str
    prompt_title: str | None
    prompt: str | None
    error_title: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class DefinedNameInfo:
    name: str
    scope_sheet: str | None  # None = workbook scope
    text: str  # the formula / reference text, without "="
    hidden: bool


@dataclass(frozen=True, slots=True)
class TableInfo:
    name: str
    display_name: str
    sheet: str
    ref: RangeKey
    header_rows: int
    totals_rows: int
    columns: tuple[str, ...]

    def column_index(self, name: str) -> int | None:
        low = name.strip().lower()
        for i, col in enumerate(self.columns):
            if col.strip().lower() == low:
                return i
        return None


@dataclass(frozen=True, slots=True)
class ActiveContent:
    """Things we detect and report but never execute or follow (FR-1, S-2, S-9)."""

    has_vba: bool = False
    has_xlm_macros: bool = False
    has_external_links: bool = False
    has_dde: bool = False
    has_ole_objects: bool = False
    has_activex: bool = False
    has_data_connections: bool = False
    external_link_targets: tuple[str, ...] = ()

    @property
    def any(self) -> bool:
        return any(
            (
                self.has_vba,
                self.has_xlm_macros,
                self.has_external_links,
                self.has_dde,
                self.has_ole_objects,
                self.has_activex,
                self.has_data_connections,
            )
        )


@dataclass(slots=True)
class Sheet:
    name: str
    index: int
    state: str  # "visible" | "hidden" | "veryHidden"
    cells: dict[tuple[int, int], Cell]
    hidden_rows: frozenset[int] = frozenset()
    hidden_cols: frozenset[int] = frozenset()
    merged: tuple[RangeKey, ...] = ()
    protected: bool = False
    data_validations: tuple[DataValidationText, ...] = ()
    headers_footers: tuple[tuple[str, str], ...] = ()  # (location, text)
    used: RangeKey | None = None
    _col_index: dict[int, list[int]] | None = field(default=None, repr=False)

    @property
    def is_hidden(self) -> bool:
        return self.state != "visible"

    def is_cell_hidden(self, row: int, col: int) -> bool:
        return self.is_hidden or row in self.hidden_rows or col in self.hidden_cols

    def _index(self) -> dict[int, list[int]]:
        if self._col_index is None:
            idx: dict[int, list[int]] = {}
            for r, c in self.cells:
                idx.setdefault(c, []).append(r)
            for rows in idx.values():
                rows.sort()
            self._col_index = idx
        return self._col_index

    def cells_in(self, r1: int, c1: int, r2: int, c2: int) -> Iterator[Cell]:
        """Existing cells inside the area, column-major, without scanning empties."""
        idx = self._index()
        if c2 - c1 + 1 > len(idx):
            cols = sorted(c for c in idx if c1 <= c <= c2)
        else:
            cols = [c for c in range(c1, c2 + 1) if c in idx]
        for c in cols:
            rows = idx[c]
            lo = bisect.bisect_left(rows, r1)
            hi = bisect.bisect_right(rows, r2)
            for r in rows[lo:hi]:
                yield self.cells[(r, c)]


@dataclass(slots=True)
class Workbook:
    file_name: str  # basename only, never a full path
    sha256: str
    size_bytes: int
    sheets: dict[str, Sheet]  # in workbook order
    names: tuple[DefinedNameInfo, ...] = ()
    tables: tuple[TableInfo, ...] = ()
    properties: tuple[tuple[str, str], ...] = ()
    calc_mode: str = "auto"
    iterate: bool = False
    date_1904: bool = False
    workbook_protected: bool = False
    active_content: ActiveContent = field(default_factory=ActiveContent)
    has_cached_values: bool = True
    chartsheet_count: int = 0
    # (vector, location, text) from parts openpyxl does not expose: threaded comments,
    # shapes, charts. Only the injection scanner reads these.
    extra_texts: tuple[tuple[str, str, str], ...] = ()
    _sheet_lookup: dict[str, str] = field(default_factory=dict, repr=False)
    _name_lookup: dict[tuple[str | None, str], DefinedNameInfo] = field(
        default_factory=dict, repr=False
    )
    _table_lookup: dict[str, TableInfo] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._sheet_lookup = {name.lower(): name for name in self.sheets}
        self._name_lookup = {(n.scope_sheet, n.name.lower()): n for n in self.names}
        for t in self.tables:
            self._table_lookup[t.display_name.lower()] = t
            self._table_lookup.setdefault(t.name.lower(), t)

    # ---- lookups (Excel names are case-insensitive)

    def sheet_name(self, name: str) -> str | None:
        return self._sheet_lookup.get(name.lower())

    def sheet(self, name: str) -> Sheet | None:
        canonical = self.sheet_name(name)
        return self.sheets[canonical] if canonical else None

    def sheet_span(self, first: str, last: str) -> list[str]:
        """Sheets of a 3D reference, in workbook order (either direction)."""
        order = list(self.sheets)
        a, b = self.sheet_name(first), self.sheet_name(last)
        if a is None or b is None:
            return []
        i, j = order.index(a), order.index(b)
        return order[min(i, j) : max(i, j) + 1]

    def cell(self, key: CellKey) -> Cell | None:
        sheet = self.sheet(key.sheet)
        return sheet.cells.get((key.row, key.col)) if sheet else None

    def defined_name(self, name: str, from_sheet: str | None) -> DefinedNameInfo | None:
        """Sheet-scoped names shadow workbook-scoped ones (FR-4)."""
        low = name.lower()
        if from_sheet is not None:
            canonical = self.sheet_name(from_sheet)
            hit = self._name_lookup.get((canonical, low))
            if hit:
                return hit
        return self._name_lookup.get((None, low))

    def table(self, name: str) -> TableInfo | None:
        return self._table_lookup.get(name.lower())

    def table_at(self, sheet: str, row: int, col: int) -> TableInfo | None:
        canonical = self.sheet_name(sheet)
        for t in self.tables:
            if t.sheet == canonical and t.ref.contains(row, col):
                return t
        return None

    # ---- iteration

    def iter_cells(self) -> Iterator[tuple[CellKey, Cell]]:
        for sheet in self.sheets.values():
            for (r, c), cell in sorted(sheet.cells.items(), key=lambda kv: (kv[0][0], kv[0][1])):
                yield CellKey(sheet.name, r, c), cell

    def iter_formula_cells(self) -> Iterator[tuple[CellKey, Cell]]:
        for key, cell in self.iter_cells():
            if cell.formula is not None:
                yield key, cell

    @property
    def cell_count(self) -> int:
        return sum(len(s.cells) for s in self.sheets.values())

    @property
    def formula_count(self) -> int:
        return sum(1 for s in self.sheets.values() for c in s.cells.values() if c.formula)

    def address(self, key: CellKey) -> str:
        return format_cell(key)
