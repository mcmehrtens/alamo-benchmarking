"""SQLite → typed in-memory data structures for the report.

We deliberately avoid pandas; the project's other reads (`writer.py`) use
stdlib ``sqlite3`` and we want to keep dependencies aligned.

All timestamps in this module are parsed into timezone-aware
``datetime.datetime`` objects in UTC. The ``telemetry_offset_seconds`` field
on :class:`RunBundle` carries the per-machine offset that was applied to
align telemetry samples to result-table wall-clock; see
:func:`_detect_telemetry_offset` for the heuristic.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from benchmarks.report.discover import DiscoveredRun


@dataclass(frozen=True)
class RunMetadata:
    run_id: str
    machine_id: str
    hostname: str
    started_at: datetime
    ended_at: datetime | None
    benchmark_repo_sha: str | None
    benchmark_repo_dirty: bool
    alamo_repo_sha: str | None
    alamo_repo_dirty: bool
    schema_version: int
    config: dict[str, Any]


@dataclass(frozen=True)
class HostInfo:
    os_name: str
    os_version: str
    kernel: str
    arch: str
    cpu_brand: str
    cores_super: int
    cores_perf: int
    cores_eff: int
    cores_physical: int
    cores_virtual: int
    topology_reason: str
    ram_gb: float
    fs_type: str
    disk_free_gb: float
    on_ac: bool | None
    governor: str
    perf_mode: str
    uptime_seconds: int
    tool_versions: dict[str, str]
    preflight: dict[str, Any]


@dataclass(frozen=True)
class ResultRow:
    result_id: str
    benchmark: str
    config: dict[str, Any]
    config_key: str  # JSON-serialized canonical form (for grouping)
    rep_index: int
    is_warmup: bool
    started_at: datetime
    ended_at: datetime | None
    wall_s: float | None
    user_s: float | None
    sys_s: float | None
    max_rss_kb: int | None
    exit_code: int | None
    status: str
    output_hash: str | None
    notes: str


@dataclass(frozen=True)
class TelemetrySample:
    ts: datetime
    cpu_freq_avg_mhz: float | None
    cpu_freq_max_mhz: float | None
    cpu_util_pct: float | None
    package_power_w: float | None
    pkg_temp_c: float | None
    mem_used_gb: float | None
    swap_used_gb: float | None
    load1: float | None
    load5: float | None
    load15: float | None


@dataclass(frozen=True)
class PerCoreSample:
    ts: datetime
    core_index: int
    core_type: str
    freq_mhz: float | None
    util_pct: float | None
    temp_c: float | None


@dataclass
class RunBundle:
    """Everything we need from one machine's DB, normalized."""

    discovered: DiscoveredRun
    run: RunMetadata
    host: HostInfo
    results: list[ResultRow]
    telemetry: list[TelemetrySample]
    per_core: list[PerCoreSample]
    telemetry_offset_seconds: float = 0.0
    db_bytes: int = 0
    telemetry_channels_available: dict[str, bool] = field(default_factory=dict)


def load_run(disc: DiscoveredRun) -> RunBundle:
    """Open a per-machine DB and pull every row into typed dataclasses."""
    conn = sqlite3.connect(f"file:{disc.db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        run = _load_run_meta(conn)
        host = _load_host(conn)
        results = _load_results(conn)
        offset = _detect_telemetry_offset(conn, results)
        telemetry = _load_telemetry(conn, offset)
        per_core = _load_per_core(conn, offset)
        channels = _telemetry_channels(telemetry, per_core)
    finally:
        conn.close()
    return RunBundle(
        discovered=disc,
        run=run,
        host=host,
        results=results,
        telemetry=telemetry,
        per_core=per_core,
        telemetry_offset_seconds=offset,
        db_bytes=disc.db_path.stat().st_size,
        telemetry_channels_available=channels,
    )


def _load_run_meta(conn: sqlite3.Connection) -> RunMetadata:
    row = conn.execute("SELECT * FROM run").fetchone()
    return RunMetadata(
        run_id=row["run_id"],
        machine_id=row["machine_id"],
        hostname=row["hostname"],
        started_at=_parse_ts(row["started_at"]),
        ended_at=_parse_ts_or_none(row["ended_at"]),
        benchmark_repo_sha=row["benchmark_repo_sha"],
        benchmark_repo_dirty=bool(row["benchmark_repo_dirty"]),
        alamo_repo_sha=row["alamo_repo_sha"],
        alamo_repo_dirty=bool(row["alamo_repo_dirty"]),
        schema_version=int(row["schema_version"]),
        config=json.loads(row["config_json"]),
    )


def _load_host(conn: sqlite3.Connection) -> HostInfo:
    row = conn.execute("SELECT * FROM host").fetchone()
    return HostInfo(
        os_name=row["os_name"] or "",
        os_version=row["os_version"] or "",
        kernel=row["kernel"] or "",
        arch=row["arch"] or "",
        cpu_brand=row["cpu_brand"] or "",
        cores_super=int(row["cores_super"] or 0),
        cores_perf=int(row["cores_perf"] or 0),
        cores_eff=int(row["cores_eff"] or 0),
        cores_physical=int(row["cores_physical"] or 0),
        cores_virtual=int(row["cores_virtual"] or 0),
        topology_reason=row["topology_reason"] or "",
        ram_gb=float(row["ram_gb"] or 0),
        fs_type=row["fs_type"] or "",
        disk_free_gb=float(row["disk_free_gb"] or 0),
        on_ac=None if row["on_ac"] is None else bool(row["on_ac"]),
        governor=row["governor"] or "",
        perf_mode=row["perf_mode"] or "",
        uptime_seconds=int(row["uptime_seconds"] or 0),
        tool_versions=json.loads(row["tool_versions_json"] or "{}"),
        preflight=json.loads(row["preflight_json"] or "{}"),
    )


def _load_results(conn: sqlite3.Connection) -> list[ResultRow]:
    rows = conn.execute(
        """SELECT result_id, benchmark, config_json, rep_index, is_warmup,
                  started_at, ended_at, wall_s, user_s, sys_s, max_rss_kb,
                  exit_code, status, output_hash, notes
           FROM result
           ORDER BY started_at"""
    ).fetchall()
    out: list[ResultRow] = []
    for r in rows:
        cfg = json.loads(r["config_json"])
        out.append(
            ResultRow(
                result_id=r["result_id"],
                benchmark=r["benchmark"],
                config=cfg,
                config_key=json.dumps(cfg, sort_keys=True),
                rep_index=int(r["rep_index"]),
                is_warmup=bool(r["is_warmup"]),
                started_at=_parse_ts(r["started_at"]),
                ended_at=_parse_ts_or_none(r["ended_at"]),
                wall_s=_float_or_none(r["wall_s"]),
                user_s=_float_or_none(r["user_s"]),
                sys_s=_float_or_none(r["sys_s"]),
                max_rss_kb=_int_or_none(r["max_rss_kb"]),
                exit_code=_int_or_none(r["exit_code"]),
                status=r["status"],
                output_hash=r["output_hash"],
                notes=r["notes"] or "",
            )
        )
    return out


def _load_telemetry(conn: sqlite3.Connection, offset_seconds: float) -> list[TelemetrySample]:
    rows = conn.execute(
        """SELECT ts, cpu_freq_avg_mhz, cpu_freq_max_mhz, cpu_util_pct,
                  package_power_w, pkg_temp_c, mem_used_gb, swap_used_gb,
                  load1, load5, load15
           FROM telemetry_sample
           ORDER BY ts"""
    ).fetchall()
    shift = timedelta(seconds=offset_seconds)
    return [
        TelemetrySample(
            ts=_parse_ts(r["ts"]) - shift,
            cpu_freq_avg_mhz=_float_or_none(r["cpu_freq_avg_mhz"]),
            cpu_freq_max_mhz=_float_or_none(r["cpu_freq_max_mhz"]),
            cpu_util_pct=_float_or_none(r["cpu_util_pct"]),
            package_power_w=_float_or_none(r["package_power_w"]),
            pkg_temp_c=_float_or_none(r["pkg_temp_c"]),
            mem_used_gb=_float_or_none(r["mem_used_gb"]),
            swap_used_gb=_float_or_none(r["swap_used_gb"]),
            load1=_float_or_none(r["load1"]),
            load5=_float_or_none(r["load5"]),
            load15=_float_or_none(r["load15"]),
        )
        for r in rows
    ]


def _load_per_core(conn: sqlite3.Connection, offset_seconds: float) -> list[PerCoreSample]:
    rows = conn.execute(
        """SELECT ts, core_index, core_type, freq_mhz, util_pct, temp_c
           FROM telemetry_per_core
           ORDER BY ts, core_index"""
    ).fetchall()
    shift = timedelta(seconds=offset_seconds)
    return [
        PerCoreSample(
            ts=_parse_ts(r["ts"]) - shift,
            core_index=int(r["core_index"]),
            core_type=r["core_type"],
            freq_mhz=_float_or_none(r["freq_mhz"]),
            util_pct=_float_or_none(r["util_pct"]),
            temp_c=_float_or_none(r["temp_c"]),
        )
        for r in rows
    ]


def _detect_telemetry_offset(conn: sqlite3.Connection, results: list[ResultRow]) -> float:
    """Return how many seconds to subtract from telemetry ts to align with results.

    The powermetrics parser on macOS emits local-time labels marked as UTC, so
    on macOS hosts telemetry ts is ~5 h ahead of the result-table started_at.
    On Linux (turbostat) the two are aligned to within seconds. We compute the
    offset from the *first* row of each table and round to the nearest minute
    — clock differences ≤ 30 s are noise and we ignore them.
    """
    if not results:
        return 0.0
    first_telem = conn.execute(
        "SELECT ts FROM telemetry_sample ORDER BY ts LIMIT 1"
    ).fetchone()
    if first_telem is None:
        return 0.0
    first_result_ts = min(r.started_at for r in results)
    delta = (_parse_ts(first_telem["ts"]) - first_result_ts).total_seconds()
    if abs(delta) < 60.0:
        return 0.0
    return round(delta / 60.0) * 60.0


def _telemetry_channels(
    samples: list[TelemetrySample], per_core: list[PerCoreSample]
) -> dict[str, bool]:
    """Per-channel availability flags, used by plot routines to skip empties."""
    return {
        "cpu_freq_avg_mhz": any(s.cpu_freq_avg_mhz is not None for s in samples),
        "cpu_freq_max_mhz": any(s.cpu_freq_max_mhz is not None for s in samples),
        "cpu_util_pct": any(s.cpu_util_pct is not None for s in samples),
        "package_power_w": any(s.package_power_w is not None for s in samples),
        "pkg_temp_c": any(s.pkg_temp_c is not None for s in samples),
        "mem_used_gb": any(s.mem_used_gb is not None for s in samples),
        "swap_used_gb": any(s.swap_used_gb is not None for s in samples),
        "load1": any(s.load1 is not None for s in samples),
        "per_core_freq_mhz": any(c.freq_mhz is not None for c in per_core),
        "per_core_util_pct": any(c.util_pct is not None for c in per_core),
        "per_core_temp_c": any(c.temp_c is not None for c in per_core),
    }


def _parse_ts(value: str) -> datetime:
    """Parse one of our ISO-8601 timestamps to an aware UTC datetime."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_ts_or_none(value: str | None) -> datetime | None:
    return None if value is None else _parse_ts(value)


def _float_or_none(value: float | int | str | None) -> float | None:
    return None if value is None else float(value)


def _int_or_none(value: int | str | None) -> int | None:
    return None if value is None else int(value)


def discovered_dbs_to_bundles(discovered: list[DiscoveredRun]) -> list[RunBundle]:
    return [load_run(d) for d in discovered]


def benchmark_window(
    bundle: RunBundle, benchmark: str, *, include_warmup: bool = False
) -> tuple[datetime, datetime] | None:
    """Earliest-start / latest-end across all reps of one benchmark."""
    rows = [
        r
        for r in bundle.results
        if r.benchmark == benchmark and (include_warmup or not r.is_warmup) and r.ended_at
    ]
    if not rows:
        return None
    return min(r.started_at for r in rows), max(r.ended_at or r.started_at for r in rows)


def measured_results(bundle: RunBundle, benchmark: str) -> list[ResultRow]:
    return [r for r in bundle.results if r.benchmark == benchmark and not r.is_warmup]


def all_results(bundle: RunBundle, benchmark: str) -> list[ResultRow]:
    return [r for r in bundle.results if r.benchmark == benchmark]


def manifest_path_relative(bundle: RunBundle, root: Path) -> str:
    try:
        return str(bundle.discovered.run_dir.relative_to(root.resolve()))
    except ValueError:
        return str(bundle.discovered.run_dir)


def optimal_scp_config(bundle: RunBundle) -> tuple[int, float, list[ResultRow]] | None:
    """For one machine: the np that produced the lowest median SCP wall time.

    Returns ``(optimal_np, median_wall_s, reps_at_optimal_np)`` or ``None`` if
    the machine has no measured SCP data. The "optimal" tag means
    wall-time-minimal, not necessarily the most parallel-efficient or the
    most CPU-saturating — those are different metrics, called out separately
    in the report. Median over the np's measured reps; ties broken by the
    smallest ``np`` (favors the simpler config).
    """
    by_np: dict[int, list[ResultRow]] = {}
    for r in measured_results(bundle, "scp_elastic"):
        if r.wall_s is None:
            continue
        by_np.setdefault(int(r.config.get("np", 0)), []).append(r)
    if not by_np:
        return None
    scored: list[tuple[float, int, list[ResultRow]]] = []
    for n, rows in by_np.items():
        walls = [r.wall_s for r in rows if r.wall_s is not None]
        if not walls:
            continue
        scored.append((statistics.median(walls), n, rows))
    if not scored:
        return None
    scored.sort(key=lambda t: (t[0], t[1]))
    median_wall, n, rows = scored[0]
    return n, median_wall, rows


def order_by_scp_optimal(bundles: list[RunBundle]) -> list[RunBundle]:
    """Sort bundles worst -> best by SCP optimal-np median wall (left = slowest).

    Machines with no SCP data sort to the far left (treated as +infinity).
    Used as the canonical machine order throughout the report so the reader
    sees the same machine in the same position in every figure and table.
    """

    def key(b: RunBundle) -> float:
        opt = optimal_scp_config(b)
        if opt is None:
            return float("inf")
        return opt[1]

    return sorted(bundles, key=key, reverse=True)


__all__ = [
    "HostInfo",
    "PerCoreSample",
    "ResultRow",
    "RunBundle",
    "RunMetadata",
    "TelemetrySample",
    "all_results",
    "benchmark_window",
    "discovered_dbs_to_bundles",
    "load_run",
    "manifest_path_relative",
    "measured_results",
    "optimal_scp_config",
    "order_by_scp_optimal",
]
