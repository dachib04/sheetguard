"""Spec §9 edge cases that are not already covered elsewhere (one test each at least)."""

from __future__ import annotations

from pathlib import Path

import openpyxl
from openpyxl.worksheet.table import Table

from sheetguard.api import analyze
from sheetguard.core.evaluator import Evaluator
from sheetguard.core.graph import DepGraph
from sheetguard.core.refs import CellKey, analyze_formula
from tests.helpers import build, rewrite_zip, write_xlsx

SHARED_SHEET = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    b"<sheetData>"
    b'<row r="1"><c r="A1"><v>1</v></c><c r="B1"><f t="shared" ref="B1:B4" si="0">A1*2</f>'
    b"<v>2</v></c></row>"
    b'<row r="2"><c r="A2"><v>2</v></c><c r="B2"><f t="shared" si="0"/><v>4</v></c></row>'
    b'<row r="3"><c r="A3"><v>3</v></c><c r="B3"><f t="shared" si="0"/><v>6</v></c></row>'
    b'<row r="4"><c r="A4"><v>4</v></c><c r="B4"><f t="shared" si="0"/><v>8</v></c></row>'
    b"</sheetData></worksheet>"
)


def test_shared_formulas_expand_per_cell(tmp_path: Path):
    """Shared formulas (as Excel writes filled ranges) become one formula per cell."""
    base = write_xlsx(tmp_path / "s.xlsx", {"A1": 1})
    p = rewrite_zip(
        base, tmp_path / "shared.xlsx", replace={"xl/worksheets/sheet1.xml": SHARED_SHEET}
    )
    a = analyze(p)
    formulas = [a.workbook.cell(CellKey("Sheet1", r, 2)).formula for r in range(1, 5)]  # type: ignore[union-attr]
    assert formulas == ["=A1*2", "=A2*2", "=A3*2", "=A4*2"]
    assert a.audit(checks=["C02"]) == []
    assert a.trace("Sheet1!B3").root.children[0].address == "Sheet1!A3"


def test_all_sheets_hidden_except_one(tmp_path: Path):
    wb = openpyxl.Workbook()
    wb.active["A1"] = "=Data!A1*2"
    for name in ("Data", "Calc", "Lists"):
        ws = wb.create_sheet(name)
        ws["A1"] = 5
        ws.sheet_state = "hidden"
    wb.save(tmp_path / "h.xlsx")
    a = analyze(tmp_path / "h.xlsx")
    states = [s.state for s in a.workbook.sheets.values()]
    assert states == ["visible", "hidden", "hidden", "hidden"]
    report = a.report()
    assert report.workbook.flags.hidden_sheets == 3
    assert {i.check_id for i in report.issues} >= {"C11"}  # Data!A1 feeds a visible formula


def test_table_renamed_after_creation(tmp_path: Path):
    """Formulas use the display name; the internal name may still be the old one."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Item", "Revenue"])
    ws.append(["a", 10])
    ws.append(["b", 20])
    table = Table(displayName="Sales2026", ref="A1:B3")
    table.name = "Table1"
    ws.add_table(table)
    ws["D1"] = "=SUM(Sales2026[Revenue])"
    ws["D2"] = "=SUM(Table1[Revenue])"  # the old name still resolves
    wb.save(tmp_path / "t.xlsx")
    a = analyze(tmp_path / "t.xlsx")
    for cell in ("D1", "D2"):
        kids = [n.address for n in a.trace(f"Sheet!{cell}").root.children]
        assert kids == ["Sheet!B2:B3"], cell


def test_deeply_nested_functions():
    depth = 120
    formula = "=" + "IF(A1>0," * depth + "1" + ",0)" * depth
    info = analyze_formula(formula)
    assert info.ok and info.functions.count("IF") == depth
    wb = build({"S": {"A1": 1, "B1": formula}})
    g = DepGraph(wb)
    assert g.precedents(CellKey("S", 1, 2)) == (CellKey("S", 1, 1),)
    ev = Evaluator(g)
    plan = ev.plan(CellKey("S", 1, 2))
    assert ev.to_python(ev.evaluate(plan)[CellKey("S", 1, 2)]) == 1


def test_booleans_and_blank_strings_in_numeric_ranges():
    """Excel's SUM ignores booleans and text inside ranges; "" is not an empty cell."""
    wb = build(
        {
            "S": {
                "A1": 5,
                "A2": True,
                "A3": "",
                "A4": 7,
                "B1": "=SUM(A1:A4)",
                "B2": "=COUNTA(A1:A4)",
                "B3": "=COUNTBLANK(A1:A4)",
            }
        }
    )
    ev = Evaluator(DepGraph(wb))
    out = {}
    for row in (1, 2, 3):
        key = CellKey("S", row, 2)
        out[row] = ev.to_python(ev.evaluate(ev.plan(key))[key])
    assert out[1] == 12
    assert out[2] == 4
    assert out[3] == 1


def test_protected_sheet_is_readable_and_reported(tmp_path: Path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"], ws["B1"] = 3, "=A1*2"
    ws.protection.sheet = True
    ws.protection.password = "secret"
    wb.save(tmp_path / "p.xlsx")
    a = analyze(tmp_path / "p.xlsx")
    assert a.workbook.sheets["Sheet"].protected
    assert a.report().workbook.sheets[0].protected
    assert a.trace("Sheet!B1").root.children[0].address == "Sheet!A1"


def test_dynamic_array_formula_loads(tmp_path: Path):
    p = write_xlsx(
        tmp_path / "d.xlsx", {"A1": 3, "A2": 1, "A3": 2, "B1": "=_xlfn._xlws.SORT(A1:A3)"}
    )
    a = analyze(p)
    assert "SORT" in analyze_formula(a.workbook.cell(CellKey("Sheet1", 1, 2)).formula).functions  # type: ignore[arg-type,union-attr]
    assert a.trace("Sheet1!B1").root.children


def test_whole_column_refs_in_lookup_functions():
    wb = build(
        {
            "S": {
                "A1": "k1",
                "A2": "k2",
                "B1": 10,
                "B2": 20,
                "C1": '=VLOOKUP("k2",A:B,2,FALSE)',
                "C2": '=INDEX(B:B,MATCH("k1",A:A,0))',
            }
        }
    )
    ev = Evaluator(DepGraph(wb))
    for row, want in ((1, 20), (2, 10)):
        key = CellKey("S", row, 3)
        assert ev.to_python(ev.evaluate(ev.plan(key))[key]) == want
