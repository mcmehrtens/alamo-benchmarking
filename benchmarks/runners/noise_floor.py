"""Noise-floor microbenchmark.

A tight CPU-bound matmul we repeat many times to establish the per-machine,
per-night variance baseline. Confidence intervals on the real benchmarks anchor
to this sigma rather than an assumed one.
"""

from __future__ import annotations

import resource
import time
from collections.abc import Iterable

import numpy as np

from benchmarks.runners.base import (
    Benchmark,
    RunContext,
    RunResult,
    RunSpec,
    maxrss_kb,
    override,
    utc_now,
)

# Matmul size targeting ~100-300 ms per rep on both Apple Silicon (via Accelerate)
# and Xeon (via OpenBLAS / MKL). At 1500 the M1 Pro finished in ~20 ms - too short
# to characterize variance robustly against perf_counter quantization. At 4000 the
# M1 Pro hits ~250 ms and a Xeon W5-2545 should land near ~300 ms; well-suited
# for measuring per-night sigma without bloating the noise-floor budget
# (20 reps * 0.3 s ~= 6 s total).
_MATMUL_SIZE = 4000
_MATMUL_SEED = 42


class NoiseFloorBenchmark(Benchmark):
    name = "noise_floor"

    @override
    def specs(self, ctx: RunContext) -> Iterable[RunSpec]:
        n = ctx.config.statistics.reps_noise_floor
        for i in range(n):
            yield RunSpec(
                benchmark=self.name,
                config={"matmul_size": _MATMUL_SIZE},
                rep_index=i,
                is_warmup=(i == 0),
            )

    @override
    def run_one(self, spec: RunSpec, ctx: RunContext) -> RunResult:
        del ctx  # unused
        rng = np.random.default_rng(seed=_MATMUL_SEED)
        a = rng.standard_normal((_MATMUL_SIZE, _MATMUL_SIZE))

        usage_start = resource.getrusage(resource.RUSAGE_SELF)
        t0 = time.perf_counter()
        started_at = utc_now()

        c = a @ a
        sink = float(c[0, 0])

        t1 = time.perf_counter()
        ended_at = utc_now()
        usage_end = resource.getrusage(resource.RUSAGE_SELF)

        return RunResult(
            spec=spec,
            started_at=started_at,
            ended_at=ended_at,
            wall_s=t1 - t0,
            user_s=usage_end.ru_utime - usage_start.ru_utime,
            sys_s=usage_end.ru_stime - usage_start.ru_stime,
            max_rss_kb=maxrss_kb(usage_end),
            exit_code=0,
            status="completed",
            notes=f"sink={sink:.6e}",
        )
