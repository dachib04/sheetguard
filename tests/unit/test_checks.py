"""Positive and negative cases for every implemented check (§5.3)."""

import pytest

from sheetguard.checks import REGISTRY, run_checks
from sheetguard.checks.c02_inconsistent import suggest_from_neighbour
from sheetguard.core.graph import DepGraph
from sheetguard.core.loader import load_workbook
from sheetguard.errors import InvalidArgumentError
from sheetguard.report.schema import Confidence, Severity
from tests.helpers import FIXTURES, build


def audit(sheets, checks=None, **kw):
    wb = build(sheets, **kw)
    return run_checks(wb, DepGraph(wb), checks)


def row(formulas: dict[str, str], extra: dict | None = None) -> dict:
    base = {f"{c}1": i for i, c in enumerate("BCDEFGH", start=1)}
    base |= {f"{c}2": i * 2 for i, c in enumerate("BCDEFGH", start=1)}
    return base | formulas | (extra or {})


# ---------------------------------------------------------------------------- C02


def test_c02_flags_interior_outlier_with_fix():
    f = {f"{c}3": f"={c}1*{c}2" for c in "BCDEFG"}
    f["E3"] = "=E1*E1"
    (issue,) = audit({"S": row(f)}, ["C02"])
    assert issue.cell == "S!E3" and issue.severity is Severity.CRITICAL
    assert issue.confidence is Confidence.HIGH
    assert issue.suggested_fix is not None and issue.suggested_fix.text == "=E1*E2"
    assert issue.evidence[0].note == "outlier"


def test_c02_vertical_outlier():
    cells = {f"A{r}": r for r in range(1, 8)} | {f"B{r}": f"=A{r}*2" for r in range(1, 8)}
    cells["B5"] = "=A5*3"
    (issue,) = audit({"S": cells}, ["C02"])
    assert issue.cell == "S!B5" and "column" in issue.description


def test_c02_consistent_row_is_clean():
    f = {f"{c}3": f"={c}1*{c}2" for c in "BCDEFG"}
    assert audit({"S": row(f)}, ["C02"]) == []


def test_c02_ignores_total_at_end_and_start():
    f = {f"{c}3": f"={c}1*{c}2" for c in "BCDEFG"} | {"H3": "=SUM(B3:G3)"}
    f2 = {f"{c}3": f"={c}1*{c}2" for c in "CDEFGH"} | {"B3": "=SUM(C3:H3)"}
    assert audit({"S": row(f)}, ["C02"]) == []
    assert audit({"S": row(f2)}, ["C02"]) == []


def test_c02_ignores_opening_period_of_roll_forward():
    f = {"B3": "=B1"} | {f"{c}3": f"={p}3+{c}2" for p, c in zip("BCDEF", "CDEFG", strict=True)}
    assert audit({"S": row(f)}, ["C02"]) == []


def test_c02_requires_three_consistent_neighbours():
    f = {"B3": "=B1*B2", "C3": "=C1*C2", "D3": "=D1+D2", "E3": "=E1-E2"}
    assert audit({"S": row(f)}, ["C02"]) == []


def test_c02_edge_outlier_low_confidence():
    f = {f"{c}3": f"={c}1*{c}2" for c in "BCDEF"} | {"G3": "=G1+5"}
    (issue,) = audit({"S": row(f)}, ["C02"])
    assert issue.cell == "S!G3" and issue.confidence is Confidence.LOW


def test_c02_reported_once_when_both_axes_disagree():
    cells = {f"{c}{r}": f"={c}{r - 1}+1" for c in "BCDEF" for r in range(2, 7)}
    cells |= {f"{c}1": 1 for c in "BCDEF"}
    cells["D4"] = "=D3*100"
    issues = audit({"S": cells}, ["C02"])
    assert [i.cell for i in issues] == ["S!D4"]


def test_suggest_from_neighbour():
    assert suggest_from_neighbour("=C1*$A$1", "C3", "E3") == "=E1*$A$1"
    assert suggest_from_neighbour("=SUM(", "C3", "E3") in (None, "=SUM(")


# ---------------------------------------------------------------------------- C04


def test_c04_root_with_propagation():
    issues = audit(
        {"S": {"A1": 0, "B1": "=1/A1", "C1": "=B1*2", "D1": "=C1+1"}},
        ["C04"],
        cached={"S!B1": "#DIV/0!", "S!C1": "#DIV/0!", "S!D1": "#DIV/0!"},
    )
    (issue,) = issues
    assert issue.cell == "S!B1" and issue.severity is Severity.CRITICAL
    assert issue.affected_count == 3
    assert "propagates to 2" in issue.description


def test_c04_na_absorbed_by_iferror_is_info():
    (issue,) = audit(
        {"S": {"A1": "=VLOOKUP(9,B1:C2,2,0)", "A2": "=IFERROR(A1,0)"}},
        ["C04"],
        cached={"S!A1": "#N/A", "S!A2": 0},
    )
    assert issue.severity is Severity.INFO


def test_c04_other_error_masked_is_warning():
    (issue,) = audit(
        {"S": {"A1": "=1/0", "A2": "=IFERROR(A1,0)"}},
        ["C04"],
        cached={"S!A1": "#DIV/0!", "S!A2": 0},
    )
    assert issue.severity is Severity.WARNING


def test_c04_deliberate_na_is_info():
    (issue,) = audit({"S": {"A1": "=NA()"}}, ["C04"], cached={"S!A1": "#N/A"})
    assert issue.severity is Severity.INFO


def test_c04_na_not_absorbed_is_critical():
    (issue,) = audit(
        {"S": {"A1": "=VLOOKUP(9,B1:C2,2,0)", "A2": "=A1*2"}},
        ["C04"],
        cached={"S!A1": "#N/A", "S!A2": "#N/A"},
    )
    assert issue.severity is Severity.CRITICAL and issue.affected_count == 2


def test_c04_literal_ref_even_without_cached_value():
    (issue,) = audit({"S": {"A1": "=#REF!*2"}}, ["C04"])
    assert issue.severity is Severity.CRITICAL and "broken reference" in issue.description


def test_c04_constant_error_cell():
    (issue,) = audit({"S": {"A1": "#N/A", "B1": "=A1"}}, ["C04"], cached={"S!B1": "#N/A"})
    assert issue.cell == "S!A1" and issue.affected_count == 2


def test_c04_clean_and_cycle_errors_left_to_c05():
    assert audit({"S": {"A1": 1, "B1": "=A1"}}, ["C04"], cached={"S!B1": 1}) == []
    assert (
        audit(
            {"S": {"A1": "=B1", "B1": "=A1"}},
            ["C04"],
            cached={"S!A1": "#VALUE!", "S!B1": "#VALUE!"},
        )
        == []
    )


# ---------------------------------------------------------------------------- C05


def test_c05_cycles():
    issues = audit({"S": {"A1": "=A1+1", "B1": "=C1", "C1": "=D1", "D1": "=B1"}}, ["C05"])
    assert [(i.cell, i.affected_count) for i in issues] == [("S!A1", 1), ("S!B1", 3)]
    assert all(i.severity is Severity.CRITICAL for i in issues)
    assert "refers to itself" in issues[0].description


def test_c05_iterative_is_warning():
    (issue,) = audit({"S": {"A1": "=B1", "B1": "=A1*0.5"}}, ["C05"], iterate=True)
    assert issue.severity is Severity.WARNING and "Iterative" in issue.description


def test_c05_no_cycle():
    assert audit({"S": {"A1": 1, "B1": "=A1", "C1": "=SUM(A1:B1)"}}, ["C05"]) == []


def test_c05_long_cycle_truncated_in_description():
    cells = {f"A{i}": f"=A{i + 1}" for i in range(1, 30)} | {"A30": "=A1"}
    (issue,) = audit({"S": cells}, ["C05"])
    assert "(+20 more)" in issue.description and len(issue.evidence) == 10


# ------------------------------------------------------------------------- runner


def test_reference_models_clean():
    for name in ("dcf", "loan", "budget"):
        wb = load_workbook(FIXTURES / f"{name}.xlsx")
        assert run_checks(wb, DepGraph(wb)) == [], name


def test_seeded_errors_found_exactly():
    wb = load_workbook(FIXTURES / "errors.xlsx")
    found = {(i.check_id, i.cell) for i in run_checks(wb, DepGraph(wb), ["C02", "C04", "C05"])}
    assert found == {
        ("C02", "Calc!E4"),
        ("C04", "Calc!B7"),
        ("C04", "Calc!F7"),
        ("C04", "Calc!D7"),
        ("C05", "Calc!B11"),
        ("C05", "Calc!B12"),
    }


def test_seeded_checks_a_and_b_found_exactly():
    wb = load_workbook(FIXTURES / "checks_a.xlsx")
    found = {(i.check_id, i.cell) for i in run_checks(wb, DepGraph(wb))}
    assert found == {
        ("C06", "Model!E5"),
        ("C01", "Model!B5"),
        ("C03", "Model!B7"),
        ("C07", "Model!I8"),
        ("C12", "Model!E5"),
        ("C12", "Model!I7"),
    }
    wb = load_workbook(FIXTURES / "checks_b.xlsx")
    found_ids = {i.check_id for i in run_checks(wb, DepGraph(wb))}
    assert found_ids == {"C01", "C08", "C09", "C10", "C11", "C12", "C13", "C14"}


def test_stable_ids_and_order_across_loads():
    a = load_workbook(FIXTURES / "errors.xlsx")
    b = load_workbook(FIXTURES / "errors.xlsx")
    ia, ib = run_checks(a, DepGraph(a)), run_checks(b, DepGraph(b))
    assert [i.model_dump_json() for i in ia] == [i.model_dump_json() for i in ib]
    assert ia[0].severity is Severity.CRITICAL and ia[-1].severity is Severity.INFO


def test_min_severity_and_selection():
    wb = load_workbook(FIXTURES / "errors.xlsx")
    g = DepGraph(wb)
    crit = run_checks(wb, g, min_severity=Severity.CRITICAL)
    assert all(i.severity is Severity.CRITICAL for i in crit)
    assert len(crit) == 5
    assert {i.check_id for i in run_checks(wb, g, ["C05"])} == {"C05"}
    with pytest.raises(InvalidArgumentError):
        run_checks(wb, g, ["C99"])


def test_registry_has_metadata():
    for cid, cls in REGISTRY.items():
        assert cls.id == cid and cls.title and cls.default_severity


# ---------------------------------------------------------- found by the benchmark


def test_c04_errors_flowing_out_of_a_cycle_are_left_to_c05():
    """Benchmark run 1: downstream #VALUE! of a cycle was reported as 83 C04 roots."""
    issues = audit(
        {"S": {"A1": "=B1+1", "B1": "=A1+1", "C1": "=A1*2", "D1": "=C1+5"}},
        ["C04", "C05"],
        cached={"S!A1": "#VALUE!", "S!B1": "#VALUE!", "S!C1": "#VALUE!", "S!D1": "#VALUE!"},
    )
    assert {i.check_id for i in issues} == {"C05"}


def test_c04_independent_error_next_to_a_cycle_still_reported():
    issues = audit(
        {"S": {"A1": "=B1", "B1": "=A1", "E1": 0, "F1": "=1/E1"}},
        ["C04"],
        cached={"S!A1": "#VALUE!", "S!B1": "#VALUE!", "S!F1": "#DIV/0!"},
    )
    assert [i.cell for i in issues] == ["S!F1"]


def test_c02_columns_need_a_stronger_majority_than_rows():
    """Benchmark run 1: stacked P&L line items (4 of 6 alike) were flagged in a column."""
    col = {f"A{r}": r for r in range(1, 7)}
    col |= {f"B{r}": f"=A{r}*2" for r in (1, 2, 3, 4)} | {"B5": "=SUM(B1:B2)", "B6": "=B4-B3"}
    assert audit({"S": col}, ["C02"]) == []
    row = {f"{c}1": i for i, c in enumerate("ABCDEF", 1)}
    row |= {f"{c}2": f"={c}1*2" for c in "ABCD"} | {"E2": "=E1+7", "F2": "=F1*2"}
    assert [i.cell for i in audit({"S": row}, ["C02"])] == ["S!E2"]
