"""Pre-parse inspection of the OOXML zip container (FR-1, FR-2, S-2, S-3, S-4).

This runs on the raw bytes *before* openpyxl sees them. It:

* sniffs the real format (legacy .xls, encrypted OOXML, .xlsb, .ods, .docx...),
* decompresses every member in a streaming way and measures the *real* size,
  so lying zip headers cannot hide a bomb,
* rejects any XML part that declares a DTD or entity (OOXML never needs one,
  so this closes XXE and billion-laughs regardless of the XML parser used later),
* counts cells in worksheet parts so the cell limit is enforced before loading,
* records active content (macros, DDE, OLE, connections, external links).
"""

from __future__ import annotations

import logging
import posixpath
import re
import zipfile
import zlib
from dataclasses import dataclass, field
from io import BytesIO

import defusedxml.ElementTree as SafeET

from sheetguard.errors import (
    CorruptedFileError,
    LimitExceededError,
    PasswordProtectedError,
    UnsafeContentError,
    UnsupportedFormatError,
)
from sheetguard.security.limits import Limits

log = logging.getLogger("sheetguard.archive")

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZIP_MAGIC = b"PK\x03\x04"
_ENCRYPTED_MARKER = "EncryptedPackage".encode("utf-16-le")

_CHUNK = 1 << 20
_XML_SUFFIXES = (".xml", ".rels", ".vml")
_DTD_PATTERN = re.compile(rb"<!DOCTYPE|<!ENTITY", re.IGNORECASE)
_CELL_PATTERN = re.compile(rb"<(?:[A-Za-z0-9_]+:)?c[\s/>]")

_WORKBOOK_CONTENT_TYPES = (
    "spreadsheetml.sheet.main+xml",
    "spreadsheetml.template.main+xml",
    "ms-excel.sheet.macroEnabled.main+xml",
    "ms-excel.template.macroEnabled.main+xml",
)


@dataclass(slots=True)
class ArchiveReport:
    member_count: int = 0
    total_uncompressed: int = 0
    cell_elements: int = 0
    has_vba: bool = False
    has_xlm_macros: bool = False
    has_external_links: bool = False
    has_dde: bool = False
    has_ole_objects: bool = False
    has_activex: bool = False
    has_data_connections: bool = False
    external_link_targets: list[str] = field(default_factory=list)
    threaded_comment_parts: list[str] = field(default_factory=list)
    # (vector, location, text) for text openpyxl does not expose (scanned in week 3)
    extra_texts: list[tuple[str, str, str]] = field(default_factory=list)


def sniff_format(data: bytes, extension: str) -> None:
    """Raise a typed error for anything that is not a zip-based .xlsx/.xlsm."""
    ext = extension.lower()
    if data.startswith(OLE_MAGIC):
        if _ENCRYPTED_MARKER in data:
            raise PasswordProtectedError(
                "The workbook is password-protected (encrypted). Remove the password and retry."
            )
        raise UnsupportedFormatError(
            "Legacy binary .xls workbooks are not supported in Phase 1. Save as .xlsx and retry."
        )
    if ext in {".xls", ".xlsb", ".ods", ".numbers", ".csv"}:
        raise UnsupportedFormatError(f"The {ext} format is not supported. Save as .xlsx and retry.")
    if ext not in {".xlsx", ".xlsm"}:
        raise UnsupportedFormatError("Only .xlsx and .xlsm workbooks are supported.")
    if not data.startswith(ZIP_MAGIC):
        raise CorruptedFileError("The file is not a valid .xlsx/.xlsm workbook (not a zip).")


def _read_member(
    zf: zipfile.ZipFile, info: zipfile.ZipInfo, limits: Limits, budget_left: int
) -> tuple[int, bool, int]:
    """Stream one member. Returns (real_size, has_dtd, cell_elements)."""
    is_xml = info.filename.lower().endswith(_XML_SUFFIXES)
    is_sheet = info.filename.startswith("xl/worksheets/") and is_xml
    size = 0
    has_dtd = False
    cells = 0
    tail = b""
    with zf.open(info) as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > limits.max_member_uncompressed_bytes:
                raise LimitExceededError("A part of the workbook is too large when decompressed.")
            if size > budget_left:
                raise LimitExceededError("The workbook is too large when decompressed.")
            if (
                info.compress_size > 0
                and size > limits.ratio_min_member_bytes
                and size / info.compress_size > limits.max_compression_ratio
            ):
                raise LimitExceededError(
                    "Suspicious compression ratio (possible zip bomb); refusing to parse."
                )
            if is_xml:
                window = tail + chunk
                if not has_dtd and _DTD_PATTERN.search(window):
                    has_dtd = True
                if is_sheet:
                    # count matches that end inside the new chunk only
                    cells += sum(1 for m in _CELL_PATTERN.finditer(window) if m.end() > len(tail))
                tail = window[-16:]
    return size, has_dtd, cells


def inspect_archive(data: bytes, limits: Limits) -> ArchiveReport:
    try:
        zf = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise CorruptedFileError("The workbook is corrupted or truncated (bad zip).") from exc

    report = ArchiveReport()
    with zf:
        infos = zf.infolist()
        report.member_count = len(infos)
        if len(infos) > limits.max_zip_members:
            raise LimitExceededError("The workbook archive has too many parts.")
        names = {i.filename for i in infos}

        if "mimetype" in names and "content.xml" in names:
            raise UnsupportedFormatError("OpenDocument spreadsheets (.ods) are not supported.")
        if "xl/workbook.bin" in names:
            raise UnsupportedFormatError("Binary .xlsb workbooks are not supported. Save as .xlsx.")
        if "word/document.xml" in names or "ppt/presentation.xml" in names:
            raise CorruptedFileError("The file is a Word or PowerPoint document, not a workbook.")
        if "[Content_Types].xml" not in names:
            raise CorruptedFileError("The file is not a valid workbook (no content types part).")

        budget = limits.max_total_uncompressed_bytes
        try:
            for info in infos:
                if info.is_dir():
                    continue
                if info.flag_bits & 0x1:
                    raise PasswordProtectedError("The workbook archive is encrypted.")
                size, has_dtd, cells = _read_member(zf, info, limits, budget)
                budget -= size
                report.total_uncompressed += size
                report.cell_elements += cells
                if has_dtd:
                    raise UnsafeContentError(
                        "The workbook contains an XML DTD or entity declaration, which valid "
                        "Excel files never need. Refusing to parse (possible XXE or entity bomb)."
                    )
                if report.cell_elements > limits.max_cells:
                    raise LimitExceededError(
                        f"The workbook has more than {limits.max_cells:,} cells."
                    )
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError, ValueError) as exc:
            # CRC failures and truncated members surface as BadZipFile / zlib.error.
            raise CorruptedFileError("The workbook is corrupted or truncated.") from exc
        except NotImplementedError as exc:
            raise CorruptedFileError("The workbook uses an unsupported zip compression.") from exc

        content_types = zf.read("[Content_Types].xml").decode("utf-8", errors="replace")
        if not any(ct in content_types for ct in _WORKBOOK_CONTENT_TYPES):
            raise CorruptedFileError("The file is a zip archive but not an Excel workbook.")

        _detect_active_content(zf, names, report)
        _collect_extra_texts(zf, names, report)
    return report


def _detect_active_content(zf: zipfile.ZipFile, names: set[str], report: ArchiveReport) -> None:
    for name in sorted(names):
        low = name.lower()
        if low.endswith("vbaproject.bin"):
            report.has_vba = True
        elif low.startswith("xl/macrosheets/"):
            report.has_xlm_macros = True
        elif low.startswith("xl/embeddings/"):
            report.has_ole_objects = True
        elif low.startswith("xl/activex/"):
            report.has_activex = True
        elif low == "xl/connections.xml":
            report.has_data_connections = True
        elif low.startswith("xl/threadedcomments/") and low.endswith(".xml"):
            report.threaded_comment_parts.append(name)
        elif low.startswith("xl/externallinks/") and low.endswith(".xml") and "/_rels/" not in low:
            report.has_external_links = True
            body = zf.read(name)
            if b"ddeLink" in body:
                report.has_dde = True
            if b"oleLink" in body:
                report.has_ole_objects = True
            rels = posixpath.join(
                posixpath.dirname(name), "_rels", posixpath.basename(name) + ".rels"
            )
            if rels in names:
                report.external_link_targets.extend(_rel_targets(zf.read(rels)))


def _rel_targets(xml: bytes) -> list[str]:
    try:
        root = SafeET.fromstring(xml)
    except Exception:
        return []
    return [el.get("Target", "") for el in root if el.get("TargetMode") == "External"]


MAX_EXTRA_TEXT_CHARS = 2_000_000


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _collect_extra_texts(zf: zipfile.ZipFile, names: set[str], report: ArchiveReport) -> None:
    """Threaded comments, shape and chart text (§7.2): read with the hardened parser.

    DTDs were already rejected for every part, so defusedxml here is a second layer.
    """
    budget = MAX_EXTRA_TEXT_CHARS
    for name in sorted(names):
        low = name.lower()
        if low.startswith("xl/threadedcomments/") and low.endswith(".xml"):
            vector = "threaded_comment"
        elif low.startswith("xl/drawings/") and low.endswith(".xml") and "/_rels/" not in low:
            vector = "shape_text"
        elif low.startswith("xl/charts/") and low.endswith(".xml") and "/_rels/" not in low:
            vector = "chart_text"
        else:
            continue
        try:
            root = SafeET.fromstring(zf.read(name))
        except Exception:
            log.info("skipped unparseable optional part (%s)", vector)
            continue
        part = posixpath.basename(name)
        if vector == "threaded_comment":
            for el in root.iter():
                if _local(el.tag) != "threadedComment":
                    continue
                ref = el.get("ref", "?")
                text = "".join(t.text or "" for t in el.iter() if _local(t.tag) == "text")
                if text and budget > 0:
                    report.extra_texts.append((vector, f"{part} cell {ref}", text[:budget]))
                    budget -= len(text)
        else:
            chunks: list[str] = []
            for el in root.iter():
                tag = _local(el.tag)
                if not el.text:
                    continue
                numeric = el.text.replace(".", "", 1).replace("-", "", 1).isdigit()
                if tag == "t" or (vector == "chart_text" and tag == "v" and not numeric):
                    chunks.append(el.text)
            text = " ".join(chunks).strip()
            if text and budget > 0:
                report.extra_texts.append((vector, part, text[:budget]))
                budget -= len(text)
