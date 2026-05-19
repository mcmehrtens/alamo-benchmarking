"""Benchmark runner registry."""

from __future__ import annotations

from benchmarks.runners.base import Benchmark, RunContext, RunResult, RunSpec
from benchmarks.runners.compile_runner import (
    CompileParallelBenchmark,
    CompileSerialBenchmark,
)
from benchmarks.runners.noise_floor import NoiseFloorBenchmark
from benchmarks.runners.regression import RegressionSuiteBenchmark
from benchmarks.runners.render import RenderEncodeBenchmark, RenderFramesBenchmark
from benchmarks.runners.scp_elastic import SCPElasticBenchmark

RUNNERS: dict[str, type[Benchmark]] = {
    NoiseFloorBenchmark.name: NoiseFloorBenchmark,
    CompileSerialBenchmark.name: CompileSerialBenchmark,
    CompileParallelBenchmark.name: CompileParallelBenchmark,
    RegressionSuiteBenchmark.name: RegressionSuiteBenchmark,
    SCPElasticBenchmark.name: SCPElasticBenchmark,
    RenderFramesBenchmark.name: RenderFramesBenchmark,
    RenderEncodeBenchmark.name: RenderEncodeBenchmark,
}

__all__ = [
    "RUNNERS",
    "Benchmark",
    "RunContext",
    "RunResult",
    "RunSpec",
]
