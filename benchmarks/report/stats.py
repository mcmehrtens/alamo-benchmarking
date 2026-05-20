"""Summary-statistic helpers for the report.

CLAUDE.md is explicit: no mean-only summaries anywhere. Every table built
through this module reports median + IQR + min/max (+ stdev when n >= 2).
The "mean" key is intentionally absent.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Summary:
    """Robust per-distribution summary."""

    n: int
    median: float | None
    q1: float | None
    q3: float | None
    iqr: float | None
    minimum: float | None
    maximum: float | None
    stdev: float | None  # sample stdev; only defined for n >= 2

    @property
    def has_data(self) -> bool:
        return self.n > 0


def summarize(values: Sequence[float | None]) -> Summary:
    """Compute median/IQR/min/max/stdev over a sequence of floats.

    NaN and None inputs are filtered out before statistics are computed.
    """
    clean = [float(v) for v in values if v is not None and not _is_nan(v)]
    n = len(clean)
    if n == 0:
        return Summary(0, None, None, None, None, None, None, None)
    if n == 1:
        only = clean[0]
        return Summary(1, only, only, only, 0.0, only, only, None)
    quartiles = statistics.quantiles(clean, n=4, method="inclusive")
    q1, _q2, q3 = quartiles
    return Summary(
        n=n,
        median=statistics.median(clean),
        q1=q1,
        q3=q3,
        iqr=q3 - q1,
        minimum=min(clean),
        maximum=max(clean),
        stdev=statistics.stdev(clean),
    )


def percentile(values: Sequence[float | None], pct: float) -> float | None:
    """Return the linear-interpolated percentile of ``values`` (pct in 0..100).

    Returns ``None`` if the sequence is empty after filtering NaNs.
    """
    clean = sorted(float(v) for v in values if v is not None and not _is_nan(v))
    n = len(clean)
    if n == 0:
        return None
    if n == 1:
        return clean[0]
    rank = (pct / 100.0) * (n - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return clean[lo]
    frac = rank - lo
    return clean[lo] + frac * (clean[hi] - clean[lo])


def _is_nan(x: float | None) -> bool:
    return isinstance(x, float) and math.isnan(x)
