"""Telemetry sidecars.

Each sidecar runs alongside the benchmark, samples per-core frequency, package
power, temperature, memory, and load average at 1 Hz, and writes samples into
the per-machine SQLite DB via a `TelemetryWriter`.

The current implementation is a `NoOpSidecar` placeholder. Platform-specific
sidecars (`powermetrics` on macOS, `turbostat` on Linux) plug in here without
changing the runner contract. See CLAUDE.md "Telemetry parser robustness".
"""

from __future__ import annotations

import platform
from pathlib import Path

from benchmarks.config import TelemetryConfig
from benchmarks.telemetry.base import NoOpSidecar, TelemetrySidecar

__all__ = ["NoOpSidecar", "TelemetrySidecar", "make_sidecar"]


def make_sidecar(cfg: TelemetryConfig, db_path: Path) -> TelemetrySidecar:
    """Return the right sidecar for this platform.

    For now, every platform gets a `NoOpSidecar`. Real `powermetrics` /
    `turbostat` implementations will swap in here without changing callers.
    """
    _ = (cfg, db_path, platform.system())  # placeholder until real sidecars land
    return NoOpSidecar()
