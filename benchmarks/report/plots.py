"""Matplotlib helpers for the report.

Every public ``draw_*`` here returns the relative path (under ``figures/``) of
the SVG it wrote. SVGs are saved with ``transparent=True`` so they drop cleanly
onto any page background; the report's ``<img>`` tags reference them by
relative path.

Notes on style:
- ``Agg`` backend, no GUI. Imported via ``matplotlib.use`` before pyplot.
- Machine-stable colors are assigned in :func:`palette_for_machines` and used
  consistently across every figure that touches more than one machine.
- We never set ``axes.facecolor``/``figure.facecolor`` ourselves; ``savefig
  (transparent=True)`` overrides them at write time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import matplotlib as mpl

mpl.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

from benchmarks.report.data import (
    PerCoreSample,
    ResultRow,
    RunBundle,
    TelemetrySample,
    benchmark_window,
    measured_results,
    optimal_scp_config,
)
from benchmarks.report.geekbench import GeekbenchData, LogLogFit, fit_loglog
from benchmarks.report.stats import summarize

_FIG_SIZE_DEFAULT = (8.0, 4.5)
_FIG_SIZE_WIDE = (10.0, 4.5)
_FIG_SIZE_SQUARE = (5.5, 5.5)
_DPI = 110

# Color cycle adapted from matplotlib's tab10 / tab20 — high contrast on both
# light and dark backgrounds.
_MACHINE_COLOR_CYCLE = [
    "#1f77b4",
    "#ff7f0e",
    "#2ca02c",
    "#d62728",
    "#9467bd",
    "#8c564b",
    "#e377c2",
    "#17becf",
]

# Soft pastel band colors used to overlay benchmark windows on full-run plots.
_BENCH_BAND_COLORS = {
    "noise_floor": "#bcbddc",
    "compile_serial": "#9ecae1",
    "compile_parallel": "#6baed6",
    "regression_suite": "#a1d99b",
    "scp_elastic": "#fdae6b",
    "render_frames": "#fdd0a2",
    "render_encode": "#fcbba1",
}


def palette_for_machines(machine_ids: list[str]) -> dict[str, str]:
    """Stable color assignment by sorted machine_id."""
    sorted_ids = sorted(machine_ids)
    return {
        m: _MACHINE_COLOR_CYCLE[i % len(_MACHINE_COLOR_CYCLE)]
        for i, m in enumerate(sorted_ids)
    }


# ---------------------------------------------------------------- saving helpers


def _save(fig: Figure, out_dir: Path, name: str) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{name}.svg"
    fig.tight_layout()
    fig.savefig(target, format="svg", transparent=True, bbox_inches="tight")
    plt.close(fig)
    return f"figures/{name}.svg"


def _machine_label(bundle: RunBundle) -> str:
    return bundle.run.machine_id


# ---------------------------------------------------------------- noise floor


def draw_noise_floor_box(
    bundles: list[RunBundle], palette: dict[str, str], out_dir: Path
) -> str:
    """Box plot of noise_floor wall_s per machine (measured reps only)."""
    fig, ax = plt.subplots(figsize=_FIG_SIZE_WIDE, dpi=_DPI)
    data: list[list[float]] = []
    labels: list[str] = []
    colors: list[str] = []
    for b in bundles:
        vals = [
            r.wall_s
            for r in measured_results(b, "noise_floor")
            if r.wall_s is not None
        ]
        if vals:
            data.append(vals)
            labels.append(_machine_label(b))
            colors.append(palette[b.run.machine_id])
    box = ax.boxplot(
        data,
        tick_labels=labels,
        widths=0.55,
        patch_artist=True,
        showmeans=False,
        whis=(0, 100),
    )
    for patch, color in zip(box["boxes"], colors, strict=True):
        patch.set_facecolor(color)
        patch.set_alpha(0.55)
        patch.set_edgecolor(color)
    for median in box["medians"]:
        median.set_color("#222")
        median.set_linewidth(1.2)
    ax.set_ylabel("Noise-floor wall time (s)")
    ax.set_title("Noise floor: 4000x4000 matmul, 19 measured reps per machine")
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    return _save(fig, out_dir, "noise_floor_boxplot")


def draw_noise_floor_strip(
    bundle: RunBundle, palette: dict[str, str], out_dir: Path
) -> str:
    """Per-machine zoom: 19 measured reps on rep-order x-axis, IQR band + median.

    The aggregate box plot is unreadable on the smaller-spread machines because
    the y-axis is dominated by the slowest. This view auto-scales to the
    machine's own range so the within-machine variance is visible.
    """
    fig, ax = plt.subplots(figsize=_FIG_SIZE_DEFAULT, dpi=_DPI)
    rows = sorted(
        (r for r in measured_results(bundle, "noise_floor") if r.wall_s is not None),
        key=lambda r: r.rep_index,
    )
    color = palette[bundle.run.machine_id]
    if not rows:
        ax.text(0.5, 0.5, "no noise_floor data", ha="center", va="center")
        return _save(fig, out_dir, f"noise_floor_{bundle.run.machine_id}")
    xs = [r.rep_index for r in rows]
    ys = [r.wall_s for r in rows if r.wall_s is not None]
    s = summarize(ys)
    if s.q1 is not None and s.q3 is not None:
        ax.axhspan(s.q1, s.q3, color=color, alpha=0.15, linewidth=0, label="IQR")
    if s.median is not None:
        ax.axhline(
            s.median,
            color=color,
            linestyle="--",
            linewidth=1.2,
            alpha=0.9,
            label=f"median = {s.median:.4f} s",
        )
    ax.scatter(xs, ys, color=color, edgecolor="#222", linewidth=0.4, s=36, zorder=3)
    ax.set_xlabel("Rep index (measurement order)")
    ax.set_ylabel("Wall time (s)")
    ax.set_title(f"Noise floor: {bundle.run.machine_id}")
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    ax.legend(frameon=False, loc="best")
    return _save(fig, out_dir, f"noise_floor_{bundle.run.machine_id}")


# ---------------------------------------------------------------- compile bars


def draw_compile_bars(
    bundles: list[RunBundle], palette: dict[str, str], out_dir: Path
) -> str:
    """Side-by-side median wall-time bars: serial vs parallel compile.

    IQR is shown as the error bar (lower=q1, upper=q3).
    """
    _ = palette  # the two bars use fixed serial/parallel colors; palette unused here
    fig, ax = plt.subplots(figsize=_FIG_SIZE_WIDE, dpi=_DPI)
    machines = [b.run.machine_id for b in bundles]
    x = np.arange(len(machines), dtype=float)
    width = 0.36

    def _stats(bench: str) -> tuple[list[float], list[float], list[float]]:
        meds: list[float] = []
        lows: list[float] = []
        highs: list[float] = []
        for b in bundles:
            vals = [
                r.wall_s
                for r in measured_results(b, bench)
                if r.wall_s is not None
            ]
            s = summarize(vals)
            meds.append(s.median or 0.0)
            lows.append((s.median or 0.0) - (s.q1 or s.median or 0.0))
            highs.append((s.q3 or s.median or 0.0) - (s.median or 0.0))
        return meds, lows, highs

    s_med, s_lo, s_hi = _stats("compile_serial")
    p_med, p_lo, p_hi = _stats("compile_parallel")

    ax.bar(
        x - width / 2,
        s_med,
        width,
        yerr=[s_lo, s_hi],
        capsize=3,
        color="#9ecae1",
        edgecolor="#3182bd",
        label="compile_serial (j=1)",
    )
    ax.bar(
        x + width / 2,
        p_med,
        width,
        yerr=[p_lo, p_hi],
        capsize=3,
        color="#6baed6",
        edgecolor="#08519c",
        label="compile_parallel (j=physical)",
    )
    ax.set_xticks(x)
    ax.set_xticklabels(machines, rotation=20, ha="right")
    ax.set_ylabel("Median wall time (s) -- error bars = IQR")
    ax.set_title("Cold-cache compile, all configured dims (2D + 3D)")
    ax.legend(frameon=False)
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    return _save(fig, out_dir, "compile_bars")


# ---------------------------------------------------------------- regression bars


def draw_regression_bars(
    bundles: list[RunBundle], palette: dict[str, str], out_dir: Path
) -> str:
    fig, ax = plt.subplots(figsize=_FIG_SIZE_WIDE, dpi=_DPI)
    machines = [b.run.machine_id for b in bundles]
    x = np.arange(len(machines), dtype=float)
    meds: list[float] = []
    lows: list[float] = []
    highs: list[float] = []
    reps_per_machine: list[list[float]] = []
    for b in bundles:
        vals = [
            r.wall_s
            for r in measured_results(b, "regression_suite")
            if r.wall_s is not None
        ]
        s = summarize(vals)
        meds.append(s.median or 0.0)
        lows.append((s.median or 0.0) - (s.q1 or s.median or 0.0))
        highs.append((s.q3 or s.median or 0.0) - (s.median or 0.0))
        reps_per_machine.append(vals)
    ax.bar(
        x,
        meds,
        0.55,
        yerr=[lows, highs],
        capsize=4,
        color=[palette[m] for m in machines],
        alpha=0.65,
        edgecolor=[palette[m] for m in machines],
        label="median (error bars = IQR)",
    )
    for xi, vals, m in zip(x, reps_per_machine, machines, strict=True):
        if vals:
            ax.scatter(
                np.full(len(vals), xi),
                vals,
                color=palette[m],
                edgecolor="#222",
                linewidth=0.4,
                s=22,
                zorder=3,
            )
    ax.set_xticks(x)
    ax.set_xticklabels(machines, rotation=20, ha="right")
    ax.set_ylabel("Wall time (s)")
    ax.set_title("Regression suite: per-rep walls (scatter) + median (bar)")
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    return _save(fig, out_dir, "regression_bars")


# ---------------------------------------------------------------- SCP plots


def _scp_points(bundle: RunBundle) -> list[tuple[int, list[float]]]:
    by_np: dict[int, list[float]] = {}
    for r in measured_results(bundle, "scp_elastic"):
        np_val = int(r.config.get("np", 0))
        if r.wall_s is not None:
            by_np.setdefault(np_val, []).append(r.wall_s)
    return sorted(by_np.items(), key=lambda kv: kv[0])


def draw_scp_walltime(
    bundles: list[RunBundle], palette: dict[str, str], out_dir: Path
) -> str:
    fig, ax = plt.subplots(figsize=_FIG_SIZE_WIDE, dpi=_DPI)
    for b in bundles:
        points = _scp_points(b)
        if not points:
            continue
        nps = [p[0] for p in points]
        meds = [summarize(p[1]).median or 0.0 for p in points]
        q1s = [summarize(p[1]).q1 or 0.0 for p in points]
        q3s = [summarize(p[1]).q3 or 0.0 for p in points]
        c = palette[b.run.machine_id]
        ax.errorbar(
            nps,
            meds,
            yerr=[np.array(meds) - np.array(q1s), np.array(q3s) - np.array(meds)],
            marker="o",
            color=c,
            ecolor=c,
            linewidth=1.4,
            capsize=3,
            label=b.run.machine_id,
        )
    ax.set_xscale("log", base=2)
    ax.set_yscale("log")
    ax.set_xlabel("MPI ranks (np)")
    ax.set_ylabel("Median wall time (s) — error bars = IQR")
    ax.set_title("SCPSpheresElastic 2D, stop_time = 0.015s")
    ax.grid(True, which="both", linestyle=":", alpha=0.4)
    ax.legend(frameon=False, loc="best")
    return _save(fig, out_dir, "scp_walltime")


def draw_scp_speedup(
    bundles: list[RunBundle], palette: dict[str, str], out_dir: Path
) -> str:
    fig, ax = plt.subplots(figsize=_FIG_SIZE_WIDE, dpi=_DPI)
    ref_max_np = 1
    for b in bundles:
        points = _scp_points(b)
        if not points:
            continue
        # baseline is the smallest np available for that machine
        nps = [p[0] for p in points]
        meds = [summarize(p[1]).median or 0.0 for p in points]
        if not meds or meds[0] <= 0:
            continue
        base = meds[0]
        speedups = [base / m if m > 0 else 0.0 for m in meds]
        c = palette[b.run.machine_id]
        ax.plot(nps, speedups, marker="o", color=c, linewidth=1.4, label=b.run.machine_id)
        ref_max_np = max(ref_max_np, *nps)
    xs = np.array([1, ref_max_np], dtype=float)
    ax.plot(xs, xs, color="#888", linestyle="--", linewidth=1.0, label="ideal (linear)")
    ax.set_xscale("log", base=2)
    ax.set_yscale("log", base=2)
    ax.set_xlabel("MPI ranks (np)")
    ax.set_ylabel("Speedup vs np=1 (median wall ratio)")
    ax.set_title("SCP strong-scaling speedup")
    ax.grid(True, which="both", linestyle=":", alpha=0.4)
    ax.legend(frameon=False, loc="best")
    return _save(fig, out_dir, "scp_speedup")


def draw_scp_efficiency(
    bundles: list[RunBundle], palette: dict[str, str], out_dir: Path
) -> str:
    fig, ax = plt.subplots(figsize=_FIG_SIZE_WIDE, dpi=_DPI)
    for b in bundles:
        points = _scp_points(b)
        if not points:
            continue
        nps = [p[0] for p in points]
        meds = [summarize(p[1]).median or 0.0 for p in points]
        if not meds or meds[0] <= 0:
            continue
        base = meds[0]
        effs = [(base / m) / n if (m > 0 and n > 0) else 0.0 for n, m in zip(nps, meds, strict=True)]
        c = palette[b.run.machine_id]
        ax.plot(nps, effs, marker="o", color=c, linewidth=1.4, label=b.run.machine_id)
    ax.axhline(1.0, color="#888", linestyle="--", linewidth=1.0, label="ideal (1.0)")
    ax.set_xscale("log", base=2)
    ax.set_xlabel("MPI ranks (np)")
    ax.set_ylabel("Parallel efficiency = speedup / np")
    ax.set_title("SCP parallel efficiency")
    ax.set_ylim(0.0, max(1.2, ax.get_ylim()[1]))
    ax.grid(True, which="both", linestyle=":", alpha=0.4)
    ax.legend(frameon=False, loc="best")
    return _save(fig, out_dir, "scp_efficiency")


def draw_scp_optimal_bars(
    bundles: list[RunBundle], palette: dict[str, str], out_dir: Path
) -> str:
    """Bar chart of each machine's optimal-np SCP wall time (linear seconds).

    For each machine: pick the ``np`` with the lowest median measured wall,
    and chart that median. Each bar is annotated on two lines: the chosen
    ``np`` and the wall multiplier relative to the fastest machine's optimal
    wall (so e.g. a ``4.13x`` label means "this machine's best is 4.13x
    slower than the fastest machine's best"). The fastest machine reads
    ``baseline``. Error bars are IQR over the reps at the optimal ``np``.
    Bundles are assumed pre-ordered (worst -> best) by the caller.
    """
    fig, ax = plt.subplots(figsize=_FIG_SIZE_WIDE, dpi=_DPI)
    machines: list[str] = []
    walls: list[float] = []
    lows: list[float] = []
    highs: list[float] = []
    np_labels: list[str] = []
    colors: list[str] = []
    for b in bundles:
        opt = optimal_scp_config(b)
        if opt is None:
            continue
        n, _med, rows = opt
        s = summarize([r.wall_s for r in rows if r.wall_s is not None])
        if s.median is None:
            continue
        machines.append(b.run.machine_id)
        walls.append(s.median)
        lows.append(s.median - (s.q1 if s.q1 is not None else s.median))
        highs.append((s.q3 if s.q3 is not None else s.median) - s.median)
        np_labels.append(f"np={n}")
        colors.append(palette[b.run.machine_id])
    if not machines:
        ax.text(0.5, 0.5, "no SCP data", ha="center", va="center")
        return _save(fig, out_dir, "scp_optimal_bars")
    fastest = min(walls)
    multipliers = [w / fastest if fastest > 0 else 0.0 for w in walls]
    x = np.arange(len(machines), dtype=float)
    ax.bar(
        x,
        walls,
        0.6,
        yerr=[lows, highs],
        capsize=4,
        color=colors,
        alpha=0.75,
        edgecolor=colors,
    )
    for xi, wall, np_label, mult in zip(x, walls, np_labels, multipliers, strict=True):
        mult_label = "baseline" if abs(mult - 1.0) < 0.005 else f"{mult:.2f}x"
        ax.annotate(
            f"{np_label}\n{mult_label}",
            xy=(xi, wall),
            xytext=(0, 8),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=9,
            color="#222",
        )
    # Leave headroom for the two-line annotations
    ax.set_ylim(0, max(walls) * 1.18)
    ax.set_xticks(x)
    ax.set_xticklabels(machines, rotation=20, ha="right")
    ax.set_ylabel("Median wall time (s) -- error bars = IQR")
    ax.set_title("SCP elastic: each machine's optimal-np median wall (linear seconds)")
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    return _save(fig, out_dir, "scp_optimal_bars")


def draw_geekbench_correlation(
    bundles: list[RunBundle],
    geekbench: GeekbenchData,
    palette: dict[str, str],
    out_dir: Path,
) -> tuple[str, dict[str, LogLogFit | None]]:
    """Two-panel scatter: SCP optimal wall vs Geekbench single-core and multi-core.

    Each panel is a log-log scatter of machines that have both an SCP
    optimal-np wall AND a Geekbench score, fitted with a least-squares
    line in log space. Machine identity is encoded in the per-point color
    (consistent with the rest of the report's palette) plus a shared
    legend below the figure -- inline per-point labels would overlap when
    two machines have similar scores. Prospective machines get a star
    marker, one distinct color per machine; the predicted SCP walls are
    shown in the accompanying table rather than inline on the plot, since
    multiple clustered prospectives would make leader-line callouts
    overlap.
    """
    fig, axes = plt.subplots(1, 2, figsize=(11.0, 6.0), dpi=_DPI)
    fits: dict[str, LogLogFit | None] = {}
    panel_specs = [
        ("single", "Geekbench single-core score"),
        ("multi", "Geekbench multi-core score"),
    ]
    # Distinct colors for prospective stars. Chosen to be visually
    # separable from the standard machine palette (which leans on tab10
    # blues / greens / earth tones); these are warm/magenta/purple tones
    # that read as "predicted" at a glance.
    prospective_colors = ["#d62728", "#9467bd", "#ff7f0e", "#e377c2", "#17becf", "#8c564b"]
    for ax_idx, (axis_key, x_label) in enumerate(panel_specs):
        ax = axes[ax_idx]
        known = _collect_known_points(bundles, geekbench, axis_key)
        scores = [p[1] for p in known]
        walls = [p[2] for p in known]
        fit = fit_loglog(scores, walls) if scores else None
        fits[axis_key] = fit

        # Scatter known machines. label= is set only on the first panel so
        # the shared fig.legend doesn't pick up duplicates.
        for machine_id, score, wall in known:
            ax.scatter(
                score,
                wall,
                color=palette.get(machine_id, "#444"),
                edgecolor="#222",
                linewidth=0.5,
                s=80,
                zorder=3,
                label=machine_id if ax_idx == 0 else None,
            )

        if fit is not None:
            x_lo, x_hi = fit.score_min, fit.score_max
            for idx, p in enumerate(geekbench.prospective):
                score_val = p.single_core if axis_key == "single" else p.multi_core
                if score_val is None or score_val <= 0:
                    continue
                pred = fit.predict(score_val)
                color = prospective_colors[idx % len(prospective_colors)]
                ax.scatter(
                    score_val,
                    pred,
                    color=color,
                    marker="*",
                    s=240,
                    edgecolor="#222",
                    linewidth=0.7,
                    zorder=5,
                    label=f"{p.cpu_label} (predicted)" if ax_idx == 0 else None,
                )
                x_lo = min(x_lo, score_val)
                x_hi = max(x_hi, score_val)
            xs_line = np.geomspace(x_lo * 0.9, x_hi * 1.1, 80)
            ys_line = [fit.predict(s) for s in xs_line]
            ax.plot(xs_line, ys_line, color="#444", linestyle="--", linewidth=1.2)
            # Fit summary box in the lower-left -- always empty space on
            # a "wall vs score" log-log plot (small score + small wall is
            # nonsensical, so no real data lives there).
            ax.text(
                0.04,
                0.06,
                f"log-log fit (n={fit.n})\nslope = {fit.slope:.2f}\n"
                f"R² = {fit.r_squared:.3f}",
                transform=ax.transAxes,
                ha="left",
                va="bottom",
                fontsize=8,
                color="#444",
                bbox={
                    "boxstyle": "round,pad=0.4",
                    "facecolor": "#ffffffd0",
                    "edgecolor": "#bbb",
                    "linewidth": 0.6,
                },
            )

        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlabel(x_label)
        ax.set_ylabel("SCP optimal-np wall (s)")
        ax.set_title(f"SCP wall vs Geekbench {axis_key}-core")
        ax.grid(True, which="both", linestyle=":", alpha=0.4)

    # Shared legend below both panels. Pulled from the first panel so the
    # ordering follows the canonical machine order (bundles arg is
    # pre-sorted worst -> best by SCP optimal wall); prospective entries
    # come after the known machines (loop order in the panel build).
    # Prospective labels can be quite long ("Mac Studio 2025 — Apple M3
    # Ultra (32 CPU) (predicted)") so we cap at 3 columns to keep each
    # column wide enough to fit the longest label without wrapping.
    handles, labels = axes[0].get_legend_handles_labels()
    n_cols = min(3, max(1, len(handles)))
    fig.legend(
        handles,
        labels,
        loc="lower center",
        ncol=n_cols,
        frameon=False,
        fontsize=8,
        bbox_to_anchor=(0.5, -0.02),
    )
    fig.suptitle("Geekbench correlation and prospective-machine prediction", fontsize=11)

    # Custom save: standard _save() doesn't leave room for the below-figure
    # legend. tight_layout packs the axes; bbox_inches='tight' on savefig
    # expands the saved bounding box to include the legend. The bottom
    # reservation (rect[1]) scales with the number of legend rows so 4
    # prospectives + 6 known machines (4 rows at 3 cols) doesn't crash
    # into the x-axis.
    n_rows = (len(handles) + n_cols - 1) // n_cols
    bottom_pad = 0.04 + 0.035 * n_rows
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "geekbench_correlation.svg"
    fig.tight_layout(rect=(0, bottom_pad, 1, 1))
    fig.savefig(target, format="svg", transparent=True, bbox_inches="tight")
    plt.close(fig)
    return "figures/geekbench_correlation.svg", fits


def _collect_known_points(
    bundles: list[RunBundle], geekbench: GeekbenchData, axis_key: str
) -> list[tuple[str, float, float]]:
    """For each bundle: (machine_id, score, scp_optimal_wall_s) if all three exist."""
    out: list[tuple[str, float, float]] = []
    for b in bundles:
        score_row = geekbench.scores.get(b.run.machine_id)
        if score_row is None:
            continue
        score = score_row.single_core if axis_key == "single" else score_row.multi_core
        if score is None or score <= 0:
            continue
        opt = optimal_scp_config(b)
        if opt is None:
            continue
        out.append((b.run.machine_id, score, opt[1]))
    return out


def draw_scp_per_machine_strip(
    bundle: RunBundle, palette: dict[str, str], out_dir: Path
) -> str:
    """Per-rep wall-time strip plot for one machine's SCP sweep."""
    fig, ax = plt.subplots(figsize=_FIG_SIZE_DEFAULT, dpi=_DPI)
    points = _scp_points(bundle)
    if not points:
        ax.text(0.5, 0.5, "no scp_elastic data", ha="center", va="center")
        return _save(fig, out_dir, f"scp_strip_{bundle.run.machine_id}")
    nps = [p[0] for p in points]
    color = palette[bundle.run.machine_id]
    xs = np.arange(len(nps), dtype=float)
    for x, (_n, vals) in zip(xs, points, strict=True):
        if vals:
            jitter = (np.random.RandomState(int(x) + 1).random(len(vals)) - 0.5) * 0.18
            ax.scatter(np.full(len(vals), x) + jitter, vals, color=color, alpha=0.75, s=28)
            s = summarize(vals)
            if s.median is not None:
                ax.plot([x - 0.22, x + 0.22], [s.median, s.median], color="#222", linewidth=1.3)
    ax.set_xticks(xs)
    ax.set_xticklabels([str(n) for n in nps])
    ax.set_xlabel("MPI ranks (np)")
    ax.set_ylabel("Wall time (s) — per-rep dots, black line = median")
    ax.set_title(f"SCP per-rep wall: {bundle.run.machine_id}")
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    return _save(fig, out_dir, f"scp_strip_{bundle.run.machine_id}")


# ---------------------------------------------------------------- render plots


def draw_render_frames_bars(
    bundles: list[RunBundle], palette: dict[str, str], out_dir: Path
) -> str:
    fig, ax = plt.subplots(figsize=_FIG_SIZE_WIDE, dpi=_DPI)
    machines = [b.run.machine_id for b in bundles]
    x = np.arange(len(machines), dtype=float)
    meds: list[float] = []
    lows: list[float] = []
    highs: list[float] = []
    reps_per_machine: list[list[float]] = []
    for b in bundles:
        vals = [
            r.wall_s
            for r in measured_results(b, "render_frames")
            if r.wall_s is not None
        ]
        s = summarize(vals)
        meds.append(s.median or 0.0)
        lows.append((s.median or 0.0) - (s.q1 or s.median or 0.0))
        highs.append((s.q3 or s.median or 0.0) - (s.median or 0.0))
        reps_per_machine.append(vals)
    ax.bar(
        x,
        meds,
        0.55,
        yerr=[lows, highs],
        capsize=4,
        color=[palette[m] for m in machines],
        alpha=0.65,
    )
    for xi, vals, m in zip(x, reps_per_machine, machines, strict=True):
        if vals:
            ax.scatter(
                np.full(len(vals), xi),
                vals,
                color=palette[m],
                edgecolor="#222",
                linewidth=0.4,
                s=22,
                zorder=3,
            )
    ax.set_xticks(x)
    ax.set_xticklabels(machines, rotation=20, ha="right")
    ax.set_ylabel("Wall time (s) — error bars = IQR")
    ax.set_title("render_frames: yt SlicePlot per plotfile (one PNG per call)")
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    return _save(fig, out_dir, "render_frames_bars")


def draw_render_encode_grouped(
    bundles: list[RunBundle], palette: dict[str, str], out_dir: Path
) -> str:
    fig, ax = plt.subplots(figsize=_FIG_SIZE_WIDE, dpi=_DPI)
    codecs = ["gifski", "h265", "av1"]
    machines = [b.run.machine_id for b in bundles]
    n_machines = len(machines)
    bar_w = 0.8 / n_machines
    for i, b in enumerate(bundles):
        meds = []
        lows = []
        highs = []
        for codec in codecs:
            vals = [
                r.wall_s
                for r in measured_results(b, "render_encode")
                if r.config.get("codec") == codec and r.wall_s is not None
            ]
            s = summarize(vals)
            meds.append(s.median or 0.0)
            lows.append((s.median or 0.0) - (s.q1 or s.median or 0.0))
            highs.append((s.q3 or s.median or 0.0) - (s.median or 0.0))
        x = np.arange(len(codecs), dtype=float) + (i - (n_machines - 1) / 2) * bar_w
        ax.bar(
            x,
            meds,
            bar_w * 0.95,
            yerr=[lows, highs],
            capsize=2,
            color=palette[b.run.machine_id],
            alpha=0.75,
            label=b.run.machine_id,
        )
    ax.set_xticks(np.arange(len(codecs), dtype=float))
    ax.set_xticklabels(codecs)
    ax.set_ylabel("Median wall time (s) — error bars = IQR")
    ax.set_title("render_encode: same input frames, three codecs")
    ax.legend(frameon=False, ncol=min(4, n_machines))
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    return _save(fig, out_dir, "render_encode_grouped")


# ---------------------------------------------------------------- telemetry


def _downsample_series(
    samples: list[TelemetrySample], attr: str, max_points: int = 1500
) -> tuple[list[datetime], list[float]]:
    raw = [(s.ts, getattr(s, attr)) for s in samples if getattr(s, attr) is not None]
    if not raw:
        return [], []
    if len(raw) <= max_points:
        return [t for t, _v in raw], [v for _t, v in raw]
    # Bin by time
    times = [t.timestamp() for t, _v in raw]
    vals = [v for _t, v in raw]
    t_min, t_max = times[0], times[-1]
    if t_max <= t_min:
        return [raw[0][0]], [raw[0][1]]
    edges = np.linspace(t_min, t_max, max_points + 1)
    bin_idx = np.clip(np.searchsorted(edges, times, side="right") - 1, 0, max_points - 1)
    binned_times: list[datetime] = []
    binned_vals: list[float] = []
    buf: list[float] = []
    current = bin_idx[0]
    bin_start = edges[current]
    for v, bi in zip(vals, bin_idx, strict=True):
        if bi != current:
            if buf:
                mid = bin_start + (edges[current + 1] - bin_start) / 2
                binned_times.append(datetime.fromtimestamp(mid, tz=UTC))
                binned_vals.append(float(np.median(buf)))
            buf = []
            current = int(bi)
            bin_start = edges[current]
        buf.append(v)
    if buf:
        mid = bin_start + (edges[current + 1] - bin_start) / 2
        binned_times.append(datetime.fromtimestamp(mid, tz=UTC))
        binned_vals.append(float(np.median(buf)))
    return binned_times, binned_vals


def _add_benchmark_bands(ax: Any, bundle: RunBundle) -> None:
    """Shade x-axis spans for each measured benchmark window."""
    for bench, color in _BENCH_BAND_COLORS.items():
        window = benchmark_window(bundle, bench)
        if window is None:
            continue
        start, end = window
        ax.axvspan(start, end, color=color, alpha=0.18, linewidth=0)


def _format_time_axis(ax: Any) -> None:
    locator = mdates.AutoDateLocator()
    formatter = mdates.ConciseDateFormatter(locator)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(formatter)


def draw_power_compare(
    bundles: list[RunBundle], palette: dict[str, str], out_dir: Path
) -> str:
    """Grouped bar: average vs peak package power over the full run, per machine.

    Bundles are assumed pre-ordered (worst -> best by SCP optimal wall). Machines
    with no ``package_power_w`` samples are skipped silently and listed in the
    figcaption-driven caller note. "Average" is the mean over every captured
    1 Hz sample for the run, "peak" is the max.
    """
    fig, ax = plt.subplots(figsize=_FIG_SIZE_WIDE, dpi=_DPI)
    machines: list[str] = []
    avgs: list[float] = []
    peaks: list[float] = []
    colors: list[str] = []
    for b in bundles:
        vals = [s.package_power_w for s in b.telemetry if s.package_power_w is not None]
        if not vals:
            continue
        machines.append(b.run.machine_id)
        avgs.append(float(np.mean(vals)))
        peaks.append(float(np.max(vals)))
        colors.append(palette[b.run.machine_id])
    if not machines:
        ax.text(0.5, 0.5, "no package_power_w data", ha="center", va="center")
        return _save(fig, out_dir, "power_compare")
    x = np.arange(len(machines), dtype=float)
    width = 0.36
    ax.bar(x - width / 2, avgs, width, color=colors, alpha=0.65, label="average over full run")
    ax.bar(
        x + width / 2,
        peaks,
        width,
        color=colors,
        alpha=0.95,
        edgecolor="#222",
        linewidth=0.6,
        label="peak over full run",
    )
    for xi, v in zip(x - width / 2, avgs, strict=True):
        ax.annotate(
            f"{v:.0f}",
            xy=(xi, v),
            xytext=(0, 4),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=8,
            color="#222",
        )
    for xi, v in zip(x + width / 2, peaks, strict=True):
        ax.annotate(
            f"{v:.0f}",
            xy=(xi, v),
            xytext=(0, 4),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=8,
            color="#222",
        )
    ax.set_xticks(x)
    ax.set_xticklabels(machines, rotation=20, ha="right")
    ax.set_ylabel("Package power (W)")
    ax.set_title("Package power: average vs peak over each machine's full run")
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    ax.legend(frameon=False)
    return _save(fig, out_dir, "power_compare")


def avg_util_during_optimal(bundle: RunBundle) -> float | None:
    """Mean per-core util_pct across all samples in the optimal-np rep windows.

    "All samples" includes idle cores: on a 24-core machine running at np=12,
    half the cores read ~0% util and pull the average down toward 50%. That's
    intentional and the figcaption calls it out.
    """
    opt = optimal_scp_config(bundle)
    if opt is None:
        return None
    _n, _med, rows = opt
    windows = [(r.started_at, r.ended_at) for r in rows if r.ended_at is not None]
    if not windows:
        return None
    vals: list[float] = []
    for s in bundle.per_core:
        if s.util_pct is None:
            continue
        for start, end in windows:
            if start <= s.ts <= end:
                vals.append(s.util_pct)
                break
    if not vals:
        return None
    return float(np.mean(vals))


def draw_optimal_util_compare(
    bundles: list[RunBundle], palette: dict[str, str], out_dir: Path
) -> str:
    """Average per-core CPU utilization during each machine's optimal SCP reps.

    Bundles pre-ordered worst -> best. The metric averages every per-core
    ``util_pct`` sample whose timestamp falls inside any measured rep at the
    machine's optimal ``np``; idle cores count toward the mean.
    """
    fig, ax = plt.subplots(figsize=_FIG_SIZE_WIDE, dpi=_DPI)
    machines: list[str] = []
    avgs: list[float] = []
    np_labels: list[str] = []
    colors: list[str] = []
    for b in bundles:
        opt = optimal_scp_config(b)
        avg = avg_util_during_optimal(b)
        if opt is None or avg is None:
            continue
        machines.append(b.run.machine_id)
        avgs.append(avg)
        np_labels.append(f"np={opt[0]}, cores={b.host.cores_physical}+{b.host.cores_virtual}v")
        colors.append(palette[b.run.machine_id])
    if not machines:
        ax.text(0.5, 0.5, "no per-core util data during optimal SCP", ha="center", va="center")
        return _save(fig, out_dir, "optimal_util_compare")
    x = np.arange(len(machines), dtype=float)
    ax.bar(x, avgs, 0.6, color=colors, alpha=0.8, edgecolor=colors)
    for xi, v, lbl in zip(x, avgs, np_labels, strict=True):
        ax.annotate(
            f"{v:.1f}%",
            xy=(xi, v),
            xytext=(0, 6),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=9,
            color="#222",
        )
        ax.annotate(
            lbl,
            xy=(xi, 0),
            xytext=(0, -22),
            textcoords="offset points",
            ha="center",
            va="top",
            fontsize=7,
            color="#555",
        )
    ax.set_xticks(x)
    ax.set_xticklabels(machines, rotation=20, ha="right")
    ax.set_ylabel("Average per-core utilization (%) -- idle cores included")
    ax.set_title("Average CPU utilization during each machine's optimal-np SCP reps")
    ax.set_ylim(0, max(100.0, *avgs) * 1.08)
    ax.axhline(100.0, color="#aaa", linewidth=0.8, linestyle=":")
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    return _save(fig, out_dir, "optimal_util_compare")


def draw_telemetry_overview(
    bundle: RunBundle, out_dir: Path
) -> dict[str, str | None]:
    """Per-machine package power + freq time series for the full run.

    Returns a dict of {channel_name: figure_path or None}. ``None`` means the
    channel had no data on this machine — caller skips rendering that panel.
    """
    out: dict[str, str | None] = {}
    name_stub = bundle.run.machine_id

    # Package power
    if bundle.telemetry_channels_available.get("package_power_w"):
        fig, ax = plt.subplots(figsize=_FIG_SIZE_WIDE, dpi=_DPI)
        ts, vals = _downsample_series(bundle.telemetry, "package_power_w")
        if ts:
            ax.plot(ts, vals, color="#d6604d", linewidth=1.1)  # pyright: ignore[reportArgumentType]
        _add_benchmark_bands(ax, bundle)
        _format_time_axis(ax)
        ax.set_ylabel("Package power (W)")
        ax.set_title(f"{name_stub} — package power over run")
        ax.grid(axis="y", linestyle=":", alpha=0.4)
        out["package_power_w"] = _save(fig, out_dir, f"telem_power_{name_stub}")
    else:
        out["package_power_w"] = None

    # Avg CPU frequency
    if bundle.telemetry_channels_available.get("cpu_freq_avg_mhz"):
        fig, ax = plt.subplots(figsize=_FIG_SIZE_WIDE, dpi=_DPI)
        ts_avg, v_avg = _downsample_series(bundle.telemetry, "cpu_freq_avg_mhz")
        ts_max, v_max = _downsample_series(bundle.telemetry, "cpu_freq_max_mhz")
        if ts_avg:
            ax.plot(ts_avg, v_avg, color="#1f77b4", linewidth=1.0, label="avg core MHz")  # pyright: ignore[reportArgumentType]
        if ts_max:
            ax.plot(ts_max, v_max, color="#ff7f0e", linewidth=1.0, alpha=0.7, label="max core MHz")  # pyright: ignore[reportArgumentType]
        _add_benchmark_bands(ax, bundle)
        _format_time_axis(ax)
        ax.set_ylabel("Frequency (MHz)")
        ax.set_title(f"{name_stub} — CPU frequency over run")
        ax.grid(axis="y", linestyle=":", alpha=0.4)
        ax.legend(frameon=False)
        out["cpu_freq_avg_mhz"] = _save(fig, out_dir, f"telem_freq_{name_stub}")
    else:
        out["cpu_freq_avg_mhz"] = None

    # Package temp (Linux only on current data)
    if bundle.telemetry_channels_available.get("pkg_temp_c"):
        fig, ax = plt.subplots(figsize=_FIG_SIZE_WIDE, dpi=_DPI)
        ts, vals = _downsample_series(bundle.telemetry, "pkg_temp_c")
        if ts:
            ax.plot(ts, vals, color="#e6550d", linewidth=1.0)  # pyright: ignore[reportArgumentType]
        _add_benchmark_bands(ax, bundle)
        _format_time_axis(ax)
        ax.set_ylabel("Package temp (°C)")
        ax.set_title(f"{name_stub} — package temperature over run")
        ax.grid(axis="y", linestyle=":", alpha=0.4)
        out["pkg_temp_c"] = _save(fig, out_dir, f"telem_temp_{name_stub}")
    else:
        out["pkg_temp_c"] = None
    return out


def _per_core_matrix(
    per_core: list[PerCoreSample],
    attr: str,
    *,
    max_time_bins: int = 600,
) -> tuple[np.ndarray, list[float], list[int], list[str]]:
    """Build a (cores x time_bins) matrix for one per-core attribute.

    Returns (matrix, time_seconds_from_start, sorted_core_indices, core_types).
    NaN cells indicate the underlying sample was missing.
    """
    rows = [s for s in per_core if getattr(s, attr) is not None]
    if not rows:
        return np.empty((0, 0)), [], [], []
    cores = sorted({s.core_index for s in rows})
    core_type_map: dict[int, str] = {}
    for s in rows:
        core_type_map.setdefault(s.core_index, s.core_type)
    # Order cores by (core_type-rank, core_index) so the heatmap groups types
    type_rank = {"performance": 0, "super": -1, "physical": 0, "efficiency": 1, "virtual": 1}
    cores.sort(key=lambda i: (type_rank.get(core_type_map.get(i, ""), 9), i))
    times = sorted({s.ts for s in rows})
    t0 = times[0]
    t_seconds = [(t - t0).total_seconds() for t in times]
    if len(times) <= max_time_bins:
        bin_assignment = {t: i for i, t in enumerate(times)}
        binned_seconds = t_seconds
    else:
        edges = np.linspace(t_seconds[0], t_seconds[-1], max_time_bins + 1)
        bin_assignment = {}
        binned_seconds = []
        for t, ts_s in zip(times, t_seconds, strict=True):
            bi = int(min(max_time_bins - 1, max(0, np.searchsorted(edges, ts_s, side="right") - 1)))
            bin_assignment[t] = bi
        for k in range(max_time_bins):
            binned_seconds.append(float((edges[k] + edges[k + 1]) / 2.0))
    n_cores = len(cores)
    n_bins = len(binned_seconds)
    # Accumulator: list of values per (core_row, bin)
    accum: dict[tuple[int, int], list[float]] = {}
    core_row = {ci: r for r, ci in enumerate(cores)}
    for s in rows:
        r = core_row[s.core_index]
        b = bin_assignment[s.ts]
        accum.setdefault((r, b), []).append(float(getattr(s, attr)))
    matrix = np.full((n_cores, n_bins), np.nan, dtype=float)
    for (r, b), vals in accum.items():
        matrix[r, b] = float(np.median(vals))
    return matrix, binned_seconds, cores, [core_type_map.get(i, "") for i in cores]


def draw_per_core_freq_heatmap(
    bundle: RunBundle, out_dir: Path
) -> str | None:
    if not bundle.telemetry_channels_available.get("per_core_freq_mhz"):
        return None
    matrix, times_s, cores, types = _per_core_matrix(bundle.per_core, "freq_mhz")
    if matrix.size == 0:
        return None
    fig, ax = plt.subplots(figsize=_FIG_SIZE_WIDE, dpi=_DPI)
    im = ax.imshow(
        matrix,
        aspect="auto",
        interpolation="nearest",
        cmap="viridis",
        extent=(times_s[0], times_s[-1], len(cores) - 0.5, -0.5),
    )
    ax.set_yticks(np.arange(len(cores)))
    ax.set_yticklabels([f"core {ci}\n{t}" for ci, t in zip(cores, types, strict=True)], fontsize=7)
    ax.set_xlabel("Time since run start (s)")
    ax.set_title(f"{bundle.run.machine_id} — per-core frequency (MHz)")
    cb = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
    cb.set_label("MHz")
    return _save(fig, out_dir, f"telem_percore_freq_{bundle.run.machine_id}")


def draw_per_core_util_heatmap(
    bundle: RunBundle, out_dir: Path
) -> str | None:
    if not bundle.telemetry_channels_available.get("per_core_util_pct"):
        return None
    matrix, times_s, cores, types = _per_core_matrix(bundle.per_core, "util_pct")
    if matrix.size == 0:
        return None
    fig, ax = plt.subplots(figsize=_FIG_SIZE_WIDE, dpi=_DPI)
    im = ax.imshow(
        matrix,
        aspect="auto",
        interpolation="nearest",
        cmap="magma",
        vmin=0,
        vmax=100,
        extent=(times_s[0], times_s[-1], len(cores) - 0.5, -0.5),
    )
    ax.set_yticks(np.arange(len(cores)))
    ax.set_yticklabels([f"core {ci}\n{t}" for ci, t in zip(cores, types, strict=True)], fontsize=7)
    ax.set_xlabel("Time since run start (s)")
    ax.set_title(f"{bundle.run.machine_id} — per-core utilization (%)")
    cb = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
    cb.set_label("% util")
    return _save(fig, out_dir, f"telem_percore_util_{bundle.run.machine_id}")


def draw_per_core_temp_heatmap(
    bundle: RunBundle, out_dir: Path
) -> str | None:
    if not bundle.telemetry_channels_available.get("per_core_temp_c"):
        return None
    matrix, times_s, cores, types = _per_core_matrix(bundle.per_core, "temp_c")
    if matrix.size == 0:
        return None
    fig, ax = plt.subplots(figsize=_FIG_SIZE_WIDE, dpi=_DPI)
    im = ax.imshow(
        matrix,
        aspect="auto",
        interpolation="nearest",
        cmap="inferno",
        extent=(times_s[0], times_s[-1], len(cores) - 0.5, -0.5),
    )
    ax.set_yticks(np.arange(len(cores)))
    ax.set_yticklabels([f"core {ci}\n{t}" for ci, t in zip(cores, types, strict=True)], fontsize=7)
    ax.set_xlabel("Time since run start (s)")
    ax.set_title(f"{bundle.run.machine_id} — per-core temperature (°C)")
    cb = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
    cb.set_label("°C")
    return _save(fig, out_dir, f"telem_percore_temp_{bundle.run.machine_id}")


def draw_scp_zoom_freq(
    bundle: RunBundle, out_dir: Path
) -> str | None:
    """Per-core frequency time series, zoomed to the optimal-np SCP rep window.

    "Optimal" = the ``np`` with the smallest median measured wall (see
    :func:`optimal_scp_config`). Within that bucket, the longest individual
    rep is selected as the zoom target so the time series is as wide as
    possible for inspection.
    """
    if not bundle.telemetry_channels_available.get("per_core_freq_mhz"):
        return None
    opt = optimal_scp_config(bundle)
    if opt is None:
        return None
    opt_np, _med, opt_rows = opt
    candidates = [r for r in opt_rows if r.ended_at is not None]
    if not candidates:
        return None
    target: ResultRow = max(candidates, key=lambda r: r.wall_s or 0.0)
    start = target.started_at
    end = target.ended_at or target.started_at
    pad = timedelta(seconds=5)
    fig, ax = plt.subplots(figsize=_FIG_SIZE_WIDE, dpi=_DPI)
    cores: dict[int, list[tuple[datetime, float]]] = {}
    core_types: dict[int, str] = {}
    for s in bundle.per_core:
        if s.freq_mhz is None:
            continue
        if not (start - pad <= s.ts <= end + pad):
            continue
        cores.setdefault(s.core_index, []).append((s.ts, s.freq_mhz))
        core_types.setdefault(s.core_index, s.core_type)
    if not cores:
        plt.close(fig)
        return None
    type_color = {
        "performance": "#1f77b4",
        "super": "#1f77b4",
        "physical": "#1f77b4",
        "efficiency": "#ff7f0e",
        "virtual": "#ff7f0e",
    }
    for ci in sorted(cores):
        pts = sorted(cores[ci])
        if not pts:
            continue
        t = [p[0] for p in pts]
        v = [p[1] for p in pts]
        ax.plot(
            t,  # pyright: ignore[reportArgumentType]
            v,
            color=type_color.get(core_types.get(ci, ""), "#666"),
            linewidth=0.7,
            alpha=0.65,
            label=core_types.get(ci, "") if ci == sorted(cores)[0] else None,
        )
    # Single legend entries per type
    seen: set[str] = set()
    handles: list[Any] = []
    labels: list[str] = []
    for ci in sorted(cores):
        t = core_types.get(ci, "")
        if t in seen:
            continue
        seen.add(t)
        handles.append(Line2D([0], [0], color=type_color.get(t, "#666"), linewidth=1.5))
        labels.append(t)
    _overlay_pkg_temp(ax, bundle, start - pad, end + pad, handles, labels)
    ax.legend(handles, labels, frameon=False, loc="best")
    _format_time_axis(ax)
    ax.set_ylabel("Frequency (MHz)")
    ax.set_title(
        f"{bundle.run.machine_id} -- per-core MHz, SCP optimal np={opt_np} "
        f"(longest measured rep, wall={target.wall_s:.1f}s)"
        if target.wall_s
        else f"{bundle.run.machine_id} -- per-core MHz, SCP optimal np={opt_np}"
    )
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    return _save(fig, out_dir, f"telem_scpzoom_freq_{bundle.run.machine_id}")


def _overlay_pkg_temp(
    ax: Any,
    bundle: RunBundle,
    window_start: datetime,
    window_end: datetime,
    handles: list[Any],
    labels: list[str],
) -> None:
    """Overlay pkg_temp_c on a twin y-axis if the run captured it.

    turbostat (Linux) reports pkg_temp_c at 1 Hz; powermetrics (macOS) does
    not surface a package-temp channel, so the overlay is skipped silently
    on Darwin runs and the figcaption flags it.
    """
    temp_pts = sorted(
        (s.ts, s.pkg_temp_c)
        for s in bundle.telemetry
        if s.pkg_temp_c is not None and window_start <= s.ts <= window_end
    )
    if not temp_pts:
        return
    ax_temp = ax.twinx()
    t_temp = [p[0] for p in temp_pts]
    v_temp = [p[1] for p in temp_pts]
    ax_temp.plot(
        t_temp,  # pyright: ignore[reportArgumentType]
        v_temp,
        color="#d62728",
        linewidth=1.5,
        linestyle="--",
        alpha=0.85,
    )
    ax_temp.set_ylabel("Package temp (°C)", color="#d62728")
    ax_temp.tick_params(axis="y", labelcolor="#d62728")
    handles.append(Line2D([0], [0], color="#d62728", linewidth=1.5, linestyle="--"))
    labels.append("pkg temp (°C)")


def annotate_unavailable_machines(
    bundles: list[RunBundle], attr: str
) -> list[str]:
    """Return machine_ids that have no data for a given telemetry attribute.

    Used by render.py to add a small footer to combined-telemetry sections
    noting which machines were excluded from the figure for lack of data.
    """
    out: list[str] = []
    for b in bundles:
        if attr == "package_power_w":
            has = any(s.package_power_w is not None for s in b.telemetry)
        elif attr == "per_core_util_pct":
            has = any(s.util_pct is not None for s in b.per_core)
        else:
            has = False
        if not has:
            out.append(b.run.machine_id)
    return out
