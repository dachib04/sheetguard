"""Statistics fixed in advance (docs/experiment-plan.md section 7). Standard library only."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """95% Wilson score interval for k successes out of n."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar test on the discordant counts b and c."""
    n = b + c
    if n == 0:
        return 1.0
    tail: float = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2**n
    return min(1.0, 2 * tail)


def holm(pvalues: Sequence[float]) -> list[float]:
    """Holm-Bonferroni adjusted p-values, in the input order."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    adjusted = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, (m - rank) * pvalues[i])
        adjusted[i] = min(1.0, running)
    return adjusted


def paired_diff_ci(
    x: Sequence[bool], y: Sequence[bool], resamples: int = 10_000, seed: int = 0
) -> tuple[float, float, float]:
    """Rate(x) - rate(y) on paired per-file outcomes, with a 95% percentile bootstrap CI."""
    if len(x) != len(y):
        raise ValueError("paired samples must have equal length")
    n = len(x)
    if n == 0:
        return (0.0, -1.0, 1.0)
    diffs = [int(a) - int(b) for a, b in zip(x, y, strict=True)]
    point = sum(diffs) / n
    rng = random.Random(seed)
    boots = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples))
    return (point, boots[int(0.025 * resamples)], boots[int(0.975 * resamples) - 1])


def majority(values: Sequence[bool]) -> bool:
    """True when more than half of the repeats are True (2 of 3)."""
    return sum(values) * 2 > len(values)
