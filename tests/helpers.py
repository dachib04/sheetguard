"""Test helpers: build workbook models in memory, and write small .xlsx files."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import openpyxl

from sheetguard.core.graph import DepGraph
from sheetguard.core.model import (
    Cell,
    DefinedNameInfo,
    Sheet,
    TableInfo,
    ValueType,
    Workbook,
)
from sheetguard.core.refs import RangeKey, parse_area

FIXTURES = Path(__file__).parent / "fixtures" / "reference"


def _vtype(v: Any) -> ValueType:
    if v is None:
        return ValueType.EMPTY
    if isinstance(v, bool):
        return ValueType.BOOL
    if isinstance(v, int | float):
        return ValueType.NUMBER
    if isinstance(v, str) and v.startswith("#"):
        return ValueType.ERROR
    return ValueType.TEXT


def build(
    sheets: dict[str, dict[str, Any]],
    *,
    cached: dict[str, Any] | None = None,
    names: list[tuple[str, str | None, str]] | None = None,
    tables: list[tuple[str, str, str, list[str]]] | None = None,
    states: dict[str, str] | None = None,
    iterate: bool = False,
    hidden_rows: dict[str, set[int]] | None = None,
) -> Workbook:
    """``sheets`` maps sheet -> {"A1": value or "=formula"}; ``cached`` maps "Sheet!A1" -> value."""
    cached = cached or {}
    out: dict[str, Sheet] = {}
    for index, (name, cells) in enumerate(sheets.items()):
        model: dict[tuple[int, int], Cell] = {}
        for addr, raw in cells.items():
            area = parse_area(addr)
            assert area is not None, addr
            r, c = area[0], area[1]
            if isinstance(raw, str) and raw.startswith("="):
                val = cached.get(f"{name}!{addr}")
                model[(r, c)] = Cell(r, c, raw, val, _vtype(val))
            else:
                model[(r, c)] = Cell(r, c, None, raw, _vtype(raw))
        rows = [r for r, _ in model] or [0]
        cols = [c for _, c in model] or [0]
        used = RangeKey(name, min(rows), min(cols), max(rows), max(cols)) if model else None
        out[name] = Sheet(
            name=name,
            index=index,
            state=(states or {}).get(name, "visible"),
            cells=model,
            used=used,
            hidden_rows=frozenset((hidden_rows or {}).get(name, set())),
        )
    name_infos = tuple(DefinedNameInfo(n, scope, text, False) for n, scope, text in names or [])
    table_infos = tuple(
        TableInfo(
            name=t,
            display_name=t,
            sheet=s,
            ref=RangeKey(s, *parse_area(ref)),  # type: ignore[misc]
            header_rows=1,
            totals_rows=0,
            columns=tuple(cols),
        )
        for t, s, ref, cols in tables or []
    )
    return Workbook(
        file_name="test.xlsx",
        sha256="0" * 64,
        size_bytes=0,
        sheets=out,
        names=name_infos,
        tables=table_infos,
        iterate=iterate,
    )


def graph_of(*args: Any, **kwargs: Any) -> DepGraph:
    return DepGraph(build(*args, **kwargs))


def write_xlsx(path: Path, cells: dict[str, Any] | None = None, title: str = "Sheet1") -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title
    for addr, value in (cells or {"A1": 1}).items():
        ws[addr] = value
    wb.save(path)
    return path


def rewrite_zip(
    src: Path,
    dest: Path,
    replace: dict[str, bytes] | None = None,
    add: dict[str, bytes] | None = None,
    drop: set[str] | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
) -> Path:
    replace, add, drop = replace or {}, add or {}, drop or set()
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dest, "w", compression) as zout:
        for item in zin.infolist():
            if item.filename in drop:
                continue
            data = replace.get(item.filename, zin.read(item.filename))
            zout.writestr(item.filename, data)
        for name, data in add.items():
            zout.writestr(name, data)
    return dest
