"""Untrusted content envelope (§6.1, MUST).

Every string that originates in a workbook (cell text, formulas, comments,
names, properties...) leaves SheetGuard inside an ``UntrustedText`` wrapper, so
a client agent can always tell file data from instructions. The injection
scanner (Week 3) fills ``flags`` and ``risk``; the envelope protects even when
the scanner misses, because the label is unconditional.

Cell *addresses* (including sheet names) are identifiers and are returned as
plain strings; sheet names are still scanned and reported as a vector (§7.2).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict

Risk = Literal["none", "low", "medium", "high"]

DEFAULT_MAX_TEXT = 2_000


class UntrustedText(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["untrusted_text"] = "untrusted_text"
    source: str
    text: str
    flags: tuple[str, ...] = ()
    risk: Risk = "none"
    truncated: bool = False
    original_length: int | None = None


def wrap(
    text: str,
    source: str,
    *,
    flags: tuple[str, ...] = (),
    risk: Risk = "none",
    max_len: int = DEFAULT_MAX_TEXT,
) -> UntrustedText:
    """Wrap workbook text. Long strings are truncated (and say so), never dropped."""
    if len(text) > max_len:
        return UntrustedText(
            source=source,
            text=text[:max_len],
            flags=flags,
            risk=risk,
            truncated=True,
            original_length=len(text),
        )
    return UntrustedText(source=source, text=text, flags=flags, risk=risk)


ModelT = TypeVar("ModelT", bound=BaseModel)
RISK_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}


def annotate(model: ModelT, index: Mapping[str, tuple[Risk, tuple[str, ...]]]) -> ModelT:
    """Copy ``model`` with every ``UntrustedText`` labelled by the scanner's verdict for
    the cell it came from (matched on the address before " (" in ``source``)."""
    if not index:
        return model
    return _annotate(model, index)  # type: ignore[no-any-return]


def _annotate(obj: Any, index: Mapping[str, tuple[Risk, tuple[str, ...]]]) -> Any:
    if isinstance(obj, UntrustedText):
        hit = index.get(obj.source.split(" (", 1)[0])
        if hit is None:
            return obj
        risk, flags = hit
        if RISK_RANK[risk] <= RISK_RANK[obj.risk] and set(flags) <= set(obj.flags):
            return obj
        best: Risk = risk if RISK_RANK[risk] > RISK_RANK[obj.risk] else obj.risk
        return obj.model_copy(
            update={"risk": best, "flags": tuple(dict.fromkeys([*obj.flags, *flags]))}
        )
    if isinstance(obj, BaseModel):
        updates = {}
        for name in type(obj).model_fields:
            value = getattr(obj, name)
            new = _annotate(value, index)
            if new is not value:
                updates[name] = new
        return obj.model_copy(update=updates) if updates else obj
    if isinstance(obj, tuple):
        items = tuple(_annotate(v, index) for v in obj)
        return items if any(a is not b for a, b in zip(items, obj, strict=True)) else obj
    return obj
