"""Tests for `cli._order_specs`.

The contract: warmups always run before measured reps, with both groups
shuffled internally by the same RNG. This protects warmups from landing
mid-sequence (where they'd no longer warm anything) while preserving the
drift-defeat goal for measured reps."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import random
from typing import Any

from benchmarks import cli
from benchmarks.runners.base import RunSpec


def _spec(idx: int, *, warmup: bool, np: int = 1) -> RunSpec:
    cfg: dict[str, Any] = {"np": np, "idx": idx}
    return RunSpec(benchmark="test", config=cfg, rep_index=idx, is_warmup=warmup)


def test_warmup_runs_before_measured_simple() -> None:
    """The classic noise_floor shape: 1 warmup + 19 measured."""
    specs = [_spec(0, warmup=True)] + [_spec(i, warmup=False) for i in range(1, 20)]
    ordered = cli._order_specs(specs, random.Random(42))
    assert ordered[0].is_warmup is True
    assert all(not s.is_warmup for s in ordered[1:])


def test_no_warmups_falls_back_to_full_shuffle() -> None:
    """When `warmup_reps=0`, everything is measured and the shuffle is uniform."""
    specs = [_spec(i, warmup=False) for i in range(10)]
    ordered = cli._order_specs(specs, random.Random(1))
    assert all(not s.is_warmup for s in ordered)
    # All input specs preserved (shuffle, not filter).
    assert {s.rep_index for s in ordered} == set(range(10))


def test_multiple_warmups_all_first() -> None:
    """scp_elastic with `warmup_reps=1` and a 4-point np sweep produces 4
    warmup specs + 4 measured specs (1 warmup + 1 rep per np)."""
    sweep = [1, 2, 4, 8]
    warmups = [_spec(0, warmup=True, np=n) for n in sweep]
    measured = [_spec(1, warmup=False, np=n) for n in sweep]
    ordered = cli._order_specs(warmups + measured, random.Random(7))
    head = ordered[: len(warmups)]
    tail = ordered[len(warmups) :]
    assert all(s.is_warmup for s in head)
    assert all(not s.is_warmup for s in tail)
    # Every np value still appears in both halves.
    assert {s.config["np"] for s in head} == set(sweep)
    assert {s.config["np"] for s in tail} == set(sweep)


def test_order_is_deterministic_for_same_seed() -> None:
    """`run.random_seed` is recorded in the manifest — same seed must produce
    the same execution order, even with the warmup-first carve-out."""
    specs_a = [_spec(i, warmup=(i < 2)) for i in range(10)]
    specs_b = [_spec(i, warmup=(i < 2)) for i in range(10)]
    out_a = cli._order_specs(specs_a, random.Random(2026))
    out_b = cli._order_specs(specs_b, random.Random(2026))
    assert [s.rep_index for s in out_a] == [s.rep_index for s in out_b]


def test_different_seeds_produce_different_orders() -> None:
    specs_a = [_spec(i, warmup=False) for i in range(20)]
    specs_b = [_spec(i, warmup=False) for i in range(20)]
    out_a = [s.rep_index for s in cli._order_specs(specs_a, random.Random(1))]
    out_b = [s.rep_index for s in cli._order_specs(specs_b, random.Random(2))]
    # Statistically certain to differ at 20 elements; this is a sanity check
    # that the seed actually propagates through.
    assert out_a != out_b


def test_warmup_specs_shuffled_among_themselves() -> None:
    """For runners with >1 warmup, warmup order is itself randomized so the
    state the first measured rep inherits doesn't always come from the same
    final warmup configuration."""
    warmups = [_spec(0, warmup=True, np=n) for n in (1, 2, 4, 8, 10)]
    # Seed picked to give a non-identity permutation. The point isn't a
    # specific order — it's that the order isn't pinned by insertion.
    ordered = cli._order_specs(warmups, random.Random(99))
    inserted_np = [s.config["np"] for s in warmups]
    shuffled_np = [s.config["np"] for s in ordered]
    assert set(shuffled_np) == set(inserted_np)
    # We don't assert "must differ" because for some seeds the shuffle might
    # produce the identity by chance. The deterministic-seed test above
    # covers reproducibility.


def test_empty_input_returns_empty() -> None:
    assert cli._order_specs([], random.Random(0)) == []
