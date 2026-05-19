"""Unit tests for the render runners' pure-logic helpers."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from benchmarks.runners import render


def test_find_latest_scp_output_picks_most_recent(tmp_path: Path) -> None:
    """The frame renderer must see the freshest SCP rep — not the alphabetically last."""
    base = tmp_path / "tests" / "SCPSpheresElastic" / "output_bench"
    base.mkdir(parents=True)
    (base / "np2_rep0_aaa").mkdir()
    older = base / "np8_rep0_zzz"
    older.mkdir()
    # Make np2_rep0_aaa fresher by touching it after a small gap.
    os.utime(older, (1_000_000, 1_000_000))
    time.sleep(0.01)
    (base / "np2_rep0_aaa").touch()
    latest = render._find_latest_scp_output(tmp_path)
    assert latest is not None
    assert latest.name == "np2_rep0_aaa"


def test_find_latest_scp_output_returns_none_when_missing(tmp_path: Path) -> None:
    assert render._find_latest_scp_output(tmp_path) is None


def test_find_latest_frames_dir_filters_by_prefix(tmp_path: Path) -> None:
    base = tmp_path / "render"
    base.mkdir()
    (base / "frames_rep0").mkdir()
    (base / "frames_rep1").mkdir()
    (base / "encode_av1_rep0").mkdir()  # not a frames dir
    latest = render._find_latest_frames_dir(tmp_path)
    assert latest is not None
    assert latest.name.startswith("frames_rep")


def test_find_latest_frames_dir_returns_none_when_no_render_dir(tmp_path: Path) -> None:
    assert render._find_latest_frames_dir(tmp_path) is None


@pytest.mark.parametrize(
    ("codec", "expected_suffix"),
    [("gifski", ".gif"), ("av1", ".webm"), ("h265", ".mp4")],
)
def test_encode_command_picks_correct_extension(
    tmp_path: Path, codec: str, expected_suffix: str
) -> None:
    pngs = [tmp_path / f"frame_{i:05d}.png" for i in range(3)]
    for p in pngs:
        p.touch()
    argv, out = render._encode_command(
        codec=codec,
        fps=30,
        pattern=str(tmp_path / "frame_*.png"),
        pngs=pngs,
        out_base=tmp_path / "encode_test",
    )
    # If the binary isn't installed in the test environment, argv may be None;
    # the suffix is still computed and is the contract we care about here.
    assert out.suffix == expected_suffix
    if argv is not None:
        assert any(str(out) in arg for arg in argv)


def test_encode_command_rejects_unknown_codec(tmp_path: Path) -> None:
    argv, _ = render._encode_command(
        codec="opus",
        fps=30,
        pattern="*.png",
        pngs=[tmp_path / "frame_00000.png"],
        out_base=tmp_path / "x",
    )
    assert argv is None
