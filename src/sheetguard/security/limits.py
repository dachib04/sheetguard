"""Resource limits (S-4, S-5, FR-12, FR-24, FR-51).

All defaults come from the Phase 1 spec. Every limit is enforced *before* the
expensive step it protects, and every violation raises ``LimitExceededError``.
"""

from __future__ import annotations

import time

from pydantic import BaseModel, ConfigDict, Field

from sheetguard.errors import TimeoutExceededError

MB = 1024 * 1024


class Limits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # S-5 file and parse limits
    max_file_bytes: int = Field(default=50 * MB, gt=0)
    max_cells: int = Field(default=2_000_000, gt=0)
    max_formula_length: int = Field(default=16_384, gt=0)
    max_recursion_depth: int = Field(default=64, gt=0)
    call_timeout_s: float = Field(default=30.0, gt=0)
    max_sheets: int = Field(default=1_000, gt=0)

    # S-4 zip-bomb protection (measured on real decompressed bytes, not headers)
    max_total_uncompressed_bytes: int = Field(default=500 * MB, gt=0)
    max_member_uncompressed_bytes: int = Field(default=200 * MB, gt=0)
    max_zip_members: int = Field(default=10_000, gt=0)
    max_compression_ratio: float = Field(default=200.0, gt=1)
    # Ratio is only judged on members bigger than this, tiny XML parts compress wildly.
    ratio_min_member_bytes: int = Field(default=1 * MB, ge=0)

    # FR-12 tracing
    trace_max_depth: int = Field(default=6, gt=0)
    trace_max_nodes: int = Field(default=500, gt=0)

    # FR-24 drivers
    max_driver_inputs: int = Field(default=200, gt=0)

    # FR-51 response size
    max_response_bytes: int = Field(default=20_000, gt=1_000)


DEFAULT_LIMITS = Limits()


class Deadline:
    """Cooperative per-call timeout (S-5). Long loops call ``check()``."""

    def __init__(self, seconds: float) -> None:
        self._end = time.monotonic() + seconds

    def check(self) -> None:
        if time.monotonic() > self._end:
            raise TimeoutExceededError("The operation exceeded the per-call time limit.")

    @property
    def remaining(self) -> float:
        return max(0.0, self._end - time.monotonic())
