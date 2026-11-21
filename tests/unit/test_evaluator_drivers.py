from __future__ import annotations

import math

import pytest

from sheetguard.api import analyze
from sheetguard.core.drivers import top_drivers
from sheetguard.core.evaluator import Evaluator, iso_to_serial
from sheetguard.core.graph import DepGraph, NameNode
from sheetguard.core.loader import load_workbook
from sheetguard.core.refs import CellKey
from sheetguard.errors import InvalidArgumentError
from tests.helpers import FIXTURES, build


def K(a: str, s: str = "S") -> CellKey:
    from sheetguard.core.refs import parse_cell_address

    return parse_cell_address(a, default_sheet=s)


def ev_of(sheets, **kw):
    g = DepGraph(build(sheets, **kw))
    return g, Evaluator(g)


def value(ev: Evaluator, target: CellKey):
    plan = ev.plan(target)
    assert not plan.partial, plan.reasons
    return ev.to_python(ev.evaluate(plan)[target])


@pytest.mark.parametrize(
    "name", ["dcf", "loan", "budget", "forecast", "captable", "structures", "checks_a"]
)
def test_recalculation_matches_cached_values(name):
    """Every non-cyclic formula recalculates to the value the spreadsheet app cached."""
    wb = load_workbook(FIXTURES / f"{name}.xlsx")
    g = DepGraph(wb)
    ev = Evaluator(g)
    checked = 0
    for key, cell in wb.iter_formula_cells():
        plan = ev.plan(key)
        if plan.partial:
            continue
        got = ev.to_python(ev.evaluate(plan)[key])
        want = cell.value
        if isinstance(want, int | float) and isinstance(got, int | float):
            assert math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-6), (key, got, want)
        else:
            assert got == want, (key, got, want)
        checked += 1
    assert checked >= 5


def test_ranges_names_tables_and_text():
    _, ev = ev_of(
        {
            "S": {
                "A1": 2,
                "A2": 3,
                "A3": None,
                "B1": "=SUM(A1:A3)*Rate",
                "C1": '=IF(A1>1,"big","small")&"!"',
                "D1": "=A1/0",
                "E1": "=IFERROR(D1,-1)",
                "F1": "=Z99*2",
                "G1": "=TRUE()",
            }
        },
        names=[("Rate", None, "S!$A$2")],
    )
    assert value(ev, K("B1")) == 15
    assert value(ev, K("C1")) == "big!"
    assert value(ev, K("D1")) == "#DIV/0!"
    assert value(ev, K("E1")) == -1
    assert value(ev, K("F1")) == 0
    assert value(ev, K("G1")) is True


def test_whole_column_keeps_anchor_for_index():
    _, ev = ev_of({"S": {"A3": 10, "A4": 20, "B1": "=INDEX(A:A,4)", "B2": "=SUM(A:A)"}})
    assert value(ev, K("B1")) == 20
    assert value(ev, K("B2")) == 30


def test_constant_name_and_name_range():
    _, ev = ev_of(
        {"S": {"A1": 1, "A2": 2, "B1": "=SUM(Data)*Const"}},
        names=[("Data", None, "S!$A$1:$A$2"), ("Const", None, "10")],
    )
    assert value(ev, K("B1")) == 30


def test_partial_for_unsupported_dynamic_cycle_3d_and_external():
    _, ev = ev_of(
        {
            "S": {
                "A1": 1,
                "B1": '=INDIRECT("A1")',
                "B2": "=OFFSET(A1,0,0)",
                "C1": "=C2",
                "C2": "=C1",
                "D1": "=SUM(S:T!A1)",
                "E1": "=[1]X!A1",
                "F1": "=LAMBDA(x,x)(1)",
                "G1": "=SUM(",
            },
            "T": {"A1": 2},
        }
    )
    reasons = {}
    for cell in ("B1", "B2", "C1", "D1", "E1", "F1", "G1"):
        plan = ev.plan(K(cell))
        assert plan.partial, cell
        reasons[cell] = (plan.unsupported, plan.reasons)
    assert "INDIRECT" in reasons["B1"][0]
    assert "OFFSET" in reasons["B2"][0]
    assert any("circular" in r for r in reasons["C1"][1])
    assert any("3D" in r for r in reasons["D1"][1])
    assert any("external" in r for r in reasons["E1"][1])
    assert any("parsed" in r for r in reasons["G1"][1])


def test_incremental_matches_full_recalc():
    wb = load_workbook(FIXTURES / "loan.xlsx")
    g = DepGraph(wb)
    ev = Evaluator(g)
    target = CellKey("Schedule", 63, 3)  # total interest
    plan = ev.plan(target)
    base = ev.evaluate(plan)
    principal = CellKey("Inputs", 1, 2)
    inc = ev.evaluate(plan, {principal: 300_000}, base)[target]
    full = ev.evaluate(plan, {principal: 300_000})[target]
    assert math.isclose(ev.to_python(inc), ev.to_python(full))  # type: ignore[arg-type]
    assert ev.to_python(inc) != ev.to_python(base[target])


def test_override_does_not_touch_model():
    wb = load_workbook(FIXTURES / "dcf.xlsx")
    before = wb.cell(CellKey("Assumptions", 7, 2))
    a = analyze(FIXTURES / "dcf.xlsx")
    a.drivers("Model!B12")
    assert load_workbook(FIXTURES / "dcf.xlsx").cell(CellKey("Assumptions", 7, 2)) == before


def test_iso_to_serial():
    assert iso_to_serial("1900-01-01T00:00:00", False) == 1
    assert iso_to_serial("1900-03-01T00:00:00", False) == 61
    assert iso_to_serial("2026-11-21T12:00:00", False) == 46347.5
    assert iso_to_serial("1904-01-02T00:00:00", True) == 1
    assert iso_to_serial("12:00:00", False) == 0.5
    assert iso_to_serial("not a date", False) == "not a date"


# ------------------------------------------------------------------------ drivers


def test_drivers_rank_dcf_inputs():
    a = analyze(FIXTURES / "dcf.xlsx")
    r = a.drivers("Model!B12", n=3)
    assert not r.partial
    assert [d.input for d in r.drivers] == ["Assumptions!B7", "Assumptions!B4", "Assumptions!B2"]
    rev = r.drivers[2]
    assert math.isclose(rev.pct_of_baseline or 0, 10.0, rel_tol=1e-6)  # linear in revenue
    assert r.index_inputs_skipped == 6
    assert isinstance(r.cached_value, float)
    assert math.isclose(r.baseline, r.cached_value, rel_tol=1e-9)  # type: ignore[arg-type]


def test_drivers_include_index_inputs():
    a = analyze(FIXTURES / "dcf.xlsx")
    r = a.drivers("Model!B12", n=20, include_index_inputs=True)
    assert r.index_inputs_skipped == 0 and r.inputs_analysed == 13


def test_drivers_zero_input_uses_absolute_delta():
    g = DepGraph(build({"S": {"A1": 0, "A2": 5, "B1": "=A1*10+A2"}}))
    r = top_drivers(g, K("B1"), zero_delta=0.5)
    zero = next(d for d in r.drivers if d.input == "S!A1")
    assert zero.zero_input and zero.delta == 0.5 and zero.abs_change == 5
    other = next(d for d in r.drivers if d.input == "S!A2")
    assert not other.zero_input and math.isclose(other.delta, 0.5)


def test_drivers_name_inputs_and_non_numeric():
    g = DepGraph(
        build(
            {"S": {"A1": "x", "A2": 4, "B1": '=IF(A1="x",A2*Rate,0)'}},
            names=[("Rate", None, "0.5")],
        )
    )
    r = top_drivers(g, K("B1"))
    names = {d.input: d for d in r.drivers}
    assert names["Rate"].kind == "name"
    assert r.non_numeric_inputs == 1
    assert NameNode(None, "Rate") in g.upstream_inputs(K("B1"), 10)[0]


def test_drivers_partial_returns_no_numbers():
    g = DepGraph(build({"S": {"A1": 1, "B1": '=INDIRECT("A1")*2'}}))
    r = top_drivers(g, K("B1"))
    assert r.partial and r.drivers == () and r.baseline is None
    assert r.unsupported_functions == ("INDIRECT",)


def test_drivers_cap_reported():
    cells = {f"A{i}": i * 1.5 for i in range(1, 30)} | {"B1": "=SUM(A1:A29)"}
    r = top_drivers(DepGraph(build({"S": cells})), K("B1"), max_inputs=10)
    assert r.capped and r.inputs_analysed == 10


@pytest.mark.parametrize("kw", [{"delta_pct": 0}, {"delta_pct": 5000}, {"zero_delta": 0}, {"n": 0}])
def test_drivers_argument_validation(kw):
    g = DepGraph(build({"S": {"A1": 1, "B1": "=A1"}}))
    with pytest.raises(InvalidArgumentError):
        top_drivers(g, K("B1"), **kw)


def test_drivers_target_must_be_formula():
    g = DepGraph(build({"S": {"A1": 1}}))
    with pytest.raises(InvalidArgumentError):
        top_drivers(g, K("A1"))


def test_shape_functions_resolved_statically():
    _, ev = ev_of(
        {
            "S": {
                "A2": 1,
                "A3": 2,
                "A4": 3,
                "B1": "=ROWS(A2:A4)",
                "B2": "=COLUMNS(A1:D1)",
                "B3": "=ROW(A4)+COLUMN(C1)",
                "B4": "=ROW()",
                "B5": "=ROWS(A:A)",
            }
        }
    )
    assert value(ev, K("B1")) == 3
    assert value(ev, K("B2")) == 4
    assert value(ev, K("B3")) == 7
    assert value(ev, K("B4")) == 4
    assert value(ev, K("B5")) == 1_048_576


def test_shape_function_on_name_is_partial():
    _, ev = ev_of(
        {"S": {"A1": 1, "B1": "=ROWS(Data)", "C1": '=CELL("row",A1)'}},
        names=[("Data", None, "S!$A$1:$A$3")],
    )
    assert "ROWS" in ev.plan(K("B1")).unsupported
    assert "CELL" in ev.plan(K("C1")).unsupported


def test_missing_evaluator_is_a_typed_error(monkeypatch):
    import builtins

    from sheetguard.core import evaluator as ev_mod
    from sheetguard.errors import ErrorCode, MissingDependencyError

    real_import = builtins.__import__

    def no_formulas(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "formulas" or name.startswith("formulas."):
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_formulas)
    with pytest.raises(MissingDependencyError) as err:
        ev_mod._load_engine()
    assert err.value.code is ErrorCode.MISSING_DEPENDENCY
    assert "sheetguard[eval]" in err.value.message
