"""Rendering pipeline benchmark.

Stages:
1. Frame generation from Alamo plotfiles (yt + matplotlib) — sequential.
2. gifski → animated GIF.
3. ffmpeg → AV1 WebM (libsvtav1).
4. ffmpeg → H.265 MP4 (libx265).

Each stage is a separate benchmark row so they can be compared independently.

Stub implementation: yields no specs. The full pipeline is deferred — it depends
on a successful SCPSpheresElastic run to produce the plotfiles to render.
"""

from __future__ import annotations

from collections.abc import Iterable

from benchmarks.runners.base import Benchmark, RunContext, RunResult, RunSpec, override


class RenderBenchmark(Benchmark):
    name = "render"

    @override
    def specs(self, ctx: RunContext) -> Iterable[RunSpec]:
        del ctx
        return iter(())

    @override
    def run_one(self, spec: RunSpec, ctx: RunContext) -> RunResult:
        del spec, ctx
        raise NotImplementedError("Rendering pipeline is a v2 feature.")
