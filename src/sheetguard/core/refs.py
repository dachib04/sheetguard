"""Cell addresses, formula tokenising and reference parsing (FR-4, FR-6, FR-10).

Formulas are tokenised with openpyxl's tokenizer (a faithful port of the Excel
grammar) and each RANGE operand is classified into one of the reference kinds
below. Nothing here evaluates anything.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import NamedTuple

from openpyxl.formula.tokenizer import Token, Tokenizer, TokenizerError
from openpyxl.utils.cell import column_index_from_string, get_column_letter

MAX_ROW = 1_048_576
MAX_COL = 16_384

# --------------------------------------------------------------------------- keys


class CellKey(NamedTuple):
    sheet: str
    row: int
    col: int


class RangeKey(NamedTuple):
    sheet: str
    r1: int
    c1: int
    r2: int
    c2: int

    @property
    def size(self) -> int:
        return (self.r2 - self.r1 + 1) * (self.c2 - self.c1 + 1)

    def contains(self, row: int, col: int) -> bool:
        return self.r1 <= row <= self.r2 and self.c1 <= col <= self.c2


# ------------------------------------------------------------------- formatting

_PLAIN_SHEET = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_LOOKS_LIKE_REF = re.compile(r"^(?:[A-Za-z]{1,3}\d+|R\d*C\d*|[RC]\d*)$", re.IGNORECASE)


def quote_sheet(name: str) -> str:
    """Quote a sheet name the way Excel does when it is needed."""
    if _PLAIN_SHEET.match(name) and not _LOOKS_LIKE_REF.match(name):
        return name
    return "'" + name.replace("'", "''") + "'"


@lru_cache(maxsize=MAX_COL)
def col_letter(col: int) -> str:
    return str(get_column_letter(col))


def a1(row: int, col: int) -> str:
    return f"{col_letter(col)}{row}"


def format_cell(key: CellKey) -> str:
    return f"{quote_sheet(key.sheet)}!{a1(key.row, key.col)}"


def format_range(key: RangeKey) -> str:
    sheet = quote_sheet(key.sheet)
    if key.r1 == 1 and key.r2 == MAX_ROW:
        return f"{sheet}!{col_letter(key.c1)}:{col_letter(key.c2)}"
    if key.c1 == 1 and key.c2 == MAX_COL:
        return f"{sheet}!{key.r1}:{key.r2}"
    if key.r1 == key.r2 and key.c1 == key.c2:
        return f"{sheet}!{a1(key.r1, key.c1)}"
    return f"{sheet}!{a1(key.r1, key.c1)}:{a1(key.r2, key.c2)}"


# ---------------------------------------------------------------- address parse

_CELL = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d{1,7})$")
_COL = re.compile(r"^\$?([A-Za-z]{1,3})$")
_ROW = re.compile(r"^\$?(\d{1,7})$")


def _parse_cell(text: str) -> tuple[int, int] | None:
    m = _CELL.match(text)
    if not m:
        return None
    col = column_index_from_string(m.group(1).upper())
    row = int(m.group(2))
    if not (1 <= row <= MAX_ROW and 1 <= col <= MAX_COL):
        return None
    return row, col


def parse_area(text: str) -> tuple[int, int, int, int] | None:
    """Parse ``A1``, ``A1:B2``, ``A:C`` or ``3:5`` into (r1, c1, r2, c2)."""
    if ":" not in text:
        cell = _parse_cell(text)
        return (cell[0], cell[1], cell[0], cell[1]) if cell else None
    left, _, right = text.partition(":")
    lc, rc = _parse_cell(left), _parse_cell(right)
    if lc and rc:
        return (min(lc[0], rc[0]), min(lc[1], rc[1]), max(lc[0], rc[0]), max(lc[1], rc[1]))
    lm, rm = _COL.match(left), _COL.match(right)
    if lm and rm:
        c1 = column_index_from_string(lm.group(1).upper())
        c2 = column_index_from_string(rm.group(1).upper())
        if c1 > MAX_COL or c2 > MAX_COL:
            return None
        return (1, min(c1, c2), MAX_ROW, max(c1, c2))
    lr, rr = _ROW.match(left), _ROW.match(right)
    if lr and rr:
        r1, r2 = int(lr.group(1)), int(rr.group(1))
        if not (1 <= r1 <= MAX_ROW and 1 <= r2 <= MAX_ROW):
            return None
        return (min(r1, r2), 1, max(r1, r2), MAX_COL)
    return None


def split_sheet_prefix(text: str) -> tuple[str | None, str]:
    """Split ``'My Sheet'!A1`` into (``My Sheet``, ``A1``). Handles '' escapes."""
    if text.startswith("'"):
        i = 1
        out: list[str] = []
        while i < len(text):
            ch = text[i]
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                if i + 1 < len(text) and text[i + 1] == "!":
                    return "".join(out), text[i + 2 :]
                return None, text
            out.append(ch)
            i += 1
        return None, text
    # Unquoted prefix: '!' cannot appear in a sheet name, so split at the first one.
    if "!" in text:
        prefix, _, rest = text.partition("!")
        return prefix, rest
    return None, text


def parse_cell_address(text: str, default_sheet: str | None = None) -> CellKey:
    """Parse a user-supplied ``Sheet!A1`` (sheet may be quoted)."""
    sheet, addr = split_sheet_prefix(text.strip())
    sheet = sheet if sheet is not None else default_sheet
    cell = _parse_cell(addr)
    if sheet is None or cell is None:
        raise ValueError("Expected a cell address like Sheet1!B7")
    return CellKey(sheet, cell[0], cell[1])


def parse_range_address(text: str, default_sheet: str | None = None) -> RangeKey:
    sheet, addr = split_sheet_prefix(text.strip())
    sheet = sheet if sheet is not None else default_sheet
    area = parse_area(addr)
    if sheet is None or area is None:
        raise ValueError("Expected a range like Sheet1!A1:C10")
    return RangeKey(sheet, *area)


# ------------------------------------------------------------- reference kinds


@dataclass(frozen=True, slots=True)
class AreaRef:
    """A cell or rectangular area on one sheet, or on a 3D span of sheets."""

    sheet: str | None  # None = same sheet as the formula
    r1: int
    c1: int
    r2: int
    c2: int
    sheet_end: str | None = None  # set for 3D refs Sheet1:Sheet3!A1

    @property
    def is_cell(self) -> bool:
        return self.r1 == self.r2 and self.c1 == self.c2


@dataclass(frozen=True, slots=True)
class NameRef:
    name: str
    sheet: str | None = None  # explicit Sheet1!Name scope


@dataclass(frozen=True, slots=True)
class StructRef:
    """Excel table reference such as ``Table1[Revenue]`` or ``[@Qty]``."""

    table: str | None  # None = the table that contains the formula cell
    specials: frozenset[str]  # subset of {#all,#data,#headers,#totals,#this row}
    col_start: str | None
    col_end: str | None


@dataclass(frozen=True, slots=True)
class ExternalRef:
    raw: str


@dataclass(frozen=True, slots=True)
class UnparsedRef:
    raw: str


Ref = AreaRef | NameRef | StructRef | ExternalRef | UnparsedRef

_NAME = re.compile(r"^[A-Za-z_\\][A-Za-z0-9_.\\?]*$")
_EXTERNAL_PREFIX = re.compile(r"^\[[^\]]+\]|\[[^\]]+\]")


def parse_operand(text: str) -> Ref:
    """Classify one RANGE operand token."""
    raw = text
    if text.startswith("[") and "!" not in text and text.endswith("]"):
        # [Col] style structured ref without a table name
        return _parse_struct(None, text) or UnparsedRef(raw)

    sheet, rest = split_sheet_prefix(text)
    if sheet is not None and "[" in sheet:
        if not sheet.startswith("[") and not text.startswith("'") and text.endswith("]"):
            # Table1[a!b]: a '!' inside a structured-ref column name, not a sheet.
            table, _, bracket = text.partition("[")
            return _parse_struct(table or None, "[" + bracket) or UnparsedRef(raw)
        return ExternalRef(raw)
    if sheet is None and _EXTERNAL_PREFIX.match(text):
        return ExternalRef(raw)

    sheet_end: str | None = None
    if sheet is not None and ":" in sheet:
        sheet, _, sheet_end = sheet.partition(":")

    # 'Sheet1!A1:Sheet1!B5' style: drop the repeated prefix on the right side
    if ":" in rest and "!" in rest:
        left, _, right = rest.partition(":")
        _, right_addr = split_sheet_prefix(right)
        rest = f"{left}:{right_addr}"

    area = parse_area(rest)
    if area is not None:
        return AreaRef(sheet, *area, sheet_end=sheet_end)

    if "[" in rest:
        table, _, bracket = rest.partition("[")
        return _parse_struct(table or None, "[" + bracket) or UnparsedRef(raw)

    if _NAME.match(rest):
        return NameRef(rest, sheet)
    return UnparsedRef(raw)


_SPECIALS = {"#all", "#data", "#headers", "#totals", "#this row"}


def _unescape_col(name: str) -> str:
    return re.sub(r"'(.)", r"\1", name).strip()


def _parse_struct(table: str | None, bracket: str) -> StructRef | None:
    if not (bracket.startswith("[") and bracket.endswith("]")):
        return None
    inner = bracket[1:-1].strip()
    specials: set[str] = set()
    if inner.startswith("@"):
        specials.add("#this row")
        inner = inner[1:].strip()
    # Items are either [x],[y] or a bare single item.
    items = re.findall(r"\[((?:[^\]']|'.)*)\]", inner) if inner.startswith("[") else []
    if not items and inner:
        items = [inner]
    cols: list[str] = []
    for item in items:
        low = item.strip().lower()
        if low in _SPECIALS:
            specials.add(low)
        else:
            cols.append(_unescape_col(item))
    col_start: str | None
    col_end: str | None
    if ":" in inner and len(cols) >= 2:
        col_start, col_end = cols[0], cols[-1]
    elif cols:
        col_start = col_end = cols[0]
    else:
        col_start = col_end = None
    return StructRef(table, frozenset(specials), col_start, col_end)


# ----------------------------------------------------------- formula analysis

_DYNAMIC_FUNCS = frozenset({"INDIRECT", "OFFSET"})
_VOLATILE_FUNCS = frozenset({"NOW", "TODAY", "RAND", "RANDBETWEEN", "INDIRECT", "OFFSET"})


@dataclass(slots=True)
class FormulaInfo:
    """Static facts extracted from one formula string."""

    ok: bool
    refs: list[Ref] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)  # upper-case, prefixes stripped
    numbers: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)  # string literals, unquoted
    errors: list[str] = field(default_factory=list)  # literal error tokens, e.g. #REF!

    @property
    def is_dynamic(self) -> bool:
        return any(f in _DYNAMIC_FUNCS for f in self.functions)

    @property
    def volatile_functions(self) -> list[str]:
        return sorted({f for f in self.functions if f in _VOLATILE_FUNCS})


def _strip_fn_prefix(name: str) -> str:
    for prefix in ("_XLFN._XLWS.", "_XLFN.", "_XLWS.", "_XLL."):
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


@lru_cache(maxsize=1 << 17)
def tokenize(formula: str) -> tuple[Token, ...] | None:
    """Tokens of a formula (cached: every check reads the same formulas; treat as read-only)."""
    try:
        return tuple(Tokenizer(formula).items)
    except (TokenizerError, IndexError, ValueError):
        return None


@lru_cache(maxsize=65_536)
def analyze_formula(formula: str) -> FormulaInfo:
    tokens = tokenize(formula)
    if tokens is None:
        return FormulaInfo(ok=False)
    info = FormulaInfo(ok=True)
    depth = 0
    for tok in tokens:
        if tok.subtype == Token.OPEN:
            depth += 1
        elif tok.subtype == Token.CLOSE:
            depth -= 1
            if depth < 0:
                info.ok = False
        if tok.type == Token.OPERAND:
            if tok.subtype == Token.RANGE:
                info.refs.append(parse_operand(tok.value))
            elif tok.subtype == Token.NUMBER:
                info.numbers.append(tok.value)
            elif tok.subtype == Token.TEXT:
                info.texts.append(tok.value[1:-1].replace('""', '"'))
            elif tok.subtype == Token.ERROR:
                info.errors.append(tok.value.upper())
        elif tok.type == Token.FUNC and tok.subtype == Token.OPEN:
            info.functions.append(_strip_fn_prefix(tok.value[:-1].upper()))
    if depth != 0:
        info.ok = False  # unbalanced parentheses: the tokenizer is lenient, we are not
    return info


# --------------------------------------------------------------------- R1C1


def _cell_to_r1c1(text: str, row: int, col: int) -> str:
    m = re.match(r"^(\$?)([A-Za-z]{1,3})(\$?)(\d+)$", text)
    if not m:
        return text
    c = column_index_from_string(m.group(2).upper())
    r = int(m.group(4))
    rpart = f"R{r}" if m.group(3) else ("R" if r == row else f"R[{r - row}]")
    cpart = f"C{c}" if m.group(1) else ("C" if c == col else f"C[{c - col}]")
    return rpart + cpart


def _addr_to_r1c1(addr: str, row: int, col: int) -> str:
    parts = addr.split(":")
    out: list[str] = []
    for part in parts:
        if _CELL.match(part):
            out.append(_cell_to_r1c1(part, row, col))
        elif (m := re.match(r"^(\$?)([A-Za-z]{1,3})$", part)) is not None:
            c = column_index_from_string(m.group(2).upper())
            out.append(f"C{c}" if m.group(1) else f"C[{c - col}]")
        elif (m := re.match(r"^(\$?)(\d+)$", part)) is not None:
            r = int(m.group(2))
            out.append(f"R{r}" if m.group(1) else f"R[{r - row}]")
        else:
            out.append(part)
    return ":".join(out)


@lru_cache(maxsize=1 << 18)
def to_r1c1(formula: str, row: int, col: int) -> str:
    """Relative R1C1 rendering used to compare formulas across cells (C02/C06).

    Whitespace is dropped and function names are upper-cased, so two formulas
    that Excel would treat as identical when filled across compare equal.
    """
    tokens = tokenize(formula)
    if tokens is None:
        return formula
    out: list[str] = []
    for tok in tokens:
        if tok.type == Token.WSPACE:
            continue
        if tok.type == Token.OPERAND and tok.subtype == Token.RANGE:
            sheet, addr = split_sheet_prefix(tok.value)
            converted = _addr_to_r1c1(addr, row, col)
            out.append(f"{quote_sheet(sheet)}!{converted}" if sheet else converted)
        elif tok.type == Token.FUNC and tok.subtype == Token.OPEN:
            out.append(tok.value.upper())
        else:
            out.append(tok.value)
    return "=" + "".join(out)


# ------------------------------------------------------------ context walking


@dataclass(frozen=True, slots=True)
class TokenContext:
    """A token plus the stack of enclosing function calls: ((NAME, arg_index), ...)."""

    token: Token
    index: int
    calls: tuple[tuple[str, int], ...]

    @property
    def function(self) -> str | None:
        return self.calls[-1][0] if self.calls else None

    @property
    def arg(self) -> int | None:
        return self.calls[-1][1] if self.calls else None

    def inside(self, names: frozenset[str] | set[str]) -> bool:
        return any(name in names for name, _ in self.calls)


@lru_cache(maxsize=1 << 17)
def walk(formula: str) -> tuple[TokenContext, ...] | None:
    tokens = tokenize(formula)
    if tokens is None:
        return None
    out: list[TokenContext] = []
    stack: list[list[object]] = []  # [name | None (plain paren), arg_index]
    for i, tok in enumerate(tokens):
        calls = tuple((str(n), int(a)) for n, a in stack if n is not None)  # type: ignore[call-overload]
        out.append(TokenContext(tok, i, calls))
        if tok.type == Token.FUNC and tok.subtype == Token.OPEN:
            stack.append([_strip_fn_prefix(tok.value[:-1].upper()), 0])
        elif tok.type == Token.PAREN and tok.subtype == Token.OPEN:
            stack.append([None, 0])
        elif tok.subtype == Token.CLOSE and stack:
            stack.pop()
        elif tok.type == Token.SEP and tok.subtype == Token.ARG and stack:
            stack[-1][1] = int(stack[-1][1]) + 1  # type: ignore[call-overload]
    return tuple(out)


def render(tokens: Sequence[Token], replace: dict[int, str] | None = None) -> str:
    """Re-assemble a formula from tokens, optionally replacing some by index."""
    replace = replace or {}
    return "=" + "".join(replace.get(i, t.value) for i, t in enumerate(tokens))
