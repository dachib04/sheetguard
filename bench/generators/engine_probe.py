"""Formula-engine probe used for ADR 0001.

Usage: python bench/generators/engine_probe.py formulas|pycel MODEL TARGET INPUT
e.g.   python bench/generators/engine_probe.py formulas dcf Model!B12 Assumptions!B3

Evaluates TARGET in tests/fixtures/reference/MODEL.xlsx with a whole-workbook engine,
then re-evaluates after a +10% change to INPUT, printing values, timings and memory.
"""

from __future__ import annotations

import contextlib
import io
import logging
import resource
import sys
import time
import warnings
from typing import Any

FIXTURES = "tests/fixtures/reference"


def _norm(ref: str) -> str:
    return ref.upper().replace("'", "")


def with_formulas(path: str, target: str, inp: str) -> tuple[Any, Any, float]:
    import formulas

    model = formulas.ExcelModel().loads(path).finish()
    sol = model.calculate()
    started = time.perf_counter()
    tkey = next(k for k in sol if _norm(k).endswith(_norm(target)))
    ikey = next(k for k in sol if _norm(k).endswith(_norm(inp)))
    value = sol[tkey].value[0][0]
    changed = model.calculate(inputs={ikey: sol[ikey].value[0][0] * 1.1}, outputs=[tkey])
    return value, changed[tkey].value[0][0], time.perf_counter() - started


def with_pycel(path: str, target: str, inp: str) -> tuple[Any, Any, float]:
    from pycel import ExcelCompiler

    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        xc = ExcelCompiler(filename=path)
        value = xc.evaluate(target)
        started = time.perf_counter()
        xc.set_value(inp, xc.evaluate(inp) * 1.1)
        changed = xc.evaluate(target)
    return value, changed, time.perf_counter() - started


def main() -> None:
    warnings.filterwarnings("ignore")
    logging.disable(logging.CRITICAL)
    engine, name, target, inp = sys.argv[1:5]
    path = f"{FIXTURES}/{name}.xlsx"
    started = time.perf_counter()
    try:
        run = with_formulas if engine == "formulas" else with_pycel
        value, changed, recalc = run(path, target, inp)
    except Exception as exc:
        print(f"{engine} {name}: ERROR {type(exc).__name__}: {str(exc).splitlines()[-1][:100]}")
        return
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    print(
        f"{engine} {name}: value={value} total={time.perf_counter() - started:.2f}s "
        f"+10%->{changed} recalc={recalc * 1000:.0f}ms maxrss={rss}MB"
    )


if __name__ == "__main__":
    main()
