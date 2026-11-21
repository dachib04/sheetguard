"""NF-1 / NF-2 performance check: open + audit synthetic workbooks.

Usage: python bench/perf.py [rows ...]   (each row holds 10 formulas)
Each size runs in a fresh process so peak memory is per size.
"""

from __future__ import annotations

import json
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def build(path: Path, rows: int) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["a", "b"] + [f"c{i}" for i in range(10)])
    for r in range(2, rows + 2):
        ws.append(
            [r, r * 2] + [f"=A{r}*B{r}+{i}" if i % 2 else f"=SUM(A{r}:B{r})*C$1" for i in range(10)]
        )
    s = wb.create_sheet("Sum")
    s["A1"] = f"=SUM(Data!C2:L{rows + 1})"
    s["A2"] = "=SUM(Data!C:C)"
    wb.save(path)


def measure(path: Path) -> dict[str, float]:
    from sheetguard.api import analyze

    t0 = time.perf_counter()
    a = analyze(path)
    t1 = time.perf_counter()
    issues = a.audit()
    findings = a.untrusted  # injection scan: part of every open_workbook
    t2 = time.perf_counter()
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024
    return {
        "formulas": a.workbook.formula_count,
        "load_s": round(t1 - t0, 2),
        "audit_and_scan_s": round(t2 - t1, 2),
        "total_s": round(t2 - t0, 2),
        "issues": len(issues),
        "untrusted_findings": len(findings),
        "peak_rss_mb": round(rss_mb),
    }


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--measure":
        print(json.dumps(measure(Path(sys.argv[2]))))
        return
    sizes = [int(x) for x in sys.argv[1:]] or [1_000, 20_000]
    with tempfile.TemporaryDirectory() as tmp:
        for rows in sizes:
            path = Path(tmp) / f"perf_{rows}.xlsx"
            build(path, rows)
            out = subprocess.run(
                [sys.executable, __file__, "--measure", str(path)],
                capture_output=True,
                text=True,
                check=True,
            )
            print(out.stdout.strip())


if __name__ == "__main__":
    main()
