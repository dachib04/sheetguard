"""Positive and negative cases for C01, C03, C06-C14 (§5.3)."""

from __future__ import annotations

from sheetguard.checks import run_checks
from sheetguard.checks.base import WORKBOOK_LABEL
from sheetguard.checks.c14_text_numbers import parse_number_text
from sheetguard.core.graph import DepGraph
from sheetguard.core.model import ActiveContent, Cell, Sheet, Style, ValueType, Workbook
from sheetguard.report.schema import Confidence, Severity
from tests.helpers import build


def audit(check, sheets, **kw):
    wb = build(sheets, **kw)
    return run_checks(wb, DepGraph(wb), [check])


def audit_wb(check, wb: Workbook):
    return run_checks(wb, DepGraph(wb), [check])


# ---------------------------------------------------------------------------- C01


def test_c01_flags_magic_number_with_named_input_fix():
    (i,) = audit("C01", {"S": {"A1": 100, "B1": "=A1*1.07"}})
    assert i.cell == "S!B1" and i.severity is Severity.WARNING
    assert "1.07" in i.description
    assert i.suggested_fix is not None and i.suggested_fix.text == "=A1*Input_1_07"


def test_c01_percent_literal():
    (i,) = audit("C01", {"S": {"A1": 100, "B1": "=A1*(1+5%)"}})
    assert "5%" in i.description
    assert i.suggested_fix.text == "=A1*(1+Input_5)"


def test_c01_allowlist_and_structural_args():
    cells = {
        "A1": 5,
        "B1": "=A1*12",
        "B2": "=A1/1000",
        "B3": "=ROUND(A1,3)",
        "B4": "=VLOOKUP(A1,D1:F9,3,FALSE)",
        "B5": "=A1*365",
        "B6": "=DATE(2026,6,30)+A1",
        "B7": "=INDEX(D1:F9,4,2)",
        "B8": "=LEFT(A1,4)",
        "B9": "=A1^2-1",
    }
    assert audit("C01", {"S": cells}) == []


def test_c01_ignores_inputs_written_as_formulas_and_named_cells():
    cells = {"A1": "=250000", "A2": "=12*1500", "A3": "=A1*0.05"}
    assert audit("C01", {"S": cells}, names=[("Rate", None, "S!$A$3")]) == []


def test_c01_groups_repeated_pattern():
    cells = {f"{c}1": i for i, c in enumerate("ABCDE", 1)}
    cells |= {f"{c}2": f"={c}1*1.5" for c in "ABCDE"}
    (i,) = audit("C01", {"S": cells})
    assert i.affected_count == 5 and "4 more" in i.description


def test_c01_custom_allowlist():
    from sheetguard.checks import CheckConfig

    wb = build({"S": {"A1": 1, "B1": "=A1*1.07"}})
    cfg = CheckConfig(allowed_constants=frozenset({"1.07"}))
    assert run_checks(wb, DepGraph(wb), ["C01"], config=cfg) == []


# ---------------------------------------------------------------------------- C03


def test_c03_direct_reference_to_empty():
    (i,) = audit("C03", {"S": {"A1": 2, "B1": "=A1*Z9"}})
    assert i.cell == "S!B1" and "S!Z9" in i.description
    assert i.evidence[-1].note == "empty"


def test_c03_cross_sheet_empty():
    (i,) = audit("C03", {"S": {"B1": "=T!C3+1"}, "T": {"A1": 1}})
    assert "T!C3" in i.description


def test_c03_tolerant_contexts_and_non_empty():
    cells = {
        "A1": 1,
        "A2": "",
        "B1": "=SUM(A1,Z9)",
        "B2": "=IF(ISBLANK(Z9),0,1)",
        "B3": '=A2&"x"',
        "B4": "=COUNTA(A1:A9)",
        "B5": "=A1*2",
        "B6": "=Nope!A1",
    }
    assert audit("C03", {"S": cells}) == []


# ---------------------------------------------------------------------------- C06


def _formula_row(const_at: str | None = "E", value=999):
    cells = {f"{c}1": i for i, c in enumerate("BCDEFG", 1)}
    cells |= {f"{c}2": f"={c}1*2" for c in "BCDEFG"}
    if const_at:
        cells[f"{const_at}2"] = value
    return cells


def test_c06_interior_constant_high_confidence_with_fix():
    (i,) = audit("C06", {"S": _formula_row()})
    assert i.cell == "S!E2" and i.severity is Severity.CRITICAL
    assert i.confidence is Confidence.HIGH
    assert i.suggested_fix.text == "=E1*2"


def test_c06_edge_constant_low_confidence():
    (i,) = audit("C06", {"S": _formula_row("G")})
    assert i.confidence is Confidence.LOW


def test_c06_clean_row_and_seed_value():
    assert audit("C06", {"S": _formula_row(None)}) == []
    seed = {"B2": 100} | {f"{c}2": f"={p}2*1.05" for p, c in zip("BCDEF", "CDEFG", strict=True)}
    assert audit("C06", {"S": seed}) == []


def test_c06_years_and_data_blocks_ignored():
    years = {"B1": 2026} | {f"{c}1": f"={p}1+1" for p, c in zip("BCDEF", "CDEFG", strict=True)}
    assert audit("C06", {"S": years}) == []
    data = {f"{c}1": i for i, c in enumerate("BCDEFG", 1)} | {"H1": "=B1*2", "I1": "=C1*2"}
    assert audit("C06", {"S": data}) == []


def test_c06_needs_three_pattern_neighbours():
    cells = {"B1": 1, "C1": 2, "D1": 3, "B2": "=B1*2", "C2": "=C1*2", "D2": 5}
    assert audit("C06", {"S": cells}) == []


def test_c06_vertical():
    cells = {f"A{r}": r for r in range(1, 8)} | {f"B{r}": f"=A{r}*3" for r in range(1, 8)}
    cells["B4"] = 12
    (i,) = audit("C06", {"S": cells})
    assert i.cell == "S!B4" and "column" in i.description


# ---------------------------------------------------------------------------- C07


def test_c07_one_short_with_fix():
    cells = {f"A{r}": r * 10 for r in range(1, 7)} | {"A8": "=SUM(A1:A5)"}
    (i,) = audit("C07", {"S": cells})
    assert i.cell == "S!A8" and "S!A6" in i.description
    assert i.suggested_fix.text == "=SUM(A1:A6)"
    assert i.confidence is Confidence.HIGH


def test_c07_horizontal_and_cross_sheet_prefix_kept():
    cells = {f"{c}1": i for i, c in enumerate("ABCDE", 1)}
    (i,) = audit("C07", {"D": cells, "S": {"A1": "=SUM(D!A1:D1)"}})
    assert i.suggested_fix.text == "=SUM(D!A1:E1)"


def test_c07_negatives():
    complete = {f"A{r}": r for r in range(1, 7)} | {"A8": "=SUM(A1:A6)"}
    plus_cell = {f"A{r}": r for r in range(1, 7)} | {"A8": "=SUM(A1:A5)+A6"}
    partial = {f"A{r}": r for r in range(1, 10)} | {"B1": "=SUM(A1:A3)"}  # block continues
    header = {"A1": "Sales"} | {f"A{r}": r for r in range(2, 7)} | {"A8": "=SUM(A2:A6)"}
    years = {"A1": 2025, "A2": 2026, "A3": 500, "A4": 600, "A5": 700, "B1": "=SUM(A3:A5)"}
    for cells in (complete, plus_cell, partial, header, years):
        assert audit("C07", {"S": cells}) == [], cells


def test_c07_start_side_medium_confidence():
    cells = {"A1": "x", **{f"A{r}": r * 10 for r in range(2, 8)}, "A9": "=SUM(A3:A7)"}
    (i,) = audit("C07", {"S": cells})
    assert "S!A2" in i.description and i.confidence is Confidence.MEDIUM


# ---------------------------------------------------------------------------- C08


def test_c08_external_formula_grouped():
    cells = {"A1": "=[1]Rates!B2", "A2": "=[1]Rates!B3", "B1": "='C:\\x\\[Ext.xlsx]S'!A1*2"}
    issues = audit("C08", {"S": cells})
    assert [(i.cell, i.affected_count) for i in issues] == [("S!A1", 2), ("S!B1", 1)]


def test_c08_external_name_and_unused_link_parts():
    wb = build({"S": {"A1": "=Fx*2"}}, names=[("Fx", None, "[2]Rates!$B$2")])
    (i,) = audit_wb("C08", wb)
    assert i.cell == "name Fx"
    wb2 = build({"S": {"A1": 1}})
    wb2.active_content = ActiveContent(
        has_external_links=True, has_dde=True, external_link_targets=("file:///x.xlsx",)
    )
    (j,) = audit_wb("C08", wb2)
    assert j.cell == WORKBOOK_LABEL and "DDE" in j.description
    assert j.evidence[0].value.text == "file:///x.xlsx"


def test_c08_clean():
    assert audit("C08", {"S": {"A1": 1, "B1": "=A1"}}) == []


# ---------------------------------------------------------------------------- C09


def test_c09_volatile_and_dynamic():
    issues = audit(
        "C09",
        {
            "S": {
                "A1": "=TODAY()",
                "A2": "=NOW()-A1",
                "B1": "INDIRECT",
                "B2": '=INDIRECT("B1")',
                "B3": "=RAND()*RANDBETWEEN(1,6)",
            }
        },
    )
    by_cell = {i.cell: i for i in issues}
    assert by_cell["S!A1"].severity is Severity.INFO
    assert "partial" in by_cell["S!B2"].description
    assert "RAND, RANDBETWEEN" in by_cell["S!B3"].description


def test_c09_clean():
    assert audit("C09", {"S": {"A1": 1, "B1": "=A1+1"}}) == []


# ---------------------------------------------------------------------------- C10


def _with_format(wb: Workbook, sheet: str, pos: tuple[int, int], fmt: str) -> Workbook:
    c = wb.sheets[sheet].cells[pos]
    wb.sheets[sheet].cells[pos] = Cell(c.row, c.col, c.formula, c.value, c.vtype, fmt, Style())
    return wb


def test_c10_percent_typed_as_whole_number():
    wb = _with_format(build({"S": {"A1": 25, "A2": 0.25}}), "S", (1, 1), "0%")
    wb = _with_format(wb, "S", (2, 1), "0%")
    (i,) = audit_wb("C10", wb)
    assert i.cell == "S!A1" and i.confidence is Confidence.LOW


def test_c10_mixed_magnitudes_and_scale_formats():
    (i,) = audit("C10", {"S": {"A1": 1_200_000, "A2": 950_000, "A3": 1.3, "A4": "=SUM(A1:A3)"}})
    assert "factor" in i.description
    wb = build({"S": {"A1": 1200, "A2": 950, "A3": 870, "A4": "=SUM(A1:A3)"}})
    wb = _with_format(wb, "S", (1, 1), "#,##0,")
    (j,) = audit_wb("C10", wb)
    assert "different scales" in j.description


def test_c10_clean():
    assert audit("C10", {"S": {"A1": 800, "A2": 15000, "A3": 2500, "A4": "=SUM(A1:A3)"}}) == []


# ---------------------------------------------------------------------------- C11


def test_c11_hidden_sheet_row_and_unused_hidden_cells():
    wb = build(
        {
            "Main": {"A1": "=Calc!A1*2", "A5": 7, "B1": "=A5+1", "A9": 3},
            "Calc": {"A1": 10, "A2": 99},
        },
        states={"Calc": "veryHidden"},
        hidden_rows={"Main": {5, 9}},
    )
    issues = audit_wb("C11", wb)
    labels = sorted(i.description for i in issues)
    assert len(issues) == 2
    assert any("very hidden sheet" in d for d in labels)
    assert any("hidden row 5" in d for d in labels)  # row 9 feeds nothing: not reported


def test_c11_clean():
    assert audit("C11", {"S": {"A1": 1, "B1": "=A1"}}) == []


# ---------------------------------------------------------------------------- C12


def test_c12_unused_inputs_grouped_and_labels():
    cells = {"A1": "Rate", "B1": 0.05, "A2": "Unused", "B2": 7, "B3": 9.5, "C1": "=B1*2"}
    (i,) = audit("C12", {"S": cells})
    assert i.cell == "S!B2:B3" and i.affected_count == 2


def test_c12_ignores_periods_text_and_formula_free_workbooks():
    cells = {f"A{r}": r - 1 for r in range(2, 8)} | {"B2": "=A2*2", "C1": "label"}
    assert audit("C12", {"S": cells}) == []
    assert audit("C12", {"S": {"A1": 5}}) == []


def test_c12_unreferenced_data_sheet_once():
    issues = audit("C12", {"S": {"A1": 1, "B1": "=A1"}, "Old": {"A1": 3, "A2": 40, "B1": 500}})
    (i,) = issues
    assert i.cell == "Old!A1 (sheet)" and i.affected_count == 3
    used = audit("C12", {"S": {"B1": "=Data!A1"}, "Data": {"A1": 3, "A2": 4}})
    assert used == []


# ---------------------------------------------------------------------------- C13


def test_c13_no_cache_manual_and_partial():
    wb = build({"S": {"A1": 1, "B1": "=A1"}})
    wb.has_cached_values = False
    wb.calc_mode = "manual"
    issues = audit_wb("C13", wb)
    assert {i.severity for i in issues} == {Severity.WARNING}
    assert len(issues) == 2
    partial = build({"S": {"A1": 1, "B1": "=A1", "B2": "=A1"}}, cached={"S!B1": 1})
    (i,) = audit_wb("C13", partial)
    assert i.severity is Severity.INFO and i.affected_count == 1


def test_c13_clean():
    assert audit("C13", {"S": {"A1": 1, "B1": "=A1"}}, cached={"S!B1": 1}) == []


# ---------------------------------------------------------------------------- C14


def test_c14_text_numbers_used_by_formulas():
    cells = {"A1": "Amount", "A2": "1,250.50", "A3": "$2,000", "A4": 7, "B1": "=SUM(A2:A4)"}
    issues = audit("C14", {"S": cells})
    assert [(i.cell, i.affected_count) for i in issues] == [("S!A2:A3", 2)]
    assert issues[0].suggested_fix.text == "1250.5"


def test_c14_negatives():
    ids = {"A1": "Customer ID", "A2": "123", "A3": "456", "B1": "=A2&A3"}
    codes = {"A1": "Amount", "A2": "00123", "B1": '=A2&""'}
    unused = {"A1": "Amount", "A2": "1,250"}
    for cells in (ids, codes, unused):
        assert audit("C14", {"S": cells}) == []


def test_parse_number_text():
    cases = {
        "1,234.50": 1234.5,
        "$99": 99,
        "(250)": -250,
        "12%": 0.12,
        "-7": -7,
        "1 000": 1000,
        "€ 5": 5,
        "100 EUR": 100,
        "0.5": 0.5,
    }
    for text, expected in cases.items():
        assert parse_number_text(text) == expected, text
    for text in ("00123", "abc", "1.2.3", "12-34", "", "1,23"):
        assert parse_number_text(text) is None, text


def test_new_checks_have_no_false_positives_on_clean_models():
    from sheetguard.core.loader import load_workbook
    from tests.helpers import FIXTURES

    for name in ("dcf", "loan", "budget", "forecast", "captable", "structures"):
        wb = load_workbook(FIXTURES / f"{name}.xlsx")
        assert run_checks(wb, DepGraph(wb)) == [], name


def test_sheet_model_shape_for_helpers():
    # guard: tests above construct Cells directly; keep the dataclass contract visible
    s = Sheet(name="S", index=0, state="visible", cells={})
    assert s.used is None and not s.is_hidden
    assert ValueType.NUMBER.value == "number"


def test_c06_short_forecast_row_with_seed_and_one_paste():
    """Found in external review: in a 5-period roll-forward row the seed plus one paste
    made 2 constants and the row was skipped as a data block. Rows of 8+ were fine."""
    row = {"B1": 1000, "C1": "=B1*1.05", "D1": "=C1*1.05", "E1": 1157.63, "F1": "=E1*1.05"}
    (i,) = audit("C06", {"S": row})
    assert i.cell == "S!E1" and i.confidence is Confidence.HIGH
    assert i.suggested_fix is not None and i.suggested_fix.text == "=D1*1.05"
    # a 5-period row that is clean stays clean
    clean = {"B1": 1000} | {f"{c}1": f"={p}1*1.05" for p, c in zip("BCDE", "CDEF", strict=True)}
    assert audit("C06", {"S": clean}) == []
