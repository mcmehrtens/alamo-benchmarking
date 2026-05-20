"""Geekbench-vs-SCP correlation: loader, log-log fit, prediction.

Geekbench 6 scores for lab machines live in ``benchmarks/report/geekbench.toml``
(operator-supplied; same source the report renders from). For every machine
that has BOTH a measured SCP optimal-np wall time AND a Geekbench score, we
run a log-log linear regression separately on single-core and multi-core
scores; the resulting fits let us predict an SCP wall for any other machine
given only its Geekbench numbers.

Why log-log: wall time is expected to scale roughly inversely with throughput
(wall ~ k / score). In log space that becomes log(wall) = -1 * log(score) + c,
a line — easy to fit, easy to read off, easy to flag when reality diverges
(the fitted slope tells us how this particular workload responds to the
Geekbench-y throughput axis).
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MachineScore:
    machine_id: str
    cpu_label: str
    single_core: float | None
    multi_core: float | None


@dataclass(frozen=True)
class Prospective:
    slug: str
    cpu_label: str
    single_core: float | None
    multi_core: float | None
    notes: str


@dataclass(frozen=True)
class GeekbenchData:
    scores: dict[str, MachineScore]
    prospective: list[Prospective]


@dataclass(frozen=True)
class LogLogFit:
    """Result of fitting log(wall) = slope * log(score) + intercept."""

    slope: float
    intercept: float
    r_squared: float
    n: int
    score_min: float
    score_max: float

    def predict(self, score: float) -> float:
        return math.exp(self.slope * math.log(score) + self.intercept)


def load_geekbench(path: Path) -> GeekbenchData:
    if not path.exists():
        return GeekbenchData(scores={}, prospective=[])
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    scores: dict[str, MachineScore] = {}
    for machine_id, body in (raw.get("scores") or {}).items():
        scores[machine_id] = MachineScore(
            machine_id=machine_id,
            cpu_label=body.get("cpu_label", machine_id),
            single_core=_as_float(body.get("single_core")),
            multi_core=_as_float(body.get("multi_core")),
        )
    prospective: list[Prospective] = []
    for slug, body in (raw.get("prospective") or {}).items():
        prospective.append(
            Prospective(
                slug=slug,
                cpu_label=body.get("cpu_label", slug),
                single_core=_as_float(body.get("single_core")),
                multi_core=_as_float(body.get("multi_core")),
                notes=body.get("notes", ""),
            )
        )
    return GeekbenchData(scores=scores, prospective=prospective)


def _as_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def fit_loglog(scores: list[float], walls: list[float]) -> LogLogFit | None:
    """Least-squares log-log linear fit. Returns None if n < 2."""
    if len(scores) != len(walls) or len(scores) < 2:
        return None
    if any(s <= 0 for s in scores) or any(w <= 0 for w in walls):
        return None
    n = len(scores)
    lx = [math.log(s) for s in scores]
    ly = [math.log(w) for w in walls]
    mean_x = sum(lx) / n
    mean_y = sum(ly) / n
    num = sum((lx[i] - mean_x) * (ly[i] - mean_y) for i in range(n))
    den = sum((lx[i] - mean_x) ** 2 for i in range(n))
    if den == 0:
        return None
    slope = num / den
    intercept = mean_y - slope * mean_x
    # R^2 in log space
    ss_tot = sum((ly[i] - mean_y) ** 2 for i in range(n))
    ss_res = sum((ly[i] - (slope * lx[i] + intercept)) ** 2 for i in range(n))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return LogLogFit(
        slope=slope,
        intercept=intercept,
        r_squared=r2,
        n=n,
        score_min=min(scores),
        score_max=max(scores),
    )
