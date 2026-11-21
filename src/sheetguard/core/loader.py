"""SafeLoader: the only way a workbook enters SheetGuard (FR-1..FR-5, S-1..S-5, S-8, S-10).

Read-only by construction: the file is read into memory exactly once with a
size cap, every check and both parse passes work on that same byte string (no
time-of-check / time-of-use gap), and nothing is ever written to disk.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import time
import warnings
from io import BytesIO
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula

from sheetguard.core.model import (
    ERROR_VALUES,
    NO_STYLE,
    ActiveContent,
    Cell,
    CellValue,
    DataValidationText,
    DefinedNameInfo,
    Sheet,
    Style,
    TableInfo,
    ValueType,
    Workbook,
)
from sheetguard.core.refs import RangeKey, parse_area
from sheetguard.errors import (
    CorruptedFileError,
    LimitExceededError,
    NotFoundError,
    SheetGuardError,
)
from sheetguard.security.archive import ArchiveReport, inspect_archive, sniff_format
from sheetguard.security.limits import DEFAULT_LIMITS, Deadline, Limits

log = logging.getLogger("sheetguard.loader")

_PARSE_FAILED = "The workbook could not be parsed (corrupted or invalid)."


def load_workbook(path: str | Path, limits: Limits = DEFAULT_LIMITS) -> Workbook:
    """Validate and load ``path``. Raises a ``SheetGuardError`` subclass on any problem."""
    started = time.perf_counter()
    deadline = Deadline(limits.call_timeout_s)
    p = Path(path)
    data = _read_capped(p, limits)
    sha = hashlib.sha256(data).hexdigest()

    sniff_format(data, p.suffix)
    archive = inspect_archive(data, limits)
    deadline.check()

    wb = _parse(data, p.name, sha, archive, limits, deadline)
    log.info(
        "loaded workbook sha256=%s bytes=%d sheets=%d cells=%d formulas=%d ms=%.0f",
        sha[:16],
        len(data),
        len(wb.sheets),
        wb.cell_count,
        wb.formula_count,
        (time.perf_counter() - started) * 1000,
    )
    return wb


def _read_capped(path: Path, limits: Limits) -> bytes:
    try:
        with path.open("rb") as fh:  # S-1: read mode only, anywhere in the code base
            data = fh.read(limits.max_file_bytes + 1)
    except FileNotFoundError as exc:
        raise NotFoundError("File not found.") from exc
    except IsADirectoryError as exc:
        raise NotFoundError("Path is a directory, not a file.") from exc
    except OSError as exc:
        raise CorruptedFileError("The file could not be read.") from exc
    if len(data) > limits.max_file_bytes:
        raise LimitExceededError(
            f"The file is larger than the {limits.max_file_bytes // (1024 * 1024)} MB limit."
        )
    if not data:
        raise CorruptedFileError("The file is empty.")
    return data


def _open(data: bytes, data_only: bool) -> Any:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # openpyxl warns about unsupported extensions
        return openpyxl.load_workbook(
            BytesIO(data),
            read_only=False,
            data_only=data_only,
            keep_vba=False,
            keep_links=False,
            rich_text=False,
        )


def _parse(
    data: bytes,
    file_name: str,
    sha: str,
    archive: ArchiveReport,
    limits: Limits,
    deadline: Deadline,
) -> Workbook:
    try:
        xl = _open(data, data_only=False)
    except SheetGuardError:
        raise
    except Exception as exc:
        raise CorruptedFileError(_PARSE_FAILED) from exc

    if len(xl.worksheets) > limits.max_sheets:
        raise LimitExceededError(f"The workbook has more than {limits.max_sheets} sheets.")

    sheets: dict[str, Sheet] = {}
    tables: list[TableInfo] = []
    names: list[DefinedNameInfo] = []
    total_cells = 0
    for index, ws in enumerate(xl.worksheets):
        sheet = _build_sheet(ws, index, limits)
        total_cells += len(sheet.cells)
        if total_cells > limits.max_cells:
            raise LimitExceededError(f"The workbook has more than {limits.max_cells:,} cells.")
        sheets[sheet.name] = sheet
        tables.extend(_tables(ws))
        names.extend(_names(getattr(ws, "defined_names", {}), ws.title))
        deadline.check()
    names.extend(_names(xl.defined_names, None))

    calc = getattr(xl, "calculation", None)
    calc_mode = str(getattr(calc, "calcMode", None) or "auto")
    iterate = bool(getattr(calc, "iterate", False))
    epoch = getattr(xl, "epoch", None)
    date_1904 = bool(epoch is not None and epoch.year == 1904)
    security = getattr(xl, "security", None)
    wb_protected = bool(
        security is not None
        and (getattr(security, "lockStructure", False) or getattr(security, "lockWindows", False))
    )
    properties = _properties(xl)
    chartsheets = len(getattr(xl, "chartsheets", []))
    del xl

    has_cached = _fill_cached_values(data, sheets, deadline)

    return Workbook(
        file_name=file_name,
        sha256=sha,
        size_bytes=len(data),
        sheets=sheets,
        names=tuple(sorted(names, key=lambda n: (n.scope_sheet or "", n.name.lower()))),
        tables=tuple(tables),
        properties=properties,
        calc_mode=calc_mode,
        iterate=iterate,
        date_1904=date_1904,
        workbook_protected=wb_protected,
        active_content=ActiveContent(
            has_vba=archive.has_vba,
            has_xlm_macros=archive.has_xlm_macros,
            has_external_links=archive.has_external_links,
            has_dde=archive.has_dde,
            has_ole_objects=archive.has_ole_objects,
            has_activex=archive.has_activex,
            has_data_connections=archive.has_data_connections,
            external_link_targets=tuple(archive.external_link_targets),
        ),
        has_cached_values=has_cached,
        chartsheet_count=chartsheets,
        extra_texts=tuple(archive.extra_texts),
    )


# ------------------------------------------------------------------- values


def normalize_value(
    value: Any, data_type: str | None, number_format: str
) -> tuple[CellValue, ValueType]:
    if value is None:
        return None, ValueType.EMPTY
    if isinstance(value, bool):
        return value, ValueType.BOOL
    if isinstance(value, int | float):
        if data_type == "e":
            return str(value), ValueType.ERROR
        return value, ValueType.NUMBER
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat(), ValueType.DATE
    if isinstance(value, dt.timedelta):
        return value.total_seconds() / 86400.0, ValueType.NUMBER
    text = str(value)
    if data_type == "e" or text.upper() in ERROR_VALUES:
        return text.upper(), ValueType.ERROR
    return text, ValueType.TEXT


def _color(color: Any) -> str | None:
    if color is None:
        return None
    ctype = getattr(color, "type", None)
    if ctype == "rgb":
        rgb = getattr(color, "rgb", None)
        return f"rgb:{rgb}" if isinstance(rgb, str) else None
    if ctype == "theme":
        return f"theme:{color.theme}:{float(color.tint or 0.0):.2f}"
    if ctype == "indexed":
        return f"indexed:{color.indexed}"
    return None


def _style(cell: Any) -> Style:
    try:
        font = cell.font
        fill = cell.fill
    except (AttributeError, IndexError):
        return NO_STYLE
    fill_color = None
    if fill is not None and getattr(fill, "fill_type", None) not in (None, "none"):
        fill_color = _color(getattr(fill, "fgColor", None))
    size = getattr(font, "sz", None) if font is not None else None
    return Style(
        font_color=_color(getattr(font, "color", None)) if font is not None else None,
        fill_color=fill_color,
        font_size=float(size) if size is not None else None,
    )


def _build_sheet(ws: Any, index: int, limits: Limits) -> Sheet:
    cells: dict[tuple[int, int], Cell] = {}
    min_r = min_c = 10**9
    max_r = max_c = 0
    for (r, c), xc in ws._cells.items():
        value = xc.value
        number_format = xc.number_format or "General"
        formula: str | None = None
        is_array = False
        array_ref: str | None = None
        if isinstance(value, ArrayFormula):
            formula, is_array, array_ref = str(value.text), True, str(value.ref)
        elif isinstance(value, DataTableFormula):
            formula = "=TABLE()"
        elif xc.data_type == "f" and isinstance(value, str):
            formula = value
        comment = xc.comment.text if xc.comment is not None else None
        hyperlink = None
        if xc.hyperlink is not None:
            hyperlink = xc.hyperlink.target or (
                f"#{xc.hyperlink.location}" if xc.hyperlink.location else None
            )
        if formula is None and value is None and comment is None and hyperlink is None:
            continue
        if formula is not None:
            if not formula.startswith("="):
                formula = "=" + formula
            if len(formula) > limits.max_formula_length:
                raise LimitExceededError(
                    f"A formula is longer than {limits.max_formula_length:,} characters."
                )
            norm, vtype = None, ValueType.EMPTY
        else:
            norm, vtype = normalize_value(value, xc.data_type, number_format)
        style = _style(xc) if (formula is not None or vtype is ValueType.TEXT) else NO_STYLE
        cells[(r, c)] = Cell(
            row=r,
            col=c,
            formula=formula,
            value=norm,
            vtype=vtype,
            number_format=number_format,
            style=style,
            is_array=is_array,
            array_ref=array_ref,
            comment=comment,
            hyperlink=hyperlink,
        )
        if formula is not None or vtype is not ValueType.EMPTY:
            min_r, min_c = min(min_r, r), min(min_c, c)
            max_r, max_c = max(max_r, r), max(max_c, c)

    used = RangeKey(ws.title, min_r, min_c, max_r, max_c) if max_r else None
    hidden_rows = frozenset(
        r
        for r, d in ws.row_dimensions.items()
        if d.hidden or (d.customHeight and d.ht is not None and d.ht == 0)
    )
    hidden_cols: set[int] = set()
    for d in ws.column_dimensions.values():
        if d.hidden and d.min and d.max:
            hidden_cols.update(range(int(d.min), min(int(d.max), 16_384) + 1))

    merged = tuple(
        RangeKey(ws.title, m.min_row, m.min_col, m.max_row, m.max_col)
        for m in ws.merged_cells.ranges
    )
    validations = tuple(
        DataValidationText(
            sqref=str(dv.sqref),
            prompt_title=dv.promptTitle,
            prompt=dv.prompt,
            error_title=dv.errorTitle,
            error=dv.error,
        )
        for dv in ws.data_validations.dataValidation
        if dv.prompt or dv.error or dv.promptTitle or dv.errorTitle
    )
    return Sheet(
        name=ws.title,
        index=index,
        state=str(ws.sheet_state or "visible"),
        cells=cells,
        hidden_rows=hidden_rows,
        hidden_cols=frozenset(hidden_cols),
        merged=merged,
        protected=bool(ws.protection.sheet),
        data_validations=validations,
        headers_footers=_headers_footers(ws),
        used=used,
    )


def _headers_footers(ws: Any) -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    for attr in (
        "oddHeader",
        "oddFooter",
        "evenHeader",
        "evenFooter",
        "firstHeader",
        "firstFooter",
    ):
        hf = getattr(ws, attr, None)
        if hf is None:
            continue
        for part in ("left", "center", "right"):
            text = getattr(getattr(hf, part, None), "text", None)
            if text:
                out.append((f"{attr}.{part}", str(text)))
    return tuple(out)


def _tables(ws: Any) -> list[TableInfo]:
    out: list[TableInfo] = []
    for t in ws.tables.values():
        area = parse_area(str(t.ref))
        if area is None:
            continue
        out.append(
            TableInfo(
                name=str(t.name),
                display_name=str(t.displayName or t.name),
                sheet=ws.title,
                ref=RangeKey(ws.title, *area),
                header_rows=1 if t.headerRowCount is None else int(t.headerRowCount),
                totals_rows=int(t.totalsRowCount or 0),
                columns=tuple(str(c.name) for c in t.tableColumns),
            )
        )
    return out


def _names(container: Any, scope: str | None) -> list[DefinedNameInfo]:
    out: list[DefinedNameInfo] = []
    for name, dn in getattr(container, "items", lambda: [])():
        text = str(dn.attr_text or "")
        out.append(
            DefinedNameInfo(name=str(name), scope_sheet=scope, text=text, hidden=bool(dn.hidden))
        )
    return out


def _properties(xl: Any) -> tuple[tuple[str, str], ...]:
    props: list[tuple[str, str]] = []
    core = xl.properties
    for attr in (
        "title",
        "subject",
        "creator",
        "keywords",
        "description",
        "category",
        "lastModifiedBy",
        "identifier",
        "language",
        "version",
        "contentStatus",
    ):
        value = getattr(core, attr, None)
        if value:
            props.append((f"core.{attr}", str(value)))
    custom = getattr(xl, "custom_doc_props", None)
    for prop in getattr(custom, "props", []) or []:
        if getattr(prop, "value", None) is not None:
            props.append((f"custom.{prop.name}", str(prop.value)))
    return tuple(props)


def _fill_cached_values(data: bytes, sheets: dict[str, Sheet], deadline: Deadline) -> bool:
    """Second pass (FR-3): attach the last cached value to every formula cell."""
    try:
        xl = _open(data, data_only=True)
    except Exception as exc:
        raise CorruptedFileError(_PARSE_FAILED) from exc
    any_formula = False
    any_cached = False
    for ws in xl.worksheets:
        sheet = sheets.get(ws.title)
        if sheet is None:
            continue
        for pos, cell in list(sheet.cells.items()):
            if cell.formula is None:
                continue
            any_formula = True
            xc = ws._cells.get(pos)
            if xc is None or xc.value is None:
                continue
            value, vtype = normalize_value(xc.value, xc.data_type, cell.number_format)
            any_cached = True
            sheet.cells[pos] = Cell(
                row=cell.row,
                col=cell.col,
                formula=cell.formula,
                value=value,
                vtype=vtype,
                number_format=cell.number_format,
                style=cell.style,
                is_array=cell.is_array,
                array_ref=cell.array_ref,
                comment=cell.comment,
                hyperlink=cell.hyperlink,
            )
        deadline.check()
    del xl
    return any_cached or not any_formula
