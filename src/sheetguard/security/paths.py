"""Path safety for the MCP server (S-6).

Only files inside a configured allowed root may be opened. Symlinks are fully
resolved first, so a link inside the root that points outside it is rejected,
and ``..`` segments cannot escape.
"""

from __future__ import annotations

import os
from pathlib import Path

from sheetguard.errors import NotFoundError, PathNotAllowedError


def resolve_inside_root(user_path: str, allowed_root: Path) -> Path:
    """Return the real path of ``user_path`` if it lies inside ``allowed_root``.

    Relative paths are interpreted relative to the root. The error messages
    never echo the resolved target, so a probe cannot learn what a link points to.
    """
    if not user_path or "\x00" in user_path:
        raise PathNotAllowedError("Path is empty or contains a NUL byte.")

    root = Path(os.path.realpath(allowed_root))
    candidate = Path(user_path)
    if not candidate.is_absolute():
        candidate = root / candidate

    real = Path(os.path.realpath(candidate))
    if real != root and root not in real.parents:
        raise PathNotAllowedError("Path is outside the allowed root.")
    if not real.exists():
        raise NotFoundError("File not found inside the allowed root.")
    if not real.is_file():
        raise PathNotAllowedError("Path is not a regular file.")
    return real
