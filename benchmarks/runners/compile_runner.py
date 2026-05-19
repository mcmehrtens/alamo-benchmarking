"""Alamo compile benchmarks.

Both `compile_serial` (`make -j1`) and `compile_parallel` (`make -j<physical>`)
run on a forced-cold cache. Each rep iterates the dimensions configured under
`[alamo] dims` (default `[3]`, production typically `[2, 3]` since the
regression suite needs both). Per rep:

1. `git -C alamo clean -fdx` + `git -C alamo checkout -- .` to wipe build
   artifacts AND any cloned AMReX dependency (so the timing reflects a true
   from-scratch build, including dependency fetch).
2. For each configured dim:
   a. `CCACHE_DISABLE=1 ./configure --dim=<dim> --comp=<compiler>`
   b. `CCACHE_DISABLE=1 make -j<N>`

The reported `wall_s` covers ALL dims in the rep (configure + make for each),
summed; this is what a new lab user does when they "build Alamo from scratch."
Each dim's exit code is checked individually; a failure on any dim fails the
whole rep.
"""

from __future__ import annotations

import contextlib
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
                config={
                    "j": j,
                    "compiler": ctx.config.alamo.compiler,
                    "dims": list(ctx.config.alamo.dims),
                },
                rep_index=i,
                is_warmup=(i < warmups),
            )

    @override
    def run_one(self, spec: RunSpec, ctx: RunContext) -> RunResult:
        log_path = ctx.log_dir / f"{self.name}_rep{spec.rep_index}.log"
        j = int(spec.config["j"])
        compiler = str(spec.config["compiler"])
        dims = [int(d) for d in spec.config["dims"]]

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
        rc = 0
        failing_dim: int | None = None
        with log_path.open("wb") as logf:
            for dim in dims:
                _log_step(logf, f"\n=== configure dim={dim} comp={compiler} ===\n")
                rc = subprocess.run(
                    ["./configure", f"--dim={dim}", f"--comp={compiler}"],
                    cwd=ctx.alamo_dir,
                    env=env,
                    check=False,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                ).returncode
                if rc != 0:
                    failing_dim = dim
                    break
                _log_step(logf, f"\n=== make -j{j} dim={dim} ===\n")
                rc = subprocess.run(
                    ["make", f"-j{j}"],
                    cwd=ctx.alamo_dir,
                    env=env,
                    check=False,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                ).returncode
                if rc != 0:
                    failing_dim = dim
                    break

        t1 = time.perf_counter()
        ended_at = utc_now()
        notes = "" if rc == 0 else f"failed at dim={failing_dim}"
        return RunResult(
            spec=spec,
            started_at=started_at,
            ended_at=ended_at,
            wall_s=t1 - t0,
            exit_code=rc,
            status="completed" if rc == 0 else "failed",
            stdout_path=str(log_path),
            stderr_path=str(log_path),
            notes=notes,
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
    """Wipe build artifacts + cloned dependencies before a compile rep.

    `git clean -fdx` deletes every untracked file — including gitignored ones,
    which is what we want for the cloned AMReX dir and per-dim build outputs.
    But `alamo/.venv` is also untracked-and-gitignored (it's the user-installed
    venv for `runtests.py` deps), and wiping it forces the user to recreate it
    between every compile rep AND fails the next `regression_suite` rep. We
    therefore preserve `.venv` via `-e .venv` and let the regression runner
    consume it across reps.
    """
    try:
        subprocess.run(
            ["git", "-C", str(alamo_dir), "clean", "-fdx", "-e", ".venv"],
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


def _log_step(logf: object, line: str) -> None:
    """Append a delimiter line to the rep's combined log."""
    write = getattr(logf, "write", None)
    if write is None:
        return
    with contextlib.suppress(OSError):
        write(line.encode())
