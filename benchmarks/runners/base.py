"""Benchmark runner contract.

Each benchmark is a subclass of `Benchmark` that emits a sequence of `RunSpec`
objects (one per (config, rep)) and knows how to execute each spec, returning a
`RunResult`.
"""

from __future__ import annotations

import resource
import sys
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, override

from benchmarks.config import Config
from benchmarks.platform_info import PlatformInfo
from benchmarks.topology import Topology

__all__ = [
    "Benchmark",
    "RunContext",
    "RunResult",
    "RunSpec",
    "maxrss_kb",
    "override",
    "utc_now",
]


@dataclass(frozen=True)
class RunSpec:
    """A single (benchmark, config, rep) to execute."""

    benchmark: str
    config: dict[str, Any]
    rep_index: int
    is_warmup: bool


@dataclass
class RunResult:
    """Outcome of a single `run_one` call."""

    spec: RunSpec
    started_at: str
    ended_at: str
    wall_s: float
    user_s: float | None = None
    sys_s: float | None = None
    max_rss_kb: int | None = None
    exit_code: int = 0
    status: str = "completed"  # 'completed' | 'failed' | 'aborted'
    stdout_path: str | None = None
    stderr_path: str | None = None
    output_hash: str | None = None
    notes: str = ""


@dataclass(frozen=True)
class RunContext:
    """Everything a runner needs to know about this invocation."""

    config: Config
    topology: Topology
    platform_info: PlatformInfo
    run_id: str
    run_dir: Path
    log_dir: Path
    alamo_dir: Path


class Benchmark(ABC):
    """Base class for all benchmarks."""

    name: str = ""

    @abstractmethod
    def specs(self, ctx: RunContext) -> Iterable[RunSpec]: ...

    @abstractmethod
    def run_one(self, spec: RunSpec, ctx: RunContext) -> RunResult: ...


def utc_now() -> str:
    """ISO-8601 UTC timestamp with microsecond precision."""
    return datetime.now(UTC).isoformat(timespec="microseconds")


def maxrss_kb(usage: resource.struct_rusage) -> int:
    """Normalize `ru_maxrss` to kilobytes across platforms.

    macOS reports it in bytes; Linux reports it in kibibytes.
    """
    raw = int(usage.ru_maxrss)
    if sys.platform == "darwin":
        return raw // 1024
    return raw
