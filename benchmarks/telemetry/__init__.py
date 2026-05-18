"""Telemetry sidecars.

Each sidecar runs alongside the benchmark suite (one start/stop per run), samples
per-core frequency, package power, and temperature at the configured cadence,
and writes samples into the per-machine SQLite DB via a `TelemetryWriter`.

Platform selection:
- macOS  → `MacosSidecar` (powermetrics) when `cfg.require_sudo`.
- Linux  → `LinuxSidecar`  (turbostat) when `cfg.require_sudo`.
- Else   → `NoOpSidecar`   (telemetry disabled).
"""

from __future__ import annotations

import logging
import platform
from pathlib import Path

from benchmarks.config import TelemetryConfig
from benchmarks.telemetry.base import (
    CoreSample,
    NoOpSidecar,
    TelemetrySample,
    TelemetrySidecar,
)
from benchmarks.telemetry.linux import LinuxSidecar
from benchmarks.telemetry.macos import MacosSidecar

__all__ = [
    "CoreSample",
    "LinuxSidecar",
    "MacosSidecar",
    "NoOpSidecar",
    "TelemetrySample",
    "TelemetrySidecar",
    "make_sidecar",
]

LOG = logging.getLogger(__name__)


def make_sidecar(cfg: TelemetryConfig, db_path: Path) -> TelemetrySidecar:
    """Return the right sidecar for this platform, given the telemetry config."""
    if not cfg.require_sudo:
        LOG.info("telemetry: sudo not required, using NoOpSidecar")
        return NoOpSidecar()

    system = platform.system()
    if system == "Darwin":
        return MacosSidecar(
            db_path=db_path,
            sample_interval_seconds=cfg.sample_interval_seconds,
        )
    if system == "Linux":
        return LinuxSidecar(
            db_path=db_path,
            sample_interval_seconds=cfg.sample_interval_seconds,
        )

    LOG.warning("telemetry: unsupported platform %r, using NoOpSidecar", system)
    return NoOpSidecar()
