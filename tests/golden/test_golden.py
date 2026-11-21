"""Golden files (§10.1): the full JSON report for 10 reference workbooks.

Any change in output fails this test until it is reviewed. To accept a
deliberate change, regenerate and review the diff:

    SHEETGUARD_UPDATE_GOLDEN=1 pytest tests/golden
    git diff tests/golden/
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sheetguard.api import analyze
from sheetguard.report.json_report import to_json
from tests.helpers import FIXTURES

GOLDEN = Path(__file__).parent / "data"
MODELS = [
    "dcf",
    "loan",
    "budget",
    "forecast",
    "captable",
    "structures",
    "errors",
    "iterative",
    "checks_a",
    "checks_b",
]


@pytest.mark.parametrize("name", MODELS)
def test_golden_report(name):
    actual = to_json(analyze(FIXTURES / f"{name}.xlsx").report())
    path = GOLDEN / f"{name}.json"
    if os.environ.get("SHEETGUARD_UPDATE_GOLDEN"):
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(actual, encoding="utf-8", newline="\n")
    expected = path.read_text(encoding="utf-8")
    assert actual == expected, f"{name}: report changed; review and regenerate (see docstring)"


def test_golden_set_is_complete():
    assert len(MODELS) == 10
    assert sorted(p.stem for p in FIXTURES.glob("*.xlsx")) == sorted(MODELS)
