"""SQLite schema. Bump SCHEMA_VERSION on any breaking change."""

from __future__ import annotations

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS run (
    run_id TEXT PRIMARY KEY,
    hostname TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    benchmark_repo_sha TEXT,
    benchmark_repo_dirty INTEGER,
    alamo_repo_sha TEXT,
    alamo_repo_dirty INTEGER,
    config_json TEXT NOT NULL,
    schema_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS host (
    run_id TEXT PRIMARY KEY,
    os_name TEXT,
    os_version TEXT,
    kernel TEXT,
    arch TEXT,
    cpu_brand TEXT,
    cores_super INTEGER,
    cores_perf INTEGER,
    cores_eff INTEGER,
    cores_physical INTEGER,
    cores_virtual INTEGER,
    topology_reason TEXT,
    ram_gb REAL,
    fs_type TEXT,
    disk_free_gb REAL,
    on_ac INTEGER,
    governor TEXT,
    perf_mode TEXT,
    uptime_seconds INTEGER,
    tool_versions_json TEXT,
    env_json TEXT,
    preflight_json TEXT,
    FOREIGN KEY (run_id) REFERENCES run(run_id)
);

CREATE TABLE IF NOT EXISTS result (
    result_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    benchmark TEXT NOT NULL,
    config_json TEXT NOT NULL,
    rep_index INTEGER NOT NULL,
    is_warmup INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    wall_s REAL,
    user_s REAL,
    sys_s REAL,
    max_rss_kb INTEGER,
    exit_code INTEGER,
    status TEXT NOT NULL,
    stdout_path TEXT,
    stderr_path TEXT,
    output_hash TEXT,
    notes TEXT,
    FOREIGN KEY (run_id) REFERENCES run(run_id)
);

CREATE TABLE IF NOT EXISTS telemetry_sample (
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    cpu_freq_avg_mhz REAL,
    cpu_freq_max_mhz REAL,
    cpu_util_pct REAL,
    package_power_w REAL,
    pkg_temp_c REAL,
    mem_used_gb REAL,
    swap_used_gb REAL,
    load1 REAL,
    load5 REAL,
    load15 REAL,
    PRIMARY KEY (run_id, ts)
);

CREATE TABLE IF NOT EXISTS telemetry_per_core (
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    core_index INTEGER NOT NULL,
    core_type TEXT NOT NULL,
    freq_mhz REAL,
    util_pct REAL,
    temp_c REAL,
    PRIMARY KEY (run_id, ts, core_index)
);

CREATE INDEX IF NOT EXISTS idx_result_run_benchmark
    ON result(run_id, benchmark);
CREATE INDEX IF NOT EXISTS idx_telem_sample_run_ts
    ON telemetry_sample(run_id, ts);
CREATE INDEX IF NOT EXISTS idx_telem_core_run_ts
    ON telemetry_per_core(run_id, ts);
"""
