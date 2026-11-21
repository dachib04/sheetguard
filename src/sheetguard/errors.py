"""Typed errors (FR-2, FR-53).

Every failure the user can cause maps to one of these. Messages are safe to show:
they never contain cell contents, stack traces or absolute paths outside the
allowed root.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    NOT_FOUND = "not_found"
    PATH_NOT_ALLOWED = "path_not_allowed"
    UNSUPPORTED_FORMAT = "unsupported_format"
    PASSWORD_PROTECTED = "password_protected"  # noqa: S105 - an error code
    CORRUPTED = "corrupted"
    LIMIT_EXCEEDED = "limit_exceeded"
    UNSAFE_CONTENT = "unsafe_content"
    INVALID_ARGUMENT = "invalid_argument"
    UNKNOWN_WORKBOOK = "unknown_workbook"
    TIMEOUT = "timeout"
    MISSING_DEPENDENCY = "missing_dependency"
    INTERNAL = "internal"


class SheetGuardError(Exception):
    """Base class. ``code`` is stable and machine-readable."""

    code: ErrorCode = ErrorCode.INTERNAL

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code.value, "message": self.message}


class NotFoundError(SheetGuardError):
    code = ErrorCode.NOT_FOUND


class PathNotAllowedError(SheetGuardError):
    code = ErrorCode.PATH_NOT_ALLOWED


class UnsupportedFormatError(SheetGuardError):
    code = ErrorCode.UNSUPPORTED_FORMAT


class PasswordProtectedError(SheetGuardError):
    code = ErrorCode.PASSWORD_PROTECTED


class CorruptedFileError(SheetGuardError):
    code = ErrorCode.CORRUPTED


class LimitExceededError(SheetGuardError):
    code = ErrorCode.LIMIT_EXCEEDED


class UnsafeContentError(SheetGuardError):
    """The file contains a construct we refuse to parse at all (e.g. an XML DTD)."""

    code = ErrorCode.UNSAFE_CONTENT


class InvalidArgumentError(SheetGuardError):
    code = ErrorCode.INVALID_ARGUMENT


class UnknownWorkbookError(SheetGuardError):
    code = ErrorCode.UNKNOWN_WORKBOOK


class TimeoutExceededError(SheetGuardError):
    code = ErrorCode.TIMEOUT


class MissingDependencyError(SheetGuardError):
    """An optional extra (e.g. ``sheetguard[eval]``) is not installed."""

    code = ErrorCode.MISSING_DEPENDENCY
