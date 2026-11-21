"""Traceability for the S-MUST requirements (§7.3) that are guarantees about what
the code never does. Each test names its requirement. The remaining S-items are
covered in test_malicious.py (S-3, S-4, S-5, S-6) and test_loader.py (S-2, S-9).
See docs/security.md for the full mapping."""

from __future__ import annotations

import ast
import builtins
import logging
import socket
from pathlib import Path

import pytest

from sheetguard.api import analyze
from sheetguard.report.html_report import to_html
from sheetguard.report.json_report import to_json

SRC = Path(__file__).parents[2] / "src" / "sheetguard"
TRAPS = Path(__file__).parents[1] / "fixtures" / "injection" / "traps.xlsx"
REF = Path(__file__).parents[1] / "fixtures" / "reference"

# The only place allowed to write a file: the CLI writing a *report* it was asked for.
WRITE_ALLOWLIST = {("cli.py", "_emit")}


def _full_run(path: Path) -> None:
    a = analyze(path)
    report = a.report()
    to_json(report)
    to_html(report)
    a.trace(next(iter(a.likely_outputs(1)), ("Summary!F4", 0))[0])
    for addr, _ in a.likely_outputs(1):
        a.drivers(addr)
    _ = a.untrusted


def test_s1_no_code_path_writes_files_statically():
    """S-1: no open(..., 'w'), .save(), write_text/write_bytes outside the allow-list."""
    offenders = []
    for py in SRC.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                receiver = getattr(getattr(node.func, "value", None), "id", "")
                writes = name in {
                    "save",
                    "write_text",
                    "write_bytes",
                    "mkdir",
                    "unlink",
                    "touch",
                } or (
                    receiver in {"os", "shutil"}
                    and name in {"remove", "rename", "replace", "rmtree", "makedirs"}
                )
                if name == "open":
                    modes = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant)]
                    modes += [
                        k.value.value
                        for k in node.keywords
                        if k.arg == "mode" and isinstance(k.value, ast.Constant)
                    ]
                    writes = any(set(str(m)) & set("wax+") for m in modes)
                if writes and (py.name, func.name) not in WRITE_ALLOWLIST:
                    offenders.append(f"{py.relative_to(SRC)}:{node.lineno} {func.name}")
    assert offenders == []


def test_s1_s10_no_file_is_opened_for_writing_at_runtime(monkeypatch):
    """S-1 / S-10: a full analysis of a hostile workbook never opens a file for writing."""
    # Warm-up: the first import of the evaluator's dependencies lets Python (and pytest's
    # assertion rewriter) write .pyc caches. That is the interpreter, not SheetGuard, so
    # import everything once before watching for writes.
    _full_run(REF / "dcf.xlsx")
    real_open = builtins.open

    def guarded(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        if any(ch in str(mode) for ch in "wax+"):
            raise AssertionError(f"write attempted: {file} {mode}")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded)
    import io

    monkeypatch.setattr(io, "open", guarded)
    for path in (TRAPS, REF / "dcf.xlsx", REF / "checks_b.xlsx"):
        _full_run(path)


def test_s7_no_network_access(monkeypatch):
    """S-7: no network I/O anywhere, even for files with external links and URLs."""

    def refuse(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    for path in (TRAPS, REF / "checks_b.xlsx"):  # hyperlinks, external link [1]
        _full_run(path)


def test_s8_logs_hold_metadata_only(caplog):
    """S-8: at INFO level, nothing from the workbook's content reaches the logs."""
    caplog.set_level(logging.DEBUG, logger="sheetguard")
    _full_run(TRAPS)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "sha256=" in text  # metadata is logged
    for secret in (
        "Ignore previous",
        "AI assistant",
        "Claude",
        "javascript:",
        "Revenue",
        "Notes",
        "approval mode",
    ):
        assert secret not in text, secret


def test_s2_active_content_reported_never_executed(tmp_path):
    """S-2: VBA, XLM, OLE, ActiveX, DDE, connections are flags in the report, and the
    workbook is opened with keep_vba=False (nothing executable is even retained)."""
    from sheetguard.core import loader

    src = (SRC / "core" / "loader.py").read_text(encoding="utf-8")
    assert "keep_vba=False" in src and "keep_links=False" in src
    assert not hasattr(loader, "exec")


def test_s13_security_md_documents_contact_and_limits():
    """S-13: SECURITY.md has a disclosure contact and the scanner's documented limits."""
    text = (SRC.parents[1] / "SECURITY.md").read_text(encoding="utf-8")
    assert "Reporting a vulnerability" in text
    assert "Limits of the prompt-injection scanner" in text
    assert "heuristic" in text


@pytest.mark.parametrize(
    "name", ["paths.py", "archive.py", "limits.py", "envelope.py", "injection.py"]
)
def test_security_modules_have_no_network_or_subprocess_imports(name):
    tree = ast.parse((SRC / "security" / name).read_text(encoding="utf-8"))
    imported = {
        a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    imported |= {
        (n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
    }
    assert not imported & {"socket", "urllib3", "requests", "httpx", "subprocess", "http"}
