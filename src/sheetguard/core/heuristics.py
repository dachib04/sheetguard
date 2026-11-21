"""Small, shared heuristics about what a cell *means* (documented in docs/checks.md)."""

from __future__ import annotations

import re

from sheetguard.core.model import Cell, ValueType, Workbook
from sheetguard.core.refs import CellKey

_ID_HEADER = re.compile(
    r"\b(id|ids|code|codes|zip|postal|postcode|phone|tel|account|acct|sku|ref|iban|"
    r"no\.?|number|#|ticker|isin|cusip|key)\b",
    re.IGNORECASE,
)


_NUMERIC_TEXT = re.compile(r"[\d\s.,$€£¥%()+\-]+")


def _int_value(cell: Cell | None) -> int | None:
    if cell is None or cell.formula is not None or cell.vtype is not ValueType.NUMBER:
        return None
    v = cell.value
    if isinstance(v, bool) or not isinstance(v, int | float) or float(v) != int(v):
        return None
    return int(v)


def is_index_like(wb: Workbook, key: CellKey) -> bool:
    """An integer constant in a run of consecutive integers (years, periods, row numbers).

    Such cells are labels, not assumptions: perturbing "2026" by 10% is meaningless.
    """
    sheet = wb.sheets.get(key.sheet)
    if sheet is None:
        return False
    v = _int_value(sheet.cells.get((key.row, key.col)))
    if v is None:
        return False
    for dr, dc in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        w = _int_value(sheet.cells.get((key.row + dr, key.col + dc)))
        if w is not None and abs(w - v) == 1:
            return True
        # a roll-forward header (=B1+1) next to the seed value also counts
        nb = sheet.cells.get((key.row + dr, key.col + dc))
        if (
            nb is not None
            and nb.formula is not None
            and isinstance(nb.value, int | float)
            and not isinstance(nb.value, bool)
            and abs(float(nb.value) - v) == 1
        ):
            return True
    return False


def column_header(wb: Workbook, key: CellKey, max_up: int = 50) -> str | None:
    """Nearest text constant above the cell in the same column."""
    sheet = wb.sheets.get(key.sheet)
    if sheet is None:
        return None
    for r in range(key.row - 1, max(0, key.row - max_up), -1):
        c = sheet.cells.get((r, key.col))
        if (
            c is not None
            and c.formula is None
            and c.vtype is ValueType.TEXT
            and c.value
            and not _NUMERIC_TEXT.fullmatch(str(c.value))
        ):
            return str(c.value)
    return None


def row_label(wb: Workbook, key: CellKey, max_left: int = 30) -> str | None:
    """The text label for a row: the first non-empty cell to the left, if it is text."""
    sheet = wb.sheets.get(key.sheet)
    if sheet is None:
        return None
    for c in range(key.col - 1, max(0, key.col - max_left), -1):
        cell = sheet.cells.get((key.row, c))
        if cell is None or cell.is_empty:
            continue
        if cell.formula is None and cell.vtype is ValueType.TEXT:
            return str(cell.value)
        return None  # another block starts here: no label for this cell
    return None


def is_id_column(wb: Workbook, key: CellKey) -> bool:
    header = column_header(wb, key)
    return bool(header and _ID_HEADER.search(header))
