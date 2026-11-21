from __future__ import annotations

import json
import re

import openpyxl
from typer.testing import CliRunner

from sheetguard.api import analyze
from sheetguard.cli import app
from sheetguard.core.fixes import from_neighbour, input_name_for, with_extended_range
from sheetguard.core.heuristics import column_header, is_id_column, is_index_like, row_label
from sheetguard.core.refs import CellKey
from sheetguard.report.html_report import to_html
from sheetguard.report.json_report import report_json_schema, to_json
from sheetguard.report.schema import Report
from tests.helpers import FIXTURES, build

runner = CliRunner()


def test_json_report_structure_and_determinism():
    a1 = analyze(FIXTURES / "checks_b.xlsx")
    a2 = analyze(FIXTURES / "checks_b.xlsx")
    j1, j2 = to_json(a1.report()), to_json(a2.report())
    assert j1 == j2
    data = json.loads(j1)
    assert data["schema_version"] == "0.1.0"
    assert data["summary"]["total"] == len(data["issues"])
    flags = data["workbook"]["flags"]
    assert flags["external_links"] and not flags["cached_values"] and flags["hidden_sheets"] == 1
    assert "timestamp" not in j1 and "/home/" not in j1  # NF-3, no paths
    Report.model_validate(data)  # round-trips through the schema


def test_report_options_and_filtering():
    a = analyze(FIXTURES / "checks_b.xlsx")
    r = a.report(checks=["C09", "C13"], min_severity="warning")  # type: ignore[arg-type]
    assert r.options.checks == ("C09", "C13")
    assert {i.check_id for i in r.issues} == {"C13"}


def test_json_schema_is_valid_json():
    schema = json.loads(report_json_schema())
    assert schema["title"] == "Report"
    assert "Issue" in schema["$defs"]


XSS = "<script>alert('x')</script><img src=x onerror=alert(1)>"


def test_html_escapes_workbook_text(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "<b>Sheet"
    ws["A1"] = XSS
    ws["B1"] = '="<script>alert(1)</script>"&D1*1.07'  # C01 -> formula shown as evidence
    ws["D1"] = 5
    ws["C1"] = "=B1&Z99"  # C03 -> evidence with the text value
    ws["A2"], ws["B2"] = "1,000", "=A2*1.07"  # C14 + C01
    wb.save(tmp_path / "xss.xlsx")
    a = analyze(tmp_path / "xss.xlsx")
    html = to_html(a.report())
    assert "<script" not in html.lower()
    assert "<img" not in html.lower()
    assert "<b>Sheet" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;b&gt;Sheet" in html
    assert "Content-Security-Policy" in html and "default-src 'none'" in html
    assert not re.search(r"<script\b", html, re.IGNORECASE)


def test_html_no_external_resources():
    html = to_html(analyze(FIXTURES / "errors.xlsx").report())
    assert "http://" not in html and "https://" not in html
    assert "Critical (5)" in html and "Untrusted content" in html
    assert "=E2*E3" in html


def test_html_clean_model():
    html = to_html(analyze(FIXTURES / "dcf.xlsx").report())
    assert "No issues found" in html and "Likely outputs" in html


# ---------------------------------------------------------------------- CLI


def test_cli_formats_and_out(tmp_path):
    out = tmp_path / "r.html"
    r = runner.invoke(app, ["audit", str(FIXTURES / "checks_a.xlsx"), "-f", "html", "-o", str(out)])
    assert r.exit_code == 1 and out.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
    j = tmp_path / "r.json"
    runner.invoke(app, ["audit", str(FIXTURES / "checks_a.xlsx"), "--json", "-o", str(j)])
    assert json.loads(j.read_text(encoding="utf-8"))["summary"]["total"] == 6
    r = runner.invoke(app, ["audit", str(FIXTURES / "dcf.xlsx"), "-o", str(tmp_path / "x.xlsx")])
    assert r.exit_code == 2 and "workbook file name" in r.output
    src = tmp_path / "self.json"
    src.write_text("{}")
    r = runner.invoke(app, ["audit", str(src), "-o", str(src)])
    assert r.exit_code == 2


def test_cli_text_summary_line():
    r = runner.invoke(app, ["audit", str(FIXTURES / "checks_b.xlsx")])
    assert r.exit_code == 0  # only warnings/info; default fail-on is critical
    assert "11 issue(s) - 0 critical, 7 warning, 4 info" in r.output


def test_cli_drivers_outputs_schema_version():
    r = runner.invoke(app, ["drivers", str(FIXTURES / "dcf.xlsx"), "Model!B12", "-n", "2"])
    assert r.exit_code == 0 and "Assumptions!B7" in r.output and "labels skipped" in r.output
    r = runner.invoke(app, ["drivers", str(FIXTURES / "dcf.xlsx"), "Model!B12", "--json"])
    assert json.loads(r.output)["drivers"][0]["input"] == "Assumptions!B7"
    r = runner.invoke(app, ["drivers", str(FIXTURES / "checks_b.xlsx"), "Report!B8"])
    assert "partial result" in r.output
    r = runner.invoke(app, ["drivers", str(FIXTURES / "dcf.xlsx"), "Assumptions!B2"])
    assert r.exit_code == 2  # not a formula
    r = runner.invoke(app, ["outputs", str(FIXTURES / "dcf.xlsx"), "--limit", "1"])
    assert r.output.startswith("Model!B12")
    assert json.loads(runner.invoke(app, ["schema"]).output)["title"] == "Report"
    assert runner.invoke(app, ["--version"]).output.startswith("sheetguard ")
    assert runner.invoke(app, ["outputs", "/nope.xlsx"]).exit_code == 2


# ---------------------------------------------------------------- fixes, heuristics


def test_fixes():
    assert from_neighbour("=B1*$A$1", "B2", "D2") == "=D1*$A$1"
    assert input_name_for("0.05") == "Input_0_05"
    assert input_name_for("5%") == "Input_5"
    assert input_name_for("%%") == "Input_value"
    assert with_extended_range("=SUM(A1:A5)", 1, (1, 1, 6, 1)) == "=SUM(A1:A6)"
    assert with_extended_range("=SUM(A1:A5)", 99, (1, 1, 6, 1)) is None


def test_heuristics():
    wb = build(
        {
            "S": {
                "A1": "Order no.",
                "A2": 1001,
                "A3": 1002,
                "B1": "Value",
                "B2": "12",
                "C2": 7,
                "D2": "Label",
                "E2": 5,
                "F2": "=E2+1",
            }
        },
        cached={"S!F2": 6},
    )
    assert is_index_like(wb, CellKey("S", 2, 1))
    assert not is_index_like(wb, CellKey("S", 2, 3))
    assert is_index_like(wb, CellKey("S", 2, 5))  # next to a roll-forward header
    assert column_header(wb, CellKey("S", 3, 1)) == "Order no."
    assert is_id_column(wb, CellKey("S", 3, 1)) and not is_id_column(wb, CellKey("S", 2, 2))
    assert row_label(wb, CellKey("S", 2, 5)) == "Label"
    assert row_label(wb, CellKey("S", 2, 4)) is None  # C2 is a number: another block
    assert column_header(wb, CellKey("Nope", 1, 1)) is None
