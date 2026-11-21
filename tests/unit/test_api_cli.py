import json
import shutil

import pytest
from typer.testing import CliRunner

from sheetguard.api import analyze
from sheetguard.cli import app
from sheetguard.errors import InvalidArgumentError
from sheetguard.security.envelope import wrap
from sheetguard.security.limits import Deadline
from tests.helpers import FIXTURES

runner = CliRunner()


def test_api_audit_trace_outputs():
    a = analyze(FIXTURES / "dcf.xlsx")
    assert a.audit() == []
    t = a.trace("Model!B12", max_depth=2)
    assert t.root.address == "Model!B12" and t.truncated
    assert a.likely_outputs(1)[0][0] == "Model!B12"
    assert a.trace("model!b12").root.address == "Model!B12"


@pytest.mark.parametrize(
    ("cell", "direction", "kw"),
    [
        ("nonsense", "precedents", {}),
        ("Nope!A1", "precedents", {}),
        ("Model!B12", "sideways", {}),
        ("Model!B12", "precedents", {"max_depth": 99}),
    ],
)
def test_api_rejects_bad_arguments(cell, direction, kw):
    a = analyze(FIXTURES / "dcf.xlsx")
    with pytest.raises(InvalidArgumentError):
        a.trace(cell, direction, **kw)


def test_cli_exit_codes(tmp_path):
    assert runner.invoke(app, ["audit", str(FIXTURES / "dcf.xlsx")]).exit_code == 0
    r = runner.invoke(app, ["audit", str(FIXTURES / "errors.xlsx")])
    assert r.exit_code == 1 and "C02" in r.output and "suggested: =E2*E3" in r.output
    bad = tmp_path / "x.xls"
    bad.write_bytes(b"nope")
    r = runner.invoke(app, ["audit", str(bad)])
    assert r.exit_code == 2 and "unsupported_format" in r.output


def test_cli_fail_on_threshold():
    r = runner.invoke(app, ["audit", str(FIXTURES / "iterative.xlsx")])
    assert r.exit_code == 0  # only a warning, default threshold is critical
    r = runner.invoke(app, ["audit", str(FIXTURES / "iterative.xlsx"), "--fail-on", "warning"])
    assert r.exit_code == 1


def test_cli_json_is_deterministic(tmp_path):
    copy = tmp_path / "errors.xlsx"
    shutil.copy(FIXTURES / "errors.xlsx", copy)
    a = runner.invoke(app, ["audit", "--json", str(FIXTURES / "errors.xlsx")])
    b = runner.invoke(app, ["audit", "--json", str(copy)])
    assert a.output == b.output  # NF-3: byte-identical for the same file
    data = json.loads(a.output)
    assert data["schema_version"]
    assert len(data["issues"]) == data["summary"]["total"] == 8
    assert data["workbook"]["file_name"] == "errors.xlsx"
    first = data["issues"][0]
    assert first["evidence"][0]["formula"]["kind"] == "untrusted_text"


def test_cli_trace():
    r = runner.invoke(
        app, ["trace", str(FIXTURES / "loan.xlsx"), "Schedule!F3", "--max-depth", "1"]
    )
    assert r.exit_code == 0
    assert json.loads(r.output)["root"]["address"] == "Schedule!F3"
    r = runner.invoke(app, ["trace", str(FIXTURES / "loan.xlsx"), "Nope!A1"])
    assert r.exit_code == 2


def test_envelope_truncates_long_text():
    w = wrap("x" * 5000, "S!A1 (cell text)")
    assert w.truncated and len(w.text) == 2000 and w.original_length == 5000
    assert not wrap("short", "src").truncated


def test_deadline():
    from sheetguard.errors import TimeoutExceededError

    d = Deadline(0.0)
    with pytest.raises(TimeoutExceededError):
        d.check()
    assert Deadline(10).remaining > 0
