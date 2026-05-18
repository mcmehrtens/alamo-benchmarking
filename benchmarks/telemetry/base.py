"""Telemetry sidecar interface and the sample shapes platform sidecars emit."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import override


@dataclass(frozen=True)
class CoreSample:
    """One per-core telemetry record. `core_type` matches the topology vocabulary:
    'super' / 'performance' / 'efficiency' on Apple Silicon, 'physical' / 'virtual'
    on Intel HT systems."""

    core_index: int
    core_type: str
    freq_mhz: float | None
    util_pct: float | None
    temp_c: float | None


def _empty_core_samples() -> tuple[CoreSample, ...]:
    return ()


@dataclass(frozen=True)
class TelemetrySample:
    """One telemetry tick. Aggregate fields populate `telemetry_sample`; the
    `per_core` tuple populates `telemetry_per_core` for the same `ts`."""

    ts: str
    cpu_freq_avg_mhz: float | None = None
    cpu_freq_max_mhz: float | None = None
    cpu_util_pct: float | None = None
    package_power_w: float | None = None
    pkg_temp_c: float | None = None
    mem_used_gb: float | None = None
    swap_used_gb: float | None = None
    load1: float | None = None
    load5: float | None = None
    load15: float | None = None
    per_core: tuple[CoreSample, ...] = field(default_factory=_empty_core_samples)


class TelemetrySidecar(ABC):
    """Sample the machine while benchmarks run.

    Lifecycle: `start(run_id)` is called once at the beginning of a run,
    `stop()` once at the end. Telemetry samples are joined to result rows by
    time range (`result.started_at`..`result.ended_at`), so the sidecar runs
    continuously across reps and cooldowns.

    Implementations must not raise on telemetry failure — log and continue.
    A dead sidecar is acceptable; a dead benchmark is not."""

    @abstractmethod
    def start(self, run_id: str) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...


class NoOpSidecar(TelemetrySidecar):
    """Telemetry that records nothing. Used when telemetry is disabled or when
    no platform sidecar is available."""

    @override
    def start(self, run_id: str) -> None:
        del run_id

    @override
    def stop(self) -> None:
        pass
