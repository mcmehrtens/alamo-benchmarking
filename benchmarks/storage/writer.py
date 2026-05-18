"""SQLite writers.

`ResultWriter` is used by the main thread for `run`, `host`, and `result` rows.
`TelemetryWriter` is used by the telemetry sidecar; it owns its own connection
so writes can happen concurrently with main-thread writes. SQLite WAL mode keeps
both safe.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from benchmarks.storage.schema import DDL, SCHEMA_VERSION


def _connect(db_path: Path, timeout: float = 30.0) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=timeout, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class ResultWriter:
    """Main-thread writer for run / host / result rows."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn = _connect(db_path)
        self._conn.executescript(DDL)

    def write_run(
        self,
        *,
        run_id: str,
        hostname: str,
        started_at: str,
        benchmark_repo_sha: str | None,
        benchmark_repo_dirty: bool,
        alamo_repo_sha: str | None,
        alamo_repo_dirty: bool,
        config: dict[str, Any],
    ) -> None:
        self._conn.execute(
            """INSERT INTO run (
                run_id, hostname, started_at, benchmark_repo_sha,
                benchmark_repo_dirty, alamo_repo_sha, alamo_repo_dirty,
                config_json, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                hostname,
                started_at,
                benchmark_repo_sha,
                int(benchmark_repo_dirty),
                alamo_repo_sha,
                int(alamo_repo_dirty),
                json.dumps(config, default=str),
                SCHEMA_VERSION,
            ),
        )

    def finalize_run(self, run_id: str, ended_at: str) -> None:
        self._conn.execute(
            "UPDATE run SET ended_at = ? WHERE run_id = ?",
            (ended_at, run_id),
        )

    def write_host(
        self,
        *,
        run_id: str,
        os_name: str,
        os_version: str,
        kernel: str,
        arch: str,
        cpu_brand: str,
        cores_super: int,
        cores_perf: int,
        cores_eff: int,
        cores_physical: int,
        cores_virtual: int,
        topology_reason: str,
        ram_gb: float,
        fs_type: str,
        disk_free_gb: float,
        on_ac: bool | None,
        governor: str,
        perf_mode: str,
        uptime_seconds: int,
        tool_versions: dict[str, str],
        env: dict[str, str],
        preflight: dict[str, Any],
    ) -> None:
        self._conn.execute(
            """INSERT INTO host (
                run_id, os_name, os_version, kernel, arch, cpu_brand,
                cores_super, cores_perf, cores_eff, cores_physical, cores_virtual,
                topology_reason, ram_gb, fs_type, disk_free_gb,
                on_ac, governor, perf_mode, uptime_seconds,
                tool_versions_json, env_json, preflight_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                os_name,
                os_version,
                kernel,
                arch,
                cpu_brand,
                cores_super,
                cores_perf,
                cores_eff,
                cores_physical,
                cores_virtual,
                topology_reason,
                ram_gb,
                fs_type,
                disk_free_gb,
                None if on_ac is None else int(on_ac),
                governor,
                perf_mode,
                uptime_seconds,
                json.dumps(tool_versions),
                json.dumps(env),
                json.dumps(preflight, default=str),
            ),
        )

    def write_result(
        self,
        *,
        result_id: str,
        run_id: str,
        benchmark: str,
        config: dict[str, Any],
        rep_index: int,
        is_warmup: bool,
        started_at: str,
        ended_at: str | None,
        wall_s: float | None,
        user_s: float | None,
        sys_s: float | None,
        max_rss_kb: int | None,
        exit_code: int | None,
        status: str,
        stdout_path: str | None,
        stderr_path: str | None,
        output_hash: str | None,
        notes: str,
    ) -> None:
        self._conn.execute(
            """INSERT INTO result (
                result_id, run_id, benchmark, config_json, rep_index, is_warmup,
                started_at, ended_at, wall_s, user_s, sys_s, max_rss_kb,
                exit_code, status, stdout_path, stderr_path, output_hash, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result_id,
                run_id,
                benchmark,
                json.dumps(config, default=str),
                rep_index,
                int(is_warmup),
                started_at,
                ended_at,
                wall_s,
                user_s,
                sys_s,
                max_rss_kb,
                exit_code,
                status,
                stdout_path,
                stderr_path,
                output_hash,
                notes,
            ),
        )

    def close(self) -> None:
        self._conn.close()


class TelemetryWriter:
    """Independent connection used by the telemetry sidecar."""

    def __init__(self, db_path: Path) -> None:
        self._conn = _connect(db_path)

    def write_sample(
        self,
        *,
        run_id: str,
        ts: str,
        cpu_freq_avg_mhz: float | None,
        cpu_freq_max_mhz: float | None,
        cpu_util_pct: float | None,
        package_power_w: float | None,
        pkg_temp_c: float | None,
        mem_used_gb: float | None,
        swap_used_gb: float | None,
        load1: float | None,
        load5: float | None,
        load15: float | None,
    ) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO telemetry_sample (
                run_id, ts, cpu_freq_avg_mhz, cpu_freq_max_mhz, cpu_util_pct,
                package_power_w, pkg_temp_c, mem_used_gb, swap_used_gb,
                load1, load5, load15
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                ts,
                cpu_freq_avg_mhz,
                cpu_freq_max_mhz,
                cpu_util_pct,
                package_power_w,
                pkg_temp_c,
                mem_used_gb,
                swap_used_gb,
                load1,
                load5,
                load15,
            ),
        )

    def write_per_core(
        self,
        *,
        run_id: str,
        ts: str,
        core_index: int,
        core_type: str,
        freq_mhz: float | None,
        util_pct: float | None,
        temp_c: float | None,
    ) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO telemetry_per_core (
                run_id, ts, core_index, core_type, freq_mhz, util_pct, temp_c
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, ts, core_index, core_type, freq_mhz, util_pct, temp_c),
        )

    def close(self) -> None:
        self._conn.close()
