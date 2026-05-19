"""Rendering pipeline benchmark.

Two cooperating runners, both reading the latest SCP plotfile set produced
earlier in the same `alamo-benchmark run`:

- ``render_frames`` — yt SlicePlot → PNG, one frame per Alamo plotfile,
  written under ``run_dir/render/frames_rep<N>/``. Per-rep dirs let us keep
  the per-rep variance measurement clean (each rep does the full work) while
  still giving the encoder a stable set of files to operate on.
- ``render_encode`` — ffmpeg/gifski → animated output, one rep per
  (codec, rep_index). It always picks the most-recently-written
  ``frames_rep*`` dir as input so the codecs see identical pixel data
  regardless of cli shuffle order.

Both runners fail the rep with a clear ``notes`` string if their prerequisite
(SCP output for frames, prior frame dir for encode) is missing, rather than
crashing the whole run.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path

from benchmarks.runners.base import (
    Benchmark,
    RunContext,
    RunResult,
    RunSpec,
    override,
    utc_now,
)

LOG = logging.getLogger(__name__)

_SCP_OUTPUT_BASE = Path("tests") / "SCPSpheresElastic" / "output_bench"
_RENDER_DIR = "render"
_FRAMES_PREFIX = "frames_rep"
# yt parses Alamo plotfiles as the BoxLib family, so fields surface under the
# "boxlib" namespace. Hardcoded here because Alamo doesn't have alternate
# namespaces in its on-disk format.
_FIELD_NAMESPACE = "boxlib"
_DEFAULT_FIELD = "eta"
_DEFAULT_AXIS = "z"


class RenderFramesBenchmark(Benchmark):
    """Render one PNG slice per Alamo cell plotfile via yt."""

    name = "render_frames"

    @override
    def specs(self, ctx: RunContext) -> Iterable[RunSpec]:
        warmups = ctx.config.statistics.warmup_reps
        reps = ctx.config.statistics.reps_short
        res = ctx.config.benchmarks.render_frame_resolution
        for i in range(warmups + reps):
            yield RunSpec(
                benchmark=self.name,
                config={
                    "resolution": [int(res[0]), int(res[1])],
                    "field": _DEFAULT_FIELD,
                    "axis": _DEFAULT_AXIS,
                },
                rep_index=i,
                is_warmup=(i < warmups),
            )

    @override
    def run_one(self, spec: RunSpec, ctx: RunContext) -> RunResult:
        log_path = ctx.log_dir / f"render_frames_rep{spec.rep_index}.log"
        target = ctx.run_dir / _RENDER_DIR / f"{_FRAMES_PREFIX}{spec.rep_index}"
        started_at = utc_now()
        t0 = time.perf_counter()

        source = _find_latest_scp_output(ctx.alamo_dir)
        if source is None:
            return _fail(
                spec,
                started_at,
                t0,
                notes=(
                    f"no SCP output under {ctx.alamo_dir / _SCP_OUTPUT_BASE}; "
                    "enable scp_elastic before render_frames"
                ),
            )

        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)

        try:
            n_frames = _render_frames(
                source=source,
                target=target,
                field=str(spec.config["field"]),
                axis=str(spec.config["axis"]),
                width=int(spec.config["resolution"][0]),
                height=int(spec.config["resolution"][1]),
                log_path=log_path,
            )
        except Exception as e:
            # A render rep failing (yt parse error, OOM, etc.) must not kill an
            # overnight run — log + record + move on.
            LOG.exception("render_frames rep %d failed", spec.rep_index)
            return _fail(spec, started_at, t0, notes=f"yt render failed: {e!r}", log_path=log_path)

        t1 = time.perf_counter()
        return RunResult(
            spec=spec,
            started_at=started_at,
            ended_at=utc_now(),
            wall_s=t1 - t0,
            exit_code=0,
            status="completed",
            stdout_path=str(log_path),
            stderr_path=str(log_path),
            notes=f"frames={n_frames} src={source.name} dest={target.name}",
        )


class RenderEncodeBenchmark(Benchmark):
    """Encode the latest rendered frame set with each configured codec."""

    name = "render_encode"

    @override
    def specs(self, ctx: RunContext) -> Iterable[RunSpec]:
        warmups = ctx.config.statistics.warmup_reps
        reps = ctx.config.statistics.reps_short
        fps = ctx.config.benchmarks.render_fps
        for codec in ctx.config.benchmarks.render_codecs:
            for i in range(warmups + reps):
                yield RunSpec(
                    benchmark=self.name,
                    config={"codec": codec, "fps": int(fps)},
                    rep_index=i,
                    is_warmup=(i < warmups),
                )

    @override
    def run_one(self, spec: RunSpec, ctx: RunContext) -> RunResult:
        codec = str(spec.config["codec"])
        fps = int(spec.config["fps"])
        log_path = ctx.log_dir / f"render_encode_{codec}_rep{spec.rep_index}.log"
        started_at = utc_now()
        t0 = time.perf_counter()

        frames_dir = _find_latest_frames_dir(ctx.run_dir)
        if frames_dir is None:
            return _fail(
                spec,
                started_at,
                t0,
                notes=(
                    f"no frames under {ctx.run_dir / _RENDER_DIR}; "
                    "enable render_frames before render_encode"
                ),
            )

        out_base = ctx.run_dir / _RENDER_DIR / f"encode_{codec}_rep{spec.rep_index}"
        rc, out_path, notes = _encode(
            codec=codec,
            fps=fps,
            frames_dir=frames_dir,
            out_base=out_base,
            log_path=log_path,
        )
        t1 = time.perf_counter()
        return RunResult(
            spec=spec,
            started_at=started_at,
            ended_at=utc_now(),
            wall_s=t1 - t0,
            exit_code=rc,
            status="completed" if rc == 0 else "failed",
            stdout_path=str(log_path),
            stderr_path=str(log_path),
            notes=f"src={frames_dir.name} out={out_path.name} {notes}",
        )


# ----------------------------------------------------------------- helpers


def _find_latest_scp_output(alamo_dir: Path) -> Path | None:
    base = alamo_dir / _SCP_OUTPUT_BASE
    if not base.is_dir():
        return None
    dirs = [p for p in base.iterdir() if p.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda p: p.stat().st_mtime)


def _find_latest_frames_dir(run_dir: Path) -> Path | None:
    base = run_dir / _RENDER_DIR
    if not base.is_dir():
        return None
    dirs = [p for p in base.iterdir() if p.is_dir() and p.name.startswith(_FRAMES_PREFIX)]
    if not dirs:
        return None
    return max(dirs, key=lambda p: p.stat().st_mtime)


def _list_plotfiles(source: Path) -> list[Path]:
    """All cell plotfile directories under an SCP output, in time order.

    We glob directly rather than reading `celloutput.visit` because yt's
    time-series loader has been flaky on Alamo's plotfile layout in practice;
    the per-plotfile load is reliable and the sort key (the leading step number
    in the dirname) gives the correct frame order.
    """
    return sorted(p for p in source.iterdir() if p.is_dir() and p.name.endswith("cell"))


def _render_frames(
    *,
    source: Path,
    target: Path,
    field: str,
    axis: str,
    width: int,
    height: int,
    log_path: Path,
) -> int:
    # yt + matplotlib together pull in ~150 MB of code; keep them off the import
    # path of the benchmark CLI so unrelated subcommands stay snappy.
    # pyright complains because yt doesn't list these in `__all__`, but they
    # are stable public callables — `yt.SlicePlot` etc. is the documented API.
    from yt import (  # noqa: PLC0415
        SlicePlot,  # pyright: ignore[reportPrivateImportUsage]
        load,  # pyright: ignore[reportPrivateImportUsage]
        set_log_level,  # pyright: ignore[reportPrivateImportUsage]
    )

    set_log_level("error")

    plotfiles = _list_plotfiles(source)
    fkey = (_FIELD_NAMESPACE, field)
    with log_path.open("a", encoding="utf-8") as logf:
        logf.write(f"source: {source}\n")
        logf.write(f"plotfiles: {len(plotfiles)}\n")
        for i, plot in enumerate(plotfiles):
            ds = load(str(plot))
            slc = SlicePlot(ds, axis, fkey)
            slc.set_buff_size([width, height])
            slc.set_log(fkey, False)
            out = target / f"frame_{i:05d}.png"
            slc.save(str(out))
            logf.write(f"  rendered {plot.name} -> {out.name}\n")
    return len(plotfiles)


def _encode(
    *,
    codec: str,
    fps: int,
    frames_dir: Path,
    out_base: Path,
    log_path: Path,
) -> tuple[int, Path, str]:
    """Run the codec-specific encoder over `frames_dir/frame_*.png`."""
    pngs = sorted(frames_dir.glob("frame_*.png"))
    if not pngs:
        return -1, out_base, f"no frames in {frames_dir.name}"

    out_base.parent.mkdir(parents=True, exist_ok=True)
    pattern = str(frames_dir / "frame_*.png")
    cmd, out = _encode_command(codec=codec, fps=fps, pattern=pattern, pngs=pngs, out_base=out_base)
    if cmd is None:
        return -1, out, "unsupported codec or missing binary"

    with log_path.open("wb") as logf:
        rc = subprocess.run(
            cmd, check=False, stdout=logf, stderr=subprocess.STDOUT
        ).returncode

    if rc != 0 or not out.exists():
        return rc, out, f"encoder rc={rc} frames={len(pngs)}"

    return 0, out, f"frames={len(pngs)} fps={fps} bytes={out.stat().st_size}"


_CODEC_OUTPUT_SUFFIX = {"gifski": ".gif", "av1": ".webm", "h265": ".mp4"}
_CODEC_BINARY = {"gifski": "gifski", "av1": "ffmpeg", "h265": "ffmpeg"}


def _encode_command(
    *,
    codec: str,
    fps: int,
    pattern: str,
    pngs: list[Path],
    out_base: Path,
) -> tuple[list[str] | None, Path]:
    """Return (argv, output_path) for a codec, or (None, output_path) if unavailable.

    `argv is None` means either the codec is unknown or its required binary
    isn't on PATH — the caller is expected to fail the rep with a clear note.
    """
    suffix = _CODEC_OUTPUT_SUFFIX.get(codec)
    binary_name = _CODEC_BINARY.get(codec)
    if suffix is None or binary_name is None:
        return None, out_base
    out = out_base.with_suffix(suffix)
    binary = shutil.which(binary_name)
    if binary is None:
        return None, out
    if codec == "gifski":
        argv = [
            binary,
            "--output",
            str(out),
            "--fps",
            str(fps),
            "--quality",
            "90",
            *[str(p) for p in pngs],
        ]
    elif codec == "av1":
        argv = _ffmpeg_video_command(
            binary=binary,
            fps=fps,
            pattern=pattern,
            codec_args=["-c:v", "libsvtav1", "-crf", "30", "-preset", "6"],
            out=out,
        )
    else:  # h265
        argv = _ffmpeg_video_command(
            binary=binary,
            fps=fps,
            pattern=pattern,
            codec_args=["-c:v", "libx265", "-crf", "28", "-preset", "medium", "-tag:v", "hvc1"],
            out=out,
        )
    return argv, out


def _ffmpeg_video_command(
    *,
    binary: str,
    fps: int,
    pattern: str,
    codec_args: list[str],
    out: Path,
) -> list[str]:
    """Build the ffmpeg argv shared by av1 (WebM) and h265 (MP4) encodes.

    The `pad=ceil(iw/2)*2:ceil(ih/2)*2` filter rounds odd dimensions up to even
    so libx265 can ingest yt-rendered PNGs whose width/height aren't multiples
    of 2 (yt's matplotlib output dimensions include axes/colorbar padding).
    """
    return [
        binary,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-framerate",
        str(fps),
        "-pattern_type",
        "glob",
        "-i",
        pattern,
        "-vf",
        "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        *codec_args,
        "-pix_fmt",
        "yuv420p",
        str(out),
    ]


def _fail(
    spec: RunSpec,
    started_at: str,
    t0: float,
    *,
    notes: str,
    log_path: Path | None = None,
) -> RunResult:
    stdout_path = str(log_path) if log_path is not None else None
    return RunResult(
        spec=spec,
        started_at=started_at,
        ended_at=utc_now(),
        wall_s=time.perf_counter() - t0,
        exit_code=-1,
        status="failed",
        stdout_path=stdout_path,
        stderr_path=stdout_path,
        notes=notes,
    )
