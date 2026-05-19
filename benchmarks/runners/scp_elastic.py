"""SCPSpheresElastic core-count sweep.

Runs the `tests/SCPSpheresElastic/input` simulation at each core count in the
topology-aware sweep (1, 2, 4, 8, ..., physical, physical+virtual, plus any
explicit extras from the config). For each (np, rep) pair, captures wall-clock
time and a SHA-256 hash of the simulation output so we can flag runs that
produced different physics.

The benchmark relies on a previously-built Alamo binary at
`alamo/bin/alamo-{dim}d-{compiler}` — produced by either a `compile_*` rep
earlier in the same run, or a manual build the user did beforehand. If no
binary is found the rep fails fast with a clear note instead of pretending to
run.
"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import time
import uuid
from collections.abc import Iterable
from pathlib import Path

from benchmarks.runners.base import Benchmark, RunContext, RunResult, RunSpec, override, utc_now

_INPUT_REL = "tests/SCPSpheresElastic/input"
_OUTPUT_BASE_REL = "tests/SCPSpheresElastic/output_bench"


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
                    config={
                        "np": np_count,
                        "stop_time": ctx.config.benchmarks.scp_elastic_stop_time,
                    },
                    rep_index=i,
                    is_warmup=(i < warmups),
                )

    @override
    def run_one(self, spec: RunSpec, ctx: RunContext) -> RunResult:
        np_count = int(spec.config["np"])
        stop_time = str(spec.config["stop_time"])
        log_path = ctx.log_dir / f"scp_np{np_count}_rep{spec.rep_index}.log"
        started_at = utc_now()
        t0 = time.perf_counter()

        binary = _find_alamo_binary(ctx.alamo_dir, ctx.config.alamo.compiler)
        if binary is None:
            return RunResult(
                spec=spec,
                started_at=started_at,
                ended_at=utc_now(),
                wall_s=time.perf_counter() - t0,
                exit_code=-1,
                status="failed",
                notes=(
                    f"no Alamo binary found under {ctx.alamo_dir}/bin/ matching "
                    f"alamo-*-{ctx.config.alamo.compiler}; build first"
                ),
            )

        rep_tag = f"np{np_count}_rep{spec.rep_index}_{uuid.uuid4().hex[:8]}"
        rep_out_rel = f"{_OUTPUT_BASE_REL}/{rep_tag}"
        rep_out_abs = ctx.alamo_dir / rep_out_rel

        env = os.environ.copy()
        env["OMPI_MCA_orte_report_bindings"] = "1"

        cmd = ["mpiexec", "-np", str(np_count)]
        # macOS OpenMPI/PRRTE cannot bind processes to cores — hwloc reports
        # no binding support on Darwin and the run aborts with exit 213. On
        # Linux affinity is reliable; ask for it explicitly so we get a
        # repeatable pinning.
        if platform.system() != "Darwin":
            cmd += ["--bind-to", "core", "--map-by", "core"]
        cmd += [
            str(binary),
            _INPUT_REL,
            f"plot_file={rep_out_rel}",
            f"stop_time={stop_time}",
        ]

        with log_path.open("wb") as logf:
            rc = subprocess.run(
                cmd,
                cwd=ctx.alamo_dir,
                env=env,
                check=False,
                stdout=logf,
                stderr=subprocess.STDOUT,
            ).returncode
        t1 = time.perf_counter()
        ended_at = utc_now()

        output_hash = _hash_output(rep_out_abs)

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


def _find_alamo_binary(alamo_dir: Path, compiler: str) -> Path | None:
    """Return the path to `alamo-*-{compiler}` under `alamo/bin/`, or None."""
    bin_dir = alamo_dir / "bin"
    if not bin_dir.is_dir():
        return None
    candidates = sorted(bin_dir.glob(f"alamo-*-{compiler}"))
    return candidates[0] if candidates else None


def _hash_output(output_dir: Path) -> str | None:
    """Compute a decomposition-invariant hash of the final plotfile's physics.

    The naïve approach of hashing on-disk bytes fails because Alamo's `Header`
    files encode the MPI box layout — np=1 and np=8 produce different headers
    for the same physics, so the hash differs across the SCP sweep and the
    "is the physics the same?" check is broken.

    We instead load the FINAL cell plotfile via yt and hash a small set of
    reductions over each field: total cell count, min, and max. These are
    exact across decompositions (every process contributes the same cells in
    aggregate; min/max are tree reductions that don't depend on order). Sum
    is intentionally omitted — floating-point summation is non-associative and
    a different reduction tree can produce slightly different totals.

    Values are quantized via `%.4e` (4 significant figures) to absorb any
    incidental FP noise. Returns None if no plotfile is present.
    """
    if not output_dir.is_dir():
        return None
    plotfiles = sorted(
        p for p in output_dir.iterdir() if p.is_dir() and p.name.endswith("cell")
    )
    if not plotfiles:
        return None
    last = plotfiles[-1]

    # Lazy import: yt is heavy and only needed at hash time, not at CLI startup.
    from yt import (  # noqa: PLC0415
        load,  # pyright: ignore[reportPrivateImportUsage]
        set_log_level,  # pyright: ignore[reportPrivateImportUsage]
    )

    # yt failures on a corrupt plotfile must never crash the benchmark rep:
    # the rep itself completed, the hash is observational only.
    try:
        set_log_level("error")
        ds = load(str(last))
        ad = ds.all_data()
    except Exception:
        return None

    h = hashlib.sha256()
    h.update(f"time={float(ds.current_time):.4e}\n".encode())
    for field in sorted(ds.field_list):
        try:
            vals = ad[field]
            line = (
                f"{field[0]}/{field[1]}: n={len(vals)} "
                f"min={float(vals.min()):.4e} max={float(vals.max()):.4e}\n"
            )
        except Exception:
            # Skip un-readable field — keep hashing the rest, mark this one absent
            # so a partial result still distinguishes from a clean run.
            line = f"{field[0]}/{field[1]}: UNREADABLE\n"
        h.update(line.encode())
    return h.hexdigest()
