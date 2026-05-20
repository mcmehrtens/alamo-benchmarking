"""Smoke + unit tests for the report package.

The full end-to-end report build is integration-tested by running
``alamo-benchmark report`` on the committed result DBs in CI / local
checkout. Here we cover the small pure-Python pieces that we'd want to
catch regressing without depending on the recorded data being present.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from benchmarks.report.data import load_run
from benchmarks.report.discover import DiscoveredRun, discover_runs
from benchmarks.report.stats import percentile, summarize
from benchmarks.storage.schema import DDL


def test_summarize_empty():
    s = summarize([])
    assert s.n == 0
    assert s.median is None
    assert s.iqr is None
    assert s.stdev is None


def test_summarize_single_value():
    s = summarize([3.0])
    assert s.n == 1
    assert s.median == 3.0
    assert s.q1 == 3.0
    assert s.q3 == 3.0
    assert s.iqr == 0.0
    assert s.minimum == 3.0
    assert s.maximum == 3.0
    assert s.stdev is None  # undefined for n=1


def test_summarize_filters_nans_and_nones():
    s = summarize([1.0, float("nan"), 2.0, None, 3.0])  # type: ignore[list-item]
    assert s.n == 3
    assert s.median == 2.0
    assert s.minimum == 1.0
    assert s.maximum == 3.0


def test_summarize_median_iqr_simple():
    s = summarize([10.0, 20.0, 30.0, 40.0, 50.0])
    assert s.median == 30.0
    assert s.q1 == 20.0
    assert s.q3 == 40.0
    assert s.iqr == 20.0


def test_percentile_empty():
    assert percentile([], 50) is None


def test_percentile_single():
    assert percentile([4.2], 99) == 4.2


def test_percentile_interpolation():
    # p95 of 0..100 in steps of 1 lands between 95 and 96 (rank 95.95)
    vals = [float(i) for i in range(101)]
    p = percentile(vals, 95)
    assert p is not None
    assert 95.0 <= p <= 96.0


def test_discover_skips_machine_dir_without_runs(tmp_path: Path):
    (tmp_path / "no_runs_here").mkdir()
    assert discover_runs(tmp_path) == []


def test_discover_skips_run_dir_without_db(tmp_path: Path):
    (tmp_path / "m1" / "run_2026-05-19T00-00-00Z").mkdir(parents=True)
    assert discover_runs(tmp_path) == []


def test_discover_picks_latest_run(tmp_path: Path):
    machine = tmp_path / "m1"
    older = machine / "run_2026-05-18T00-00-00Z"
    older.mkdir(parents=True)
    (older / "x.db").write_bytes(b"placeholder")
    newer = machine / "run_2026-05-19T12-30-00Z"
    newer.mkdir(parents=True)
    (newer / "y.db").write_bytes(b"placeholder")
    runs = discover_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0].machine_id == "m1"
    assert runs[0].run_dir == newer


def _seed_test_db(path: Path, *, machine_id: str, telem_offset_seconds: int = 0) -> None:
    """Insert just enough rows to exercise ``load_run`` and offset detection."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(DDL)
        run_id = "test-run"
        started = datetime(2026, 5, 19, 22, 0, 0, tzinfo=UTC)
        ended = started + timedelta(hours=1)
        conn.execute(
            "INSERT INTO run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                machine_id,
                "test-host",
                started.isoformat(),
                ended.isoformat(),
                "abc123",
                0,
                "def456",
                0,
                '{"run": {"mode": "full"}, "benchmarks": {"regression": {"skip_tests": []}}}',
                2,
            ),
        )
        conn.execute(
            "INSERT INTO host VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                "Linux",
                "Linux 6.0",
                "6.0.0-generic",
                "x86_64",
                "Intel Test",
                0,
                4,
                0,
                4,
                4,
                "test",
                32.0,
                "ext4",
                100.0,
                1,
                "performance",
                "n/a",
                3600,
                "{}",
                "{}",
                '{"passed": true, "checks": []}',
            ),
        )
        rep_start = started + timedelta(seconds=1)
        rep_end = rep_start + timedelta(seconds=2)
        conn.execute(
            "INSERT INTO result VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "r1",
                run_id,
                "noise_floor",
                '{"matmul_size": 100}',
                0,
                0,
                rep_start.isoformat(),
                rep_end.isoformat(),
                0.5,
                None,
                None,
                None,
                0,
                "completed",
                None,
                None,
                None,
                "",
            ),
        )
        # Drop a single telemetry sample, offset if requested
        telem_ts = started + timedelta(seconds=2 + telem_offset_seconds)
        conn.execute(
            "INSERT INTO telemetry_sample (run_id, ts, cpu_freq_avg_mhz) VALUES (?, ?, ?)",
            (run_id, telem_ts.isoformat(), 3500.0),
        )
        conn.commit()
    finally:
        conn.close()


def test_load_run_offset_zero_for_aligned_timestamps(tmp_path: Path):
    db = tmp_path / "run_2026-05-19T22-00-00Z" / "run.db"
    db.parent.mkdir(parents=True)
    _seed_test_db(db, machine_id="aligned-host", telem_offset_seconds=0)
    bundle = load_run(
        DiscoveredRun(
            machine_id="aligned-host",
            run_dir=db.parent,
            db_path=db,
            manifest_path=None,
        )
    )
    assert bundle.telemetry_offset_seconds == 0.0
    assert len(bundle.telemetry) == 1


def test_load_run_offset_detected_when_telemetry_lags_5h(tmp_path: Path):
    """Mirror the macOS powermetrics local-time-as-UTC quirk."""
    db = tmp_path / "run_2026-05-19T22-00-00Z" / "run.db"
    db.parent.mkdir(parents=True)
    _seed_test_db(db, machine_id="skewed-host", telem_offset_seconds=5 * 3600)
    bundle = load_run(
        DiscoveredRun(
            machine_id="skewed-host",
            run_dir=db.parent,
            db_path=db,
            manifest_path=None,
        )
    )
    assert bundle.telemetry_offset_seconds == 5 * 3600
    # After applying the shift, the sample's ts should be ~within the rep window
    sample = bundle.telemetry[0]
    rep = bundle.results[0]
    assert rep.started_at <= sample.ts <= (rep.ended_at or rep.started_at) + timedelta(seconds=5)
