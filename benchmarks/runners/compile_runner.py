"""Alamo compile benchmarks.

Both `compile_serial` (`make -j1`) and `compile_parallel` (`make -j<physical>`)
run on a forced-cold cache. Each rep:

1. `git clean -fdx alamo/` + `git checkout -- .` to wipe build artifacts and
   any stray local edits.
2. `CCACHE_DISABLE=1 ./configure --comp=clang++`
3. `CCACHE_DISABLE=1 make -j<N>`

The wall-clock time covers configure + make together. See README "Design
decisions" for why we don't run a warm-cache variant.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path

from benchmarks.runners.base import Benchmark, RunContext, RunResult, RunSpec, override, utc_now


class _CompileBase(Benchmark):
    def _jobs(self, ctx: RunContext) -> int:
        raise NotImplementedError

    @override
    def specs(self, ctx: RunContext) -> Iterable[RunSpec]:
        warmups = ctx.config.statistics.warmup_reps
        reps = ctx.config.statistics.reps_long
        j = self._jobs(ctx)
        for i in range(warmups + reps):
            yield RunSpec(
                benchmark=self.name,
                config={"j": j, "compiler": ctx.config.alamo.compiler},
                rep_index=i,
                is_warmup=(i < warmups),
            )

    @override
    def run_one(self, spec: RunSpec, ctx: RunContext) -> RunResult:
        log_path = ctx.log_dir / f"{self.name}_rep{spec.rep_index}.log"

        started_at = utc_now()
        t0 = time.perf_counter()

        clean_err = _force_cold_cache(ctx.alamo_dir)
        if clean_err is not None:
            return RunResult(
                spec=spec,
                started_at=started_at,
                ended_at=utc_now(),
                wall_s=time.perf_counter() - t0,
                exit_code=clean_err.returncode,
                status="failed",
                notes=f"cold-cache step failed: {clean_err}",
            )

        env = _build_env()
        with log_path.open("wb") as logf:
            rc = subprocess.run(
                ["./configure", f"--comp={ctx.config.alamo.compiler}"],
                cwd=ctx.alamo_dir,
                env=env,
                check=False,
                stdout=logf,
                stderr=subprocess.STDOUT,
            ).returncode
            if rc == 0:
                j = int(spec.config["j"])
                rc = subprocess.run(
                    ["make", f"-j{j}"],
                    cwd=ctx.alamo_dir,
                    env=env,
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


class CompileSerialBenchmark(_CompileBase):
    name = "compile_serial"

    @override
    def _jobs(self, ctx: RunContext) -> int:
        del ctx
        return 1


class CompileParallelBenchmark(_CompileBase):
    name = "compile_parallel"

    @override
    def _jobs(self, ctx: RunContext) -> int:
        return ctx.topology.physical


def _force_cold_cache(alamo_dir: Path) -> subprocess.CalledProcessError | None:
    """Wipe build artifacts before a compile rep. Returns the error on failure."""
    try:
        subprocess.run(
            ["git", "-C", str(alamo_dir), "clean", "-fdx"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(alamo_dir), "checkout", "--", "."],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        return e
    return None


def _build_env() -> dict[str, str]:
    env = os.environ.copy()
    env["CCACHE_DISABLE"] = "1"
    return env
