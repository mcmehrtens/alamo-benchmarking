"""Powermetrics parser tests against real captures from M1 Pro / M4 Pro / M5 Pro.

Real fixtures > mocks (per CLAUDE.md). These verify cluster-name mapping,
mW-to-W conversion, the Fusion-Architecture super-cluster on M5 Pro, and the
incremental parser's handling of the `\\n\\x00` plist separator powermetrics
emits between samples."""

from __future__ import annotations

from pathlib import Path

from benchmarks.telemetry.base import TelemetrySample
from benchmarks.telemetry.macos import (
    PowermetricsStreamParser,
    parse_powermetrics_stream,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _core_types(sample: TelemetrySample) -> set[str]:
    return {c.core_type for c in sample.per_core}


def test_m1pro_emits_five_samples_with_p_and_e_clusters():
    samples = parse_powermetrics_stream(_load("powermetrics_macos_m1pro.plist"))
    assert len(samples) == 5
    types = _core_types(samples[0])
    assert "performance" in types
    assert "efficiency" in types
    assert "super" not in types


def test_m4pro_emits_five_samples_with_p_and_e_clusters():
    samples = parse_powermetrics_stream(_load("powermetrics_macos_m4pro.plist"))
    assert len(samples) == 5
    types = _core_types(samples[0])
    assert "performance" in types
    assert "efficiency" in types
    assert "super" not in types


def test_m5pro_has_fusion_super_cluster_and_no_efficiency():
    samples = parse_powermetrics_stream(_load("powermetrics_macos_m5pro.plist"))
    assert len(samples) == 5
    types = _core_types(samples[0])
    assert "super" in types, "M5 Pro should report S-Cluster cores as 'super'"
    assert "performance" in types
    assert "efficiency" not in types, "Fusion-Architecture chips have no E-cluster"


def test_package_power_is_in_watts_not_milliwatts():
    samples = parse_powermetrics_stream(_load("powermetrics_macos_m5pro.plist"))
    pkg = samples[0].package_power_w
    assert pkg is not None
    # Idle captures should land far below the chip's package TDP (~40W).
    # The fixtures are idle samples, so a value over 100 W means we forgot
    # the mW->W conversion.
    assert 0.0 < pkg < 100.0, f"package_power_w={pkg} looks unconverted"


def test_per_core_indices_are_unique_within_a_sample():
    samples = parse_powermetrics_stream(_load("powermetrics_macos_m1pro.plist"))
    indices = [c.core_index for c in samples[0].per_core]
    assert len(indices) == len(set(indices))


def test_timestamp_is_iso8601_utc():
    samples = parse_powermetrics_stream(_load("powermetrics_macos_m1pro.plist"))
    ts = samples[0].ts
    assert ts.endswith(("+00:00", "Z"))


def test_incremental_feed_matches_oneshot_parse():
    """Splitting the stream into arbitrary chunks must produce the same samples."""
    data = _load("powermetrics_macos_m4pro.plist")
    parser = PowermetricsStreamParser()
    out: list[TelemetrySample] = []
    chunk_size = 1024
    for i in range(0, len(data), chunk_size):
        out.extend(parser.feed(data[i : i + chunk_size]))
    out.extend(parser.flush())
    oneshot = parse_powermetrics_stream(data)
    assert len(out) == len(oneshot)
    for a, b in zip(out, oneshot, strict=True):
        assert a.ts == b.ts
        assert a.package_power_w == b.package_power_w
        assert len(a.per_core) == len(b.per_core)


def test_malformed_plist_is_skipped_not_raised():
    """A truncated plist between two good ones must not kill the parser."""
    good = _load("powermetrics_macos_m1pro.plist")
    # Insert a junk plist that closes with `</plist>` so the splitter advances.
    junk = b"<?xml version='1.0'?><plist><not real xml></plist>\n\x00"
    samples = parse_powermetrics_stream(good[:26460] + junk + good[26460:])
    # 5 valid plists in `good`; the junk should be dropped, leaving 5.
    assert len(samples) == 5
