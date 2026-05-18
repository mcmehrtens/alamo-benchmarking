"""Command-line entry point for `alamo-benchmark`.

The canonical end-to-end invocation is `alamo-benchmark run` — see README.md.
Other subcommands (`preflight`, `describe`, `dry-run`) are diagnostic helpers.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks import platform_info as pinfo
from benchmarks import topology as topo_mod
from benchmarks.config import Config, load_config
from benchmarks.preflight import PreflightReport, run_preflight
from benchmarks.runners import RUNNERS, Benchmark, RunContext, RunSpec
from benchmarks.storage.writer import ResultWriter
from benchmarks.telemetry import make_sidecar

LOG = logging.getLogger("alamo-benchmark")

DEFAULT_CONFIG_PATH = Path("configs/default.toml")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging()

    cmd: str | None = getattr(args, "cmd", None)
    if cmd == "run":
        return _cmd_run(args)
    if cmd == "preflight":
        return _cmd_preflight(args)
    if cmd == "describe":
        return _cmd_describe(args)
    if cmd == "dry-run":
        return _cmd_dry_run(args)
    parser.print_help()
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alamo-benchmark",
        description=(
            "Cross-platform benchmarking suite for Alamo. "
            "Run `alamo-benchmark run` for the full overnight suite."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    p_run = sub.add_parser("run", help="Run the full benchmark suite (canonical).")
    _add_config_args(p_run)
    p_run.add_argument(
        "--mode",
        choices=["full", "quick"],
        default=None,
        help="Override the run.mode key in the config file.",
    )
    p_run.add_argument(
        "--force",
        action="store_true",
        help="Run even if pre-flight checks fail (failures still recorded).",
    )
    p_run.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Override the output directory.",
    )

    p_pre = sub.add_parser("preflight", help="Run pre-flight checks only.")
    _add_config_args(p_pre)

    sub.add_parser("describe", help="Print topology and tool versions.")

    p_dry = sub.add_parser("dry-run", help="Show what `run` would execute.")
    _add_config_args(p_dry)
    return parser


def _add_config_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config TOML (default: {DEFAULT_CONFIG_PATH}).",
    )


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.Formatter.converter = time.gmtime


# ----------------------------------------------------------- describe


def _cmd_describe(args: argparse.Namespace) -> int:
    del args
    info = pinfo.collect(Path.cwd())
    topo = topo_mod.detect_topology()
    print(f"Hostname:       {info.hostname}")
    print(f"OS:             {info.os_name} {info.os_version}  ({info.arch})")
    print(f"Kernel:         {info.kernel}")
    print(f"CPU:            {info.cpu_brand}")
    print(f"  topology:     {topo.classification_reason}")
    print(f"  physical:     {topo.physical}")
    print(f"  virtual:      {topo.virtual}")
    print(f"  sweep:        {topo.core_sweep()}")
    print(f"RAM:            {info.ram_gb} GB")
    print(f"Free disk:      {info.disk_free_gb} GB ({info.fs_type})")
    print(f"AC:             {info.on_ac}")
    print(f"Governor:       {info.governor}")
    print(f"Perf mode:      {info.perf_mode}")
    print(f"Uptime:         {info.uptime_seconds // 3600} h")
    print()
    print("Tool versions:")
    for tool, version in info.tool_versions.items():
        print(f"  {tool:<10} {version}")
    return 0


# ----------------------------------------------------------- preflight


def _cmd_preflight(args: argparse.Namespace) -> int:
    cfg = _load_and_apply(args)
    report = run_preflight(cfg.preflight, output_dir=cfg.output_dir)
    _print_preflight(report)
    return 0 if report.passed else 1


def _print_preflight(report: PreflightReport) -> None:
    print(f"Pre-flight: {'PASS' if report.passed else 'FAIL'}")
    for check in report.checks:
        marker = "OK " if check.passed else "XX"
        print(
            f"  [{marker}] {check.name:<22} {check.observed:<40} (req: {check.required}, {check.severity})"
        )


# ----------------------------------------------------------- dry-run


def _cmd_dry_run(args: argparse.Namespace) -> int:
    cfg = _load_and_apply(args)
    topo = topo_mod.detect_topology()
    info = pinfo.collect(cfg.output_dir)

    db_path, manifest_path = _plan_output_paths(cfg, info.hostname)
    print(f"Would write DB:        {db_path}")
    print(f"Would write manifest:  {manifest_path}")
    print(f"Topology:              {topo.classification_reason}")
    print(f"Core sweep:            {topo.core_sweep(cfg.benchmarks.scp_elastic_extra_core_counts)}")
    print()
    print("Enabled benchmarks (in execution order):")
    ctx = _build_context(cfg, topo, info, run_id="DRY-RUN")
    total_reps = 0
    for name in cfg.benchmarks.enabled:
        runner_cls = RUNNERS.get(name)
        if runner_cls is None:
            print(f"  ! {name}: unknown runner")
            continue
        specs = list(runner_cls().specs(ctx))
        total_reps += len(specs)
        warmup_n = sum(1 for s in specs if s.is_warmup)
        print(f"  - {name}: {len(specs)} reps ({warmup_n} warmup)")
    print(f"\nTotal reps: {total_reps}")
    return 0


# ----------------------------------------------------------- run


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = _load_and_apply(args)
    info = pinfo.collect(cfg.output_dir)
    topo = topo_mod.detect_topology()

    report = run_preflight(cfg.preflight, output_dir=cfg.output_dir)
    _print_preflight(report)
    if not report.passed and not args.force:
        LOG.error("Pre-flight failed. Use --force to override (failures still recorded).")
        return 2

    run_id = uuid.uuid4().hex
    started_at = _ts_now()
    db_path, manifest_path = _plan_output_paths(cfg, info.hostname)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    bench_sha, bench_dirty = _git_sha(Path())
    alamo_sha, alamo_dirty = _git_sha(Path("alamo"))

    ctx = _build_context(cfg, topo, info, run_id=run_id, run_dir=db_path.parent)
    ctx.log_dir.mkdir(parents=True, exist_ok=True)

    writer = ResultWriter(db_path)
    try:
        writer.write_run(
            run_id=run_id,
            hostname=info.hostname,
            started_at=started_at,
            benchmark_repo_sha=bench_sha,
            benchmark_repo_dirty=bench_dirty,
            alamo_repo_sha=alamo_sha,
            alamo_repo_dirty=alamo_dirty,
            config=cfg.raw,
        )
        writer.write_host(
            run_id=run_id,
            os_name=info.os_name,
            os_version=info.os_version,
            kernel=info.kernel,
            arch=info.arch,
            cpu_brand=info.cpu_brand,
            cores_super=topo.super_cores,
            cores_perf=topo.perf_cores,
            cores_eff=topo.eff_cores,
            cores_physical=topo.physical,
            cores_virtual=topo.virtual,
            topology_reason=topo.classification_reason,
            ram_gb=info.ram_gb,
            fs_type=info.fs_type,
            disk_free_gb=info.disk_free_gb,
            on_ac=info.on_ac,
            governor=info.governor,
            perf_mode=info.perf_mode,
            uptime_seconds=info.uptime_seconds,
            tool_versions=info.tool_versions,
            env=info.raw_env,
            preflight=report.to_dict(),
        )

        rng = random.Random(cfg.random_seed)  # noqa: S311 - not cryptographic, reproducibility seed
        sidecar = make_sidecar(cfg.telemetry, db_path)
        sidecar.start(run_id)
        try:
            for runner_name in cfg.benchmarks.enabled:
                runner_cls = RUNNERS.get(runner_name)
                if runner_cls is None:
                    LOG.warning("Skipping unknown runner: %s", runner_name)
                    continue
                runner = runner_cls()
                specs = list(runner.specs(ctx))
                rng.shuffle(specs)
                LOG.info("Starting %s with %d reps", runner_name, len(specs))
                for spec in specs:
                    _execute_spec(
                        runner, spec, ctx, writer, run_id, cfg.statistics.cooldown_seconds
                    )
        finally:
            sidecar.stop()
        writer.finalize_run(run_id, _ts_now())
    finally:
        writer.close()

    _write_manifest(
        manifest_path,
        run_id=run_id,
        started_at=started_at,
        cfg=cfg,
        info=info,
        topo=topo,
        report=report,
        bench_sha=bench_sha,
        bench_dirty=bench_dirty,
        alamo_sha=alamo_sha,
        alamo_dirty=alamo_dirty,
    )
    LOG.info("Run complete. DB: %s", db_path)
    return 0


def _execute_spec(
    runner: Benchmark,
    spec: RunSpec,
    ctx: RunContext,
    writer: ResultWriter,
    run_id: str,
    cooldown_s: float,
) -> None:
    LOG.info(
        "  %s rep=%d config=%s%s",
        spec.benchmark,
        spec.rep_index,
        spec.config,
        " (warmup)" if spec.is_warmup else "",
    )
    try:
        result = runner.run_one(spec, ctx)
    except Exception as e:
        LOG.exception("Rep failed")
        ts = _ts_now()
        writer.write_result(
            result_id=uuid.uuid4().hex,
            run_id=run_id,
            benchmark=spec.benchmark,
            config=spec.config,
            rep_index=spec.rep_index,
            is_warmup=spec.is_warmup,
            started_at=ts,
            ended_at=ts,
            wall_s=None,
            user_s=None,
            sys_s=None,
            max_rss_kb=None,
            exit_code=-1,
            status="failed",
            stdout_path=None,
            stderr_path=None,
            output_hash=None,
            notes=f"Python exception: {e!r}",
        )
        return

    writer.write_result(
        result_id=uuid.uuid4().hex,
        run_id=run_id,
        benchmark=spec.benchmark,
        config=spec.config,
        rep_index=spec.rep_index,
        is_warmup=spec.is_warmup,
        started_at=result.started_at,
        ended_at=result.ended_at,
        wall_s=result.wall_s,
        user_s=result.user_s,
        sys_s=result.sys_s,
        max_rss_kb=result.max_rss_kb,
        exit_code=result.exit_code,
        status=result.status,
        stdout_path=result.stdout_path,
        stderr_path=result.stderr_path,
        output_hash=result.output_hash,
        notes=result.notes,
    )
    if cooldown_s > 0:
        time.sleep(cooldown_s)


# ----------------------------------------------------------- helpers


def _load_and_apply(args: argparse.Namespace) -> Config:
    cfg = load_config(args.config)
    mode = getattr(args, "mode", None)
    output_dir = getattr(args, "output_dir", None)
    if mode is not None:
        cfg = replace(cfg, mode=mode)
    if output_dir is not None:
        cfg = replace(cfg, output_dir=output_dir)
    return cfg


def _build_context(
    cfg: Config,
    topo: topo_mod.Topology,
    info: pinfo.PlatformInfo,
    *,
    run_id: str,
    run_dir: Path | None = None,
) -> RunContext:
    if run_dir is None:
        run_dir = cfg.output_dir / info.hostname / f"run_{_ts_for_path(_ts_now())}"
    log_dir = run_dir / "logs"
    return RunContext(
        config=cfg,
        topology=topo,
        platform_info=info,
        run_id=run_id,
        run_dir=run_dir,
        log_dir=log_dir,
        alamo_dir=Path("alamo").resolve(),
    )


def _plan_output_paths(cfg: Config, hostname: str) -> tuple[Path, Path]:
    ts = _ts_for_path(_ts_now())
    host_dir = cfg.output_dir / hostname
    db_path = host_dir / f"run_{ts}.db"
    manifest_path = host_dir / f"run_{ts}.manifest.json"
    return db_path, manifest_path


def _ts_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _ts_for_path(ts: str) -> str:
    return ts.replace("+00:00", "Z").replace(":", "-")


def _git_sha(repo: Path) -> tuple[str | None, bool]:
    if not (repo / ".git").exists() and not (repo.parent / ".git").exists():
        return None, False
    try:
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = (
            subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            != ""
        )
        return sha, dirty
    except subprocess.CalledProcessError, FileNotFoundError:
        return None, False


def _write_manifest(
    path: Path,
    *,
    run_id: str,
    started_at: str,
    cfg: Config,
    info: pinfo.PlatformInfo,
    topo: topo_mod.Topology,
    report: PreflightReport,
    bench_sha: str | None,
    bench_dirty: bool,
    alamo_sha: str | None,
    alamo_dirty: bool,
) -> None:
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "started_at": started_at,
        "config_path": str(cfg.source_path),
        "config": cfg.raw,
        "platform": info.to_manifest(),
        "topology": topo.to_manifest(),
        "preflight": report.to_dict(),
        "git": {
            "benchmark_repo": {"sha": bench_sha, "dirty": bench_dirty},
            "alamo_repo": {"sha": alamo_sha, "dirty": alamo_dirty},
        },
    }
    path.write_text(json.dumps(manifest, indent=2, default=str))


if __name__ == "__main__":
    sys.exit(main())
