"""Tests for the OpenMPI binding-report parser.

The benchmark sets `OMPI_MCA_orte_report_bindings=1` and passes
`--bind-to core --map-by core` (on Linux). The runtime prints one line per
rank to stderr. We parse those lines and surface a one-line summary in the
result row, so post-run analysis can tell at a glance whether each `-np`
sweep step actually got the binding it requested."""

from __future__ import annotations

from benchmarks.runners.scp_elastic import parse_mpi_bindings

# Real-world capture pattern from `mpiexec --bind-to core --map-by core -np 4`
# on a quad-core box. The exact format is `[host:pid] MCW rank N bound to ...`.
_BOUND_4 = """\
[xeon-w5:12345] MCW rank 0 bound to socket 0[core 0[hwt 0]]: [B/././.][./././.]
[xeon-w5:12345] MCW rank 1 bound to socket 0[core 1[hwt 0]]: [./B/./.][./././.]
[xeon-w5:12345] MCW rank 2 bound to socket 0[core 2[hwt 0]]: [././B/.][./././.]
[xeon-w5:12345] MCW rank 3 bound to socket 0[core 3[hwt 0]]: [./././B][./././.]
"""


def test_empty_stderr_parses_to_zero() -> None:
    info = parse_mpi_bindings("")
    assert info.ranks_bound == 0
    assert info.ranks_unbound == 0
    assert info.sockets == []
    assert info.cores == []
    assert info.one_rank_per_core is False


def test_four_ranks_one_per_core() -> None:
    info = parse_mpi_bindings(_BOUND_4)
    assert info.ranks_bound == 4
    assert info.ranks_unbound == 0
    assert info.sockets == [0]
    assert info.cores == [0, 1, 2, 3]
    assert info.hwts == [0]
    assert info.one_rank_per_core is True


def test_dual_socket_distribution() -> None:
    stderr = """\
[box:1] MCW rank 0 bound to socket 0[core 0[hwt 0]]: [B...][....]
[box:1] MCW rank 1 bound to socket 1[core 0[hwt 0]]: [....][B...]
[box:1] MCW rank 2 bound to socket 0[core 1[hwt 0]]: [.B..][....]
[box:1] MCW rank 3 bound to socket 1[core 1[hwt 0]]: [....][.B..]
"""
    info = parse_mpi_bindings(stderr)
    assert info.ranks_bound == 4
    assert info.sockets == [0, 1]
    assert info.cores == [0, 1]
    assert info.one_rank_per_core is True


def test_oversubscribed_core_flagged() -> None:
    """Two ranks pinned to the same (socket, core) means PRRTE oversubscribed.
    Likely indicates the user asked for more ranks than physical cores."""
    stderr = """\
[box:1] MCW rank 0 bound to socket 0[core 0[hwt 0]]: [B.][..]
[box:1] MCW rank 1 bound to socket 0[core 0[hwt 0]]: [B.][..]
"""
    info = parse_mpi_bindings(stderr)
    assert info.ranks_bound == 2
    assert info.one_rank_per_core is False


def test_unbound_ranks_counted_separately() -> None:
    stderr = """\
[box:1] MCW rank 0 bound to socket 0[core 0[hwt 0]]: [B/././.]
[box:1] MCW rank 1 is not bound (or bound to all available processors)
[box:1] MCW rank 2 is not bound (or bound to all available processors)
"""
    info = parse_mpi_bindings(stderr)
    assert info.ranks_bound == 1
    assert info.ranks_unbound == 2


def test_hwthread_seen_when_present() -> None:
    stderr = """\
[box:1] MCW rank 0 bound to socket 0[core 0[hwt 1]]: [.B/././.]
"""
    info = parse_mpi_bindings(stderr)
    assert info.hwts == [1]


def test_extra_noise_ignored() -> None:
    """Real stderr contains lots of unrelated noise — Alamo banner, MPI init
    chatter, etc. The parser should pick out only binding lines."""
    stderr = """\
Some random Alamo banner line
[host:1] mca_btl_base_select: matching btl 'self' [success]
[host:1] MCW rank 0 bound to socket 0[core 5[hwt 0]]: [...B][....]
WARNING: unrelated stderr noise
[host:1] MCW rank 1 bound to socket 0[core 6[hwt 0]]: [....][..B.]
"""
    info = parse_mpi_bindings(stderr)
    assert info.ranks_bound == 2
    assert info.cores == [5, 6]
