"""Configuration loading."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StatisticsConfig:
    warmup_reps: int
    reps_short: int
    reps_long: int
    reps_noise_floor: int
    cooldown_seconds: float


@dataclass(frozen=True)
class PreflightConfig:
    require_ac: bool
    require_governor: str
    max_load_1min: float
    min_disk_free_gb: float
    max_uptime_days: int


@dataclass(frozen=True)
class AlamoConfig:
    build_target: str
    compiler: str
    dims: tuple[int, ...]


@dataclass(frozen=True)
class TelemetryConfig:
    sample_interval_seconds: float
    require_sudo: bool


@dataclass(frozen=True)
class BenchmarksConfig:
    enabled: tuple[str, ...]
    scp_elastic_extra_core_counts: tuple[int, ...]
    scp_elastic_stop_time: str
    scp_elastic_dim: int
    regression_skip_tests: tuple[str, ...]
    render_codecs: tuple[str, ...]
    render_frame_resolution: tuple[int, int]
    render_fps: int


@dataclass(frozen=True)
class Config:
    mode: str
    output_dir: Path
    random_seed: int
    statistics: StatisticsConfig
    preflight: PreflightConfig
    alamo: AlamoConfig
    telemetry: TelemetryConfig
    benchmarks: BenchmarksConfig
    source_path: Path
    raw: dict[str, Any] = field(repr=False)


def load_config(path: Path) -> Config:
    """Load a TOML config file into a `Config` dataclass."""
    data: dict[str, Any] = tomllib.loads(path.read_text())
    run = data["run"]
    stats = data["statistics"]
    pre = data["preflight"]
    alamo = data["alamo"]
    tel = data["telemetry"]
    bench = data["benchmarks"]

    seed = int(run["random_seed"])
    if seed == 0:
        seed = int.from_bytes(os.urandom(8), "big") & 0x7FFFFFFFFFFFFFFF

    render_cfg: dict[str, Any] = bench.get("render", {})
    scp_cfg: dict[str, Any] = bench.get("scp_elastic", {})
    reg_cfg: dict[str, Any] = bench.get("regression", {})
    res_raw: list[int] = render_cfg.get("frame_resolution", [1920, 1080])

    return Config(
        mode=str(run["mode"]),
        output_dir=Path(run["output_dir"]),
        random_seed=seed,
        statistics=StatisticsConfig(
            warmup_reps=int(stats["warmup_reps"]),
            reps_short=int(stats["reps_short"]),
            reps_long=int(stats["reps_long"]),
            reps_noise_floor=int(stats["reps_noise_floor"]),
            cooldown_seconds=float(stats["cooldown_seconds"]),
        ),
        preflight=PreflightConfig(
            require_ac=bool(pre["require_ac"]),
            require_governor=str(pre["require_governor"]),
            max_load_1min=float(pre["max_load_1min"]),
            min_disk_free_gb=float(pre["min_disk_free_gb"]),
            max_uptime_days=int(pre["max_uptime_days"]),
        ),
        alamo=AlamoConfig(
            build_target=str(alamo["build_target"]),
            compiler=str(alamo["compiler"]),
            dims=tuple(int(d) for d in alamo.get("dims", [3])),
        ),
        telemetry=TelemetryConfig(
            sample_interval_seconds=float(tel["sample_interval_seconds"]),
            require_sudo=bool(tel["require_sudo"]),
        ),
        benchmarks=BenchmarksConfig(
            enabled=tuple(str(x) for x in bench["enabled"]),
            scp_elastic_extra_core_counts=tuple(
                int(x) for x in scp_cfg.get("extra_core_counts", [])
            ),
            scp_elastic_stop_time=str(scp_cfg.get("stop_time", "0.001_s")),
            scp_elastic_dim=int(scp_cfg.get("dim", 2)),
            regression_skip_tests=tuple(str(x) for x in reg_cfg.get("skip_tests", [])),
            render_codecs=tuple(
                str(x) for x in render_cfg.get("codecs", ["gifski", "av1", "h265"])
            ),
            render_frame_resolution=(int(res_raw[0]), int(res_raw[1])),
            render_fps=int(render_cfg.get("fps", 30)),
        ),
        source_path=path,
        raw=data,
    )
