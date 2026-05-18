"""Telemetry sidecar interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import override


class TelemetrySidecar(ABC):
    """Sample the machine while a benchmark runs.

    Lifecycle: `start(run_id)` is called immediately before the benchmark's
    `run_one`, `stop()` immediately after. The sidecar must tolerate being
    started/stopped many times during a single program run.

    Implementations must not raise on telemetry failure — log and continue.
    A dead sidecar is fine; a dead benchmark is not.
    """

    @abstractmethod
    def start(self, run_id: str) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...


class NoOpSidecar(TelemetrySidecar):
    """Telemetry that records nothing. Placeholder until platform sidecars land."""

    @override
    def start(self, run_id: str) -> None:
        del run_id

    @override
    def stop(self) -> None:
        pass
