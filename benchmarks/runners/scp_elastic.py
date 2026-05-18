"""SCPSpheresElastic core-count sweep.

Runs the `tests/SCPSpheresElastic/input` simulation at each core count in the
topology-aware sweep (1, 2, 4, 8, ..., physical, physical+virtual, plus any
explicit extras from the config). For each (np, rep) pair, captures wall-clock
time and a hash of the simulation output so we can flag runs that produced
different physics.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path

from benchmarks.runners.base import Benchmark, RunContext, RunResult, RunSpec, override, utc_now


class SCPElasticBenchmark(Benchmark):
    name = "scp_elastic"

    @override
    def specs(self, ctx: RunContext) -> Iterable[RunSpec]:
        sweep = ctx.topology.core_sweep(
            extra=ctx.config.benchmarks.scp_elastic_extra_core_counts,
        )
        warmups = ctx.config.statistics.warmup_reps
        reps = ctx.config.statistics.reps_short
        for np_count in sweep:
            for i in range(warmups + reps):
                yield RunSpec(
                    benchmark=self.name,
                    config={"np": np_count},
                    rep_index=i,
                    is_warmup=(i < warmups),
                )

    @override
    def run_one(self, spec: RunSpec, ctx: RunContext) -> RunResult:
        np_count = int(spec.config["np"])
        log_path = ctx.log_dir / f"scp_np{np_count}_rep{spec.rep_index}.log"

        input_path = ctx.alamo_dir / ctx.config.alamo.build_target
        env = os.environ.copy()
        env["OMPI_MCA_orte_report_bindings"] = "1"

        started_at = utc_now()
        t0 = time.perf_counter()
        with log_path.open("wb") as logf:
            rc = subprocess.run(
                [
                    "mpiexec",
                    "-np",
                    str(np_count),
                    "--bind-to",
                    "core",
                    "--map-by",
                    "core",
                    str(input_path),
                ],
                cwd=ctx.alamo_dir,
                env=env,
                check=False,
                stdout=logf,
                stderr=subprocess.STDOUT,
            ).returncode
        t1 = time.perf_counter()
        ended_at = utc_now()

        output_hash = _hash_output(ctx.alamo_dir / "output")

        return RunResult(
            spec=spec,
            started_at=started_at,
            ended_at=ended_at,
            wall_s=t1 - t0,
            exit_code=rc,
            status="completed" if rc == 0 else "failed",
            stdout_path=str(log_path),
            stderr_path=str(log_path),
            output_hash=output_hash,
        )


def _hash_output(output_dir: Path) -> str | None:
    """Hash a stable subset of Alamo's text output.

    We hash all `*.dat` files plus `Header` files (the small text artifacts
    that the code writes deterministically given fixed input + ranks). Binary
    plotfiles are skipped because their on-disk byte order can depend on rank
    topology even when the physics is identical. Returns None if no matching
    files exist — the runner records that without failing.
    """
    if not output_dir.exists():
        return None
    h = hashlib.sha256()
    found = False
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix != ".dat" and path.name != "Header":
            continue
        try:
            h.update(path.relative_to(output_dir).as_posix().encode())
            h.update(b"\x00")
            h.update(path.read_bytes())
            found = True
        except OSError:
            continue
    return h.hexdigest() if found else None
