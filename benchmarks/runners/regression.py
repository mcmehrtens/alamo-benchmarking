"""Alamo's full regression suite (`make test`)."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterable

from benchmarks.runners.base import Benchmark, RunContext, RunResult, RunSpec, override, utc_now


class RegressionSuiteBenchmark(Benchmark):
    name = "regression_suite"

    @override
    def specs(self, ctx: RunContext) -> Iterable[RunSpec]:
        warmups = ctx.config.statistics.warmup_reps
        reps = ctx.config.statistics.reps_long
        for i in range(warmups + reps):
            yield RunSpec(
                benchmark=self.name,
                config={},
                rep_index=i,
                is_warmup=(i < warmups),
            )

    @override
    def run_one(self, spec: RunSpec, ctx: RunContext) -> RunResult:
        log_path = ctx.log_dir / f"regression_rep{spec.rep_index}.log"
        started_at = utc_now()
        t0 = time.perf_counter()
        with log_path.open("wb") as logf:
            rc = subprocess.run(
                ["make", "test"],
                cwd=ctx.alamo_dir,
                check=False,
                stdout=logf,
                stderr=subprocess.STDOUT,
            ).returncode
        t1 = time.perf_counter()
        ended_at = utc_now()
        return RunResult(
            spec=spec,
            started_at=started_at,
            ended_at=ended_at,
            wall_s=t1 - t0,
            exit_code=rc,
            status="completed" if rc == 0 else "failed",
            stdout_path=str(log_path),
            stderr_path=str(log_path),
        )
