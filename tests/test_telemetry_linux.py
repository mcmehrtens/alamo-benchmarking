"""Turbostat parser tests against real Xeon captures.

Fixtures cover three CPU generations with different turbostat column sets:
- W-1370 (Rocket Lake): GFX / SysWatt columns, 8 cores + HT
- W5-2545 (Sapphire Rapids): no GFX columns, 12 cores + HT
- E3-1240v6 (Kaby Lake): older C-state column names (C1%/C3%/C6%/C7s%/C8% vs
  C1ACPI% etc.), 4 cores + HT

Together they verify the parser is keyed by column name (not position) and
tolerates the column delta across kernel/CPU generations."""

from __future__ import annotations

from pathlib import Path

from benchmarks.telemetry.base import TelemetrySample
from benchmarks.telemetry.linux import TurbostatStreamParser, parse_turbostat_stream

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_w_1370_emits_five_iterations():
    samples = parse_turbostat_stream(_load("turbostat_linux_xeon-w-1370.txt"))
    assert len(samples) == 5


def test_w5_2545_emits_five_iterations():
    samples = parse_turbostat_stream(_load("turbostat_linux_xeon-w5-2545.txt"))
    assert len(samples) == 5


def test_w_1370_package_power_present_and_reasonable():
    samples = parse_turbostat_stream(_load("turbostat_linux_xeon-w-1370.txt"))
    pkg = samples[0].package_power_w
    assert pkg is not None
    # Xeon W-1370: 80 W TDP package; the captures look heavily loaded
    # (~80 PkgWatt). 5 < pkg < 200 is the sanity envelope.
    assert 5.0 < pkg < 200.0, f"package_power_w={pkg} out of plausible range"


def test_w5_2545_package_power_present():
    samples = parse_turbostat_stream(_load("turbostat_linux_xeon-w5-2545.txt"))
    pkg = samples[0].package_power_w
    assert pkg is not None
    assert pkg > 0.0


def test_w_1370_has_hyperthread_siblings_as_virtual():
    """Each physical Core appears twice in the per-CPU rows (HT); the second
    sighting should be marked 'virtual'."""
    samples = parse_turbostat_stream(_load("turbostat_linux_xeon-w-1370.txt"))
    sample = samples[0]
    physical = [c for c in sample.per_core if c.core_type == "physical"]
    virtual = [c for c in sample.per_core if c.core_type == "virtual"]
    assert len(physical) > 0
    assert len(virtual) > 0
    assert len(physical) == len(virtual), (
        "W-1370 is HT-enabled; physical and virtual counts should match"
    )


def test_w5_2545_per_core_count_matches_xeon_w5_2545():
    """W5-2545: 12 physical cores + HT = 24 logical CPUs."""
    samples = parse_turbostat_stream(_load("turbostat_linux_xeon-w5-2545.txt"))
    assert len(samples[0].per_core) == 24


def test_w_1370_per_core_count_matches_xeon_w_1370():
    """W-1370: 8 physical cores + HT = 16 logical CPUs."""
    samples = parse_turbostat_stream(_load("turbostat_linux_xeon-w-1370.txt"))
    assert len(samples[0].per_core) == 16


def test_temperature_present_on_physical_thread_rows():
    """Turbostat only reports CoreTmp on the first thread of each core; the
    parser should populate temp_c on physical rows."""
    samples = parse_turbostat_stream(_load("turbostat_linux_xeon-w-1370.txt"))
    physical_with_temp = [
        c for c in samples[0].per_core if c.core_type == "physical" and c.temp_c is not None
    ]
    assert len(physical_with_temp) > 0


def test_incremental_feed_matches_oneshot_parse():
    """Chunked feeds must produce the same samples as a one-shot parse."""
    data = _load("turbostat_linux_xeon-w5-2545.txt")
    parser = TurbostatStreamParser()
    out: list[TelemetrySample] = []
    chunk_size = 200
    for i in range(0, len(data), chunk_size):
        out.extend(parser.feed(data[i : i + chunk_size]))
    out.extend(parser.flush())
    oneshot = parse_turbostat_stream(data)
    assert len(out) == len(oneshot)
    for a, b in zip(out, oneshot, strict=True):
        assert a.package_power_w == b.package_power_w
        assert len(a.per_core) == len(b.per_core)


def test_column_layout_differs_between_w_1370_and_w5_2545():
    """Sanity: the two fixtures have different headers; both must parse."""
    a = parse_turbostat_stream(_load("turbostat_linux_xeon-w-1370.txt"))
    b = parse_turbostat_stream(_load("turbostat_linux_xeon-w5-2545.txt"))
    assert len(a) == len(b) == 5


def test_e3_1240v6_emits_five_iterations():
    samples = parse_turbostat_stream(_load("turbostat_linux_xeon-e3-1240v6.txt"))
    assert len(samples) == 5


def test_e3_1240v6_per_core_count_matches_xeon_e3_1240v6():
    """E3-1240v6: 4 physical cores + HT = 8 logical CPUs."""
    samples = parse_turbostat_stream(_load("turbostat_linux_xeon-e3-1240v6.txt"))
    assert len(samples[0].per_core) == 8


def test_e3_1240v6_has_hyperthread_siblings_as_virtual():
    samples = parse_turbostat_stream(_load("turbostat_linux_xeon-e3-1240v6.txt"))
    sample = samples[0]
    physical = [c for c in sample.per_core if c.core_type == "physical"]
    virtual = [c for c in sample.per_core if c.core_type == "virtual"]
    assert len(physical) == 4
    assert len(virtual) == 4


def test_e3_1240v6_older_cstate_columns_still_yield_package_power():
    """Kaby Lake turbostat uses C1%/C3%/C6%/C7s%/C8% instead of the newer
    C1ACPI%/C2ACPI%/... names — the parser keys by column name so PkgWatt
    should still resolve cleanly."""
    samples = parse_turbostat_stream(_load("turbostat_linux_xeon-e3-1240v6.txt"))
    pkg = samples[0].package_power_w
    assert pkg is not None
    # Capture is idle; ~10 W is typical for this 72 W TDP chip at idle.
    assert 0.0 < pkg < 200.0, f"package_power_w={pkg} out of plausible range"
