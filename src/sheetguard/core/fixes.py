"""FixSuggest (FR-30..FR-32): proposed formulas as *text only*. Nothing here,
or anywhere in SheetGuard, writes to a workbook."""

from __future__ import annotations

import re

from openpyxl.formula.translate import Translator, TranslatorError

from sheetguard.core.refs import a1, render, tokenize


def from_neighbour(formula: str, origin: str, dest: str) -> str | None:
    """FR-30: the neighbour's formula rewritten for the target position (C02, C06)."""
    try:
        return str(Translator(formula, origin=origin).translate_formula(dest))
    except (TranslatorError, ValueError, KeyError):
        return None


def input_name_for(literal: str) -> str:
    """A readable, valid defined-name suggestion for a literal, e.g. 0.05 -> Input_0_05."""
    clean = re.sub(r"[^0-9A-Za-z]+", "_", literal).strip("_") or "value"
    return f"Input_{clean}"


def with_named_input(formula: str, token_index: int, name: str) -> str | None:
    """FR-31: the formula with one numeric literal replaced by a named input."""
    tokens = tokenize(formula)
    if tokens is None or not 0 <= token_index < len(tokens):
        return None
    return render(tokens, {token_index: name})


def with_extended_range(
    formula: str, token_index: int, new_area: tuple[int, int, int, int], keep_prefix: str = ""
) -> str | None:
    """C07: the formula with one range token widened to ``new_area``."""
    tokens = tokenize(formula)
    if tokens is None or not 0 <= token_index < len(tokens):
        return None
    r1, c1, r2, c2 = new_area
    return render(tokens, {token_index: f"{keep_prefix}{a1(r1, c1)}:{a1(r2, c2)}"})
