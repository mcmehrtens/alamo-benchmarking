"""Tests for the RAPL power-cap preflight check.

We point `_check_rapl` at a temp path that mimics `/sys/class/powercap/` so the
test runs identically on macOS and Linux. The check itself reads only sysfs
files (no sudo, no /proc), so a fake tree fully exercises the logic."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path

from benchmarks import preflight


def _write_package(root: Path, idx: int, pl1_uw: int, max_uw: int) -> None:
    pkg = root / f"intel-rapl:{idx}"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "constraint_0_power_limit_uw").write_text(str(pl1_uw))
    (pkg / "constraint_0_max_power_uw").write_text(str(max_uw))
    (pkg / "name").write_text(f"package-{idx}")


def test_non_linux_skips_cleanly(tmp_path: Path) -> None:
    check = preflight._check_rapl("Darwin", root=tmp_path)
    assert check.passed
    assert check.severity == "advisory"
    assert "n/a" in check.observed


def test_missing_powercap_dir_skips(tmp_path: Path) -> None:
    """An AMD or VM Linux box may lack the powercap sysfs entirely."""
    check = preflight._check_rapl("Linux", root=tmp_path / "missing")
    assert check.passed
    assert "missing" in check.observed


def test_empty_powercap_dir_skips(tmp_path: Path) -> None:
    check = preflight._check_rapl("Linux", root=tmp_path)
    assert check.passed
    assert "no intel-rapl" in check.observed


def test_pl1_at_max_passes(tmp_path: Path) -> None:
    # Xeon W5-2545: 200 W TDP package. PL1 == max ⇒ unrestricted.
    _write_package(tmp_path, 0, pl1_uw=200_000_000, max_uw=200_000_000)
    check = preflight._check_rapl("Linux", root=tmp_path)
    assert check.passed
    assert "PL1 200W/200W" in check.observed


def test_pl1_at_90_percent_passes(tmp_path: Path) -> None:
    # 90% of TDP is above the 80% threshold — common thermal-margin setting.
    _write_package(tmp_path, 0, pl1_uw=180_000_000, max_uw=200_000_000)
    check = preflight._check_rapl("Linux", root=tmp_path)
    assert check.passed
    assert "PL1 180W/200W" in check.observed


def test_pl1_at_50_percent_fails(tmp_path: Path) -> None:
    # 50% of TDP — definitely throttled. Sustained workloads will look slow.
    _write_package(tmp_path, 0, pl1_uw=100_000_000, max_uw=200_000_000)
    check = preflight._check_rapl("Linux", root=tmp_path)
    assert not check.passed
    assert "PL1 100W/200W" in check.observed
    assert "80%" in check.required


def test_multi_socket_both_must_pass(tmp_path: Path) -> None:
    """Dual-socket box: if ANY package is throttled, flag advisory failure."""
    _write_package(tmp_path, 0, pl1_uw=200_000_000, max_uw=200_000_000)
    _write_package(tmp_path, 1, pl1_uw=100_000_000, max_uw=200_000_000)  # throttled
    check = preflight._check_rapl("Linux", root=tmp_path)
    assert not check.passed
    # Both packages should appear in the observed string.
    assert "intel-rapl:0" in check.observed
    assert "intel-rapl:1" in check.observed


def test_unreadable_package_marked_but_not_failed(tmp_path: Path) -> None:
    """A package dir without the expected files (e.g. partial sysfs mount)
    should be reported as unreadable but not cause a hard fail by itself."""
    pkg = tmp_path / "intel-rapl:0"
    pkg.mkdir()
    # no constraint files
    check = preflight._check_rapl("Linux", root=tmp_path)
    assert check.passed  # no readable data → no negative finding
    assert "unreadable" in check.observed


def test_subzone_dirs_ignored(tmp_path: Path) -> None:
    """`/sys/class/powercap/intel-rapl:0:0` is a sub-zone (DRAM, etc.) — we
    only want top-level package zones."""
    _write_package(tmp_path, 0, pl1_uw=200_000_000, max_uw=200_000_000)
    # Sub-zone with unrealistic PL1 — should be ignored entirely.
    sub = tmp_path / "intel-rapl:0:0"
    sub.mkdir()
    (sub / "constraint_0_power_limit_uw").write_text("1000000")  # 1 W
    (sub / "constraint_0_max_power_uw").write_text("200000000")
    check = preflight._check_rapl("Linux", root=tmp_path)
    assert check.passed  # only :0 is examined; :0:0 ignored
    assert "intel-rapl:0:0" not in check.observed


def test_zero_max_power_handled(tmp_path: Path) -> None:
    """A package reporting max_power=0 would div-by-zero a naive impl."""
    _write_package(tmp_path, 0, pl1_uw=200_000_000, max_uw=0)
    check = preflight._check_rapl("Linux", root=tmp_path)
    # No valid ratio computable → reported as unreadable rather than failing
    assert check.passed
    assert "unreadable" in check.observed
