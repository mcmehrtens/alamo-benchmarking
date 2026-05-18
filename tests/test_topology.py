"""Unit tests for `benchmarks.topology` — pure-logic checks of the sweep generator
and the Apple-Silicon classification rules."""

from __future__ import annotations

from benchmarks.topology import Topology


def _topo(physical: int, virtual: int = 0) -> Topology:
    return Topology(
        physical=physical,
        virtual=virtual,
        super_cores=0,
        perf_cores=physical,
        eff_cores=virtual,
        cpu_brand="test",
        classification_reason="test",
    )


def test_core_sweep_doubles_to_physical():
    assert _topo(8).core_sweep() == [1, 2, 4, 8]


def test_core_sweep_appends_physical_plus_virtual_when_present():
    assert _topo(8, virtual=2).core_sweep() == [1, 2, 4, 8, 10]


def test_core_sweep_handles_non_power_of_two_physical():
    # Real Xeon: 18 physical cores.
    assert _topo(18).core_sweep() == [1, 2, 4, 8, 16, 18]


def test_core_sweep_includes_extras_and_dedups():
    assert _topo(8).core_sweep(extra=(2, 3, 6, 8)) == [1, 2, 3, 4, 6, 8]


def test_core_sweep_drops_zero_and_negative_extras():
    assert _topo(4).core_sweep(extra=(0, -1, 5)) == [1, 2, 4, 5]


def test_core_sweep_when_physical_is_one():
    assert _topo(1).core_sweep() == [1]


def test_core_sweep_when_physical_plus_virtual_equals_xeon_with_ht():
    # Xeon 18-core + HT: physical=18, virtual=18 (one extra thread per core).
    assert _topo(18, virtual=18).core_sweep() == [1, 2, 4, 8, 16, 18, 36]
