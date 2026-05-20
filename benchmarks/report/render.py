"""Top-level orchestration: bundles → plot files + Jinja2 template variables.

The single ``build_report`` entry point opens every per-machine DB, draws
every figure, and writes ``index.html`` next to a ``figures/`` directory.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from benchmarks.report import plots
from benchmarks.report.data import (
    RunBundle,
    benchmark_window,
    discovered_dbs_to_bundles,
    manifest_path_relative,
    measured_results,
    optimal_scp_config,
    order_by_scp_optimal,
)
from benchmarks.report.discover import discover_runs
from benchmarks.report.geekbench import GeekbenchData, LogLogFit, load_geekbench
from benchmarks.report.stats import percentile, summarize

LOG = logging.getLogger("alamo-benchmark.report")

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_GEEKBENCH_TOML = Path(__file__).parent / "geekbench.toml"

_BENCHMARK_ORDER = [
    "noise_floor",
    "compile_serial",
    "compile_parallel",
    "regression_suite",
    "scp_elastic",
    "render_frames",
    "render_encode",
]


@dataclass(frozen=True)
class ReportPaths:
    out_dir: Path
    figures_dir: Path
    index_html: Path


def build_report(results_root: Path, out_dir: Path) -> ReportPaths:
    paths = ReportPaths(
        out_dir=out_dir,
        figures_dir=out_dir / "figures",
        index_html=out_dir / "index.html",
    )
    paths.figures_dir.mkdir(parents=True, exist_ok=True)

    discovered = discover_runs(results_root)
    if not discovered:
        raise RuntimeError(f"No machine result directories found under {results_root}")
    LOG.info("Discovered %d per-machine run(s)", len(discovered))
    bundles = discovered_dbs_to_bundles(discovered)
    # Canonical machine order for the entire report: slowest optimal-np SCP wall
    # first, fastest last. Used for every cross-machine plot AND every per-machine
    # subsection so the reader sees a consistent left->right / top->bottom progression.
    bundles = order_by_scp_optimal(bundles)
    palette = plots.palette_for_machines([b.run.machine_id for b in bundles])

    geekbench = load_geekbench(_GEEKBENCH_TOML)
    LOG.info(
        "Loaded Geekbench data: %d scored machine(s), %d prospective machine(s)",
        len(geekbench.scores),
        len(geekbench.prospective),
    )

    LOG.info("Drawing aggregate figures")
    figs = _draw_aggregate_figures(bundles, palette, paths.figures_dir)

    LOG.info("Drawing Geekbench correlation")
    figs["geekbench"], geekbench_fits = plots.draw_geekbench_correlation(
        bundles, geekbench, palette, paths.figures_dir
    )

    LOG.info("Drawing per-machine figures")
    per_machine = _draw_per_machine_figures(bundles, palette, paths.figures_dir)

    LOG.info("Computing tables")
    ctx = _build_template_context(
        bundles=bundles,
        palette=palette,
        results_root=results_root,
        figs=figs,
        per_machine=per_machine,
        geekbench=geekbench,
        geekbench_fits=geekbench_fits,
    )

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["fmt_seconds"] = _fmt_seconds
    env.filters["fmt_float"] = _fmt_float
    env.filters["fmt_pct"] = _fmt_pct
    env.filters["fmt_dt"] = _fmt_dt
    env.filters["fmt_dur"] = _fmt_duration
    env.filters["fmt_bytes"] = _fmt_bytes
    template = env.get_template("report.html.j2")
    paths.index_html.write_text(template.render(**ctx), encoding="utf-8")
    LOG.info("Report written to %s", paths.index_html)
    return paths


# ---------------------------------------------------------------- figure groups


def _draw_aggregate_figures(
    bundles: list[RunBundle], palette: dict[str, str], figures_dir: Path
) -> dict[str, str]:
    return {
        "noise_floor": plots.draw_noise_floor_box(bundles, palette, figures_dir),
        "compile": plots.draw_compile_bars(bundles, palette, figures_dir),
        "regression": plots.draw_regression_bars(bundles, palette, figures_dir),
        "scp_wall": plots.draw_scp_walltime(bundles, palette, figures_dir),
        "scp_speedup": plots.draw_scp_speedup(bundles, palette, figures_dir),
        "scp_efficiency": plots.draw_scp_efficiency(bundles, palette, figures_dir),
        "scp_optimal": plots.draw_scp_optimal_bars(bundles, palette, figures_dir),
        "render_frames": plots.draw_render_frames_bars(bundles, palette, figures_dir),
        "render_encode": plots.draw_render_encode_grouped(bundles, palette, figures_dir),
        "power_compare": plots.draw_power_compare(bundles, palette, figures_dir),
        "optimal_util": plots.draw_optimal_util_compare(bundles, palette, figures_dir),
    }


def _draw_per_machine_figures(
    bundles: list[RunBundle], palette: dict[str, str], figures_dir: Path
) -> dict[str, dict[str, str | None]]:
    out: dict[str, dict[str, str | None]] = {}
    for b in bundles:
        block: dict[str, str | None] = {
            "noise_floor": plots.draw_noise_floor_strip(b, palette, figures_dir),
            "scp_strip": plots.draw_scp_per_machine_strip(b, palette, figures_dir),
        }
        block.update(plots.draw_telemetry_overview(b, figures_dir))
        block["per_core_freq"] = plots.draw_per_core_freq_heatmap(b, figures_dir)
        block["per_core_util"] = plots.draw_per_core_util_heatmap(b, figures_dir)
        block["per_core_temp"] = plots.draw_per_core_temp_heatmap(b, figures_dir)
        block["scp_zoom_freq"] = plots.draw_scp_zoom_freq(b, figures_dir)
        out[b.run.machine_id] = block
    return out


# ---------------------------------------------------------------- template context


def _build_template_context(
    *,
    bundles: list[RunBundle],
    palette: dict[str, str],
    results_root: Path,
    figs: dict[str, str],
    per_machine: dict[str, dict[str, str | None]],
    geekbench: GeekbenchData,
    geekbench_fits: dict[str, LogLogFit | None],
) -> dict[str, Any]:
    machine_ids = [b.run.machine_id for b in bundles]
    inventory = [_inventory_row(b, results_root) for b in bundles]
    tool_keys = _stable_tool_keys(bundles)
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "machine_ids": machine_ids,
        "palette": palette,
        "bundles": bundles,
        "inventory": inventory,
        "tool_keys": tool_keys,
        "preflight_rows": [_preflight_rows(b) for b in bundles],
        "noise_floor_table": [_noise_floor_row(b) for b in bundles],
        "noise_floor_tabs": _noise_floor_tabs(bundles, per_machine),
        "compile_table": [_compile_row(b) for b in bundles],
        "regression_table": [_regression_row(b) for b in bundles],
        "scp_sweep_tables": [_scp_sweep_table(b) for b in bundles],
        "scp_hash_tables": [_scp_hash_table(b) for b in bundles],
        "scp_optimal_table": _scp_optimal_table(bundles),
        "render_frames_table": [_render_frames_row(b) for b in bundles],
        "render_encode_table": [_render_encode_table(b) for b in bundles],
        "telemetry_table": _telemetry_summary_table(bundles),
        "telemetry_channels": [_telemetry_channels_row(b) for b in bundles],
        "telemetry_combined_table": _telemetry_combined_table(bundles),
        "appendix_rows": [_appendix_row(b, results_root) for b in bundles],
        "figs": figs,
        "per_machine": per_machine,
        "macos_offset_machines": [
            b.run.machine_id for b in bundles if b.telemetry_offset_seconds != 0
        ],
        "regression_skip_count": _regression_skip_count(bundles),
        "power_compare_missing": plots.annotate_unavailable_machines(bundles, "package_power_w"),
        "optimal_util_missing": plots.annotate_unavailable_machines(bundles, "per_core_util_pct"),
        "geekbench_known": _geekbench_known_table(bundles, geekbench, geekbench_fits),
        "geekbench_prospective": _geekbench_prospective_table(geekbench, geekbench_fits),
        "geekbench_fits": geekbench_fits,
    }


# ---------------------------------------------------------------- table builders


def _inventory_row(b: RunBundle, results_root: Path) -> dict[str, Any]:
    run = b.run
    return {
        "machine_id": run.machine_id,
        "hostname": run.hostname,
        "os": _os_display(b),
        "kernel": b.host.kernel,
        "cpu": b.host.cpu_brand,
        "cores_physical": b.host.cores_physical,
        "cores_virtual": b.host.cores_virtual,
        "ram_gb": b.host.ram_gb,
        "started_at": run.started_at,
        "duration_seconds": (run.ended_at - run.started_at).total_seconds()
        if run.ended_at
        else None,
        "benchmark_sha": run.benchmark_repo_sha,
        "benchmark_dirty": run.benchmark_repo_dirty,
        "alamo_sha": run.alamo_repo_sha,
        "alamo_dirty": run.alamo_repo_dirty,
        "config_mode": run.config.get("run", {}).get("mode", "?"),
        "schema_version": run.schema_version,
        "run_dir": manifest_path_relative(b, results_root),
    }


def _os_display(b: RunBundle) -> str:
    # On macOS the `os_version` field holds a clean string like "macOS 26.5".
    # On Linux the field holds the uname `version` (an SMP banner several lines
    # long), so we fall back to "Linux <kernel>" instead.
    if b.host.os_name == "Darwin":
        return b.host.os_version or b.host.os_name
    if b.host.os_name == "Linux":
        return f"Linux {b.host.kernel}".strip()
    return f"{b.host.os_name} {b.host.os_version}".strip()


def _stable_tool_keys(bundles: list[RunBundle]) -> list[str]:
    keys: set[str] = set()
    for b in bundles:
        keys.update(b.host.tool_versions.keys())
    return sorted(keys)


def _preflight_rows(b: RunBundle) -> dict[str, Any]:
    preflight = b.host.preflight or {}
    return {
        "machine_id": b.run.machine_id,
        "passed": bool(preflight.get("passed", True)),
        "checks": preflight.get("checks", []),
    }


def _noise_floor_row(b: RunBundle) -> dict[str, Any]:
    vals = [r.wall_s for r in measured_results(b, "noise_floor") if r.wall_s is not None]
    s = summarize(vals)
    return {
        "machine_id": b.run.machine_id,
        "n": s.n,
        "median_s": s.median,
        "iqr_s": s.iqr,
        "min_s": s.minimum,
        "max_s": s.maximum,
        "stdev_s": s.stdev,
    }


def _compile_row(b: RunBundle) -> dict[str, Any]:
    serial = summarize(
        [r.wall_s for r in measured_results(b, "compile_serial") if r.wall_s is not None]
    )
    parallel = summarize(
        [r.wall_s for r in measured_results(b, "compile_parallel") if r.wall_s is not None]
    )
    speedup = None
    if serial.median and parallel.median and parallel.median > 0:
        speedup = serial.median / parallel.median
    # Look up `j` from any config row
    j_value: int | None = None
    for r in measured_results(b, "compile_parallel"):
        j = r.config.get("j")
        if isinstance(j, int):
            j_value = j
            break
    return {
        "machine_id": b.run.machine_id,
        "serial_median_s": serial.median,
        "serial_iqr_s": serial.iqr,
        "serial_n": serial.n,
        "parallel_median_s": parallel.median,
        "parallel_iqr_s": parallel.iqr,
        "parallel_n": parallel.n,
        "j": j_value,
        "speedup": speedup,
    }


def _regression_row(b: RunBundle) -> dict[str, Any]:
    vals = [r.wall_s for r in measured_results(b, "regression_suite") if r.wall_s is not None]
    s = summarize(vals)
    notes = ", ".join({r.notes for r in measured_results(b, "regression_suite") if r.notes})
    return {
        "machine_id": b.run.machine_id,
        "n": s.n,
        "median_s": s.median,
        "iqr_s": s.iqr,
        "min_s": s.minimum,
        "max_s": s.maximum,
        "notes": notes or "—",
    }


def _noise_floor_tabs(
    bundles: list[RunBundle], per_machine: dict[str, dict[str, str | None]]
) -> list[dict[str, Any]]:
    """One entry per machine for the noise-floor tab bar."""
    return [
        {
            "machine_id": b.run.machine_id,
            "fig": per_machine.get(b.run.machine_id, {}).get("noise_floor"),
        }
        for b in bundles
    ]


def _scp_optimal_table(bundles: list[RunBundle]) -> list[dict[str, Any]]:
    """Per-machine optimal-np walls plus multiplier vs the fleet's fastest machine."""
    rows: list[dict[str, Any]] = []
    for b in bundles:
        opt = optimal_scp_config(b)
        if opt is None:
            rows.append(
                {
                    "machine_id": b.run.machine_id,
                    "optimal_np": None,
                    "median_s": None,
                    "iqr_s": None,
                    "min_s": None,
                    "max_s": None,
                    "reps": 0,
                    "multiplier": None,
                    "pct_slower": None,
                }
            )
            continue
        n, _med, opt_rows = opt
        s = summarize([r.wall_s for r in opt_rows if r.wall_s is not None])
        rows.append(
            {
                "machine_id": b.run.machine_id,
                "optimal_np": n,
                "median_s": s.median,
                "iqr_s": s.iqr,
                "min_s": s.minimum,
                "max_s": s.maximum,
                "reps": s.n,
                "multiplier": None,  # filled below
                "pct_slower": None,  # filled below
            }
        )
    # Fill multiplier + percent-slower against the fastest measured median wall
    walls = [r["median_s"] for r in rows if r["median_s"] is not None]
    if walls:
        fastest = min(walls)
        for r in rows:
            m = r["median_s"]
            if m is None or fastest <= 0:
                continue
            r["multiplier"] = m / fastest
            r["pct_slower"] = (m - fastest) / fastest * 100.0
    return rows


def _scp_sweep_table(b: RunBundle) -> dict[str, Any]:
    by_np: dict[int, list[float]] = defaultdict(list)
    for r in measured_results(b, "scp_elastic"):
        if r.wall_s is not None:
            by_np[int(r.config.get("np", 0))].append(r.wall_s)
    rows: list[dict[str, Any]] = []
    sorted_nps = sorted(by_np.keys())
    base_median = None
    if sorted_nps:
        base_summary = summarize(by_np[sorted_nps[0]])
        base_median = base_summary.median
    for n in sorted_nps:
        s = summarize(by_np[n])
        speedup = None
        eff = None
        if base_median and s.median and s.median > 0:
            speedup = base_median / s.median
            eff = speedup / n if n > 0 else None
        rows.append(
            {
                "np": n,
                "reps": s.n,
                "median_s": s.median,
                "iqr_s": s.iqr,
                "min_s": s.minimum,
                "max_s": s.maximum,
                "stdev_s": s.stdev,
                "speedup": speedup,
                "efficiency": eff,
            }
        )
    return {"machine_id": b.run.machine_id, "rows": rows}


def _scp_hash_table(b: RunBundle) -> dict[str, Any]:
    by_np: dict[int, list[str]] = defaultdict(list)
    for r in measured_results(b, "scp_elastic"):
        if r.output_hash:
            by_np[int(r.config.get("np", 0))].append(r.output_hash)
    rows: list[dict[str, Any]] = []
    for n in sorted(by_np.keys()):
        hashes = by_np[n]
        distinct = sorted(set(hashes))
        rows.append(
            {
                "np": n,
                "reps": len(hashes),
                "distinct": len(distinct),
                "hashes": [h[:12] for h in distinct],
            }
        )
    return {"machine_id": b.run.machine_id, "rows": rows}


def _render_frames_row(b: RunBundle) -> dict[str, Any]:
    vals = [r.wall_s for r in measured_results(b, "render_frames") if r.wall_s is not None]
    s = summarize(vals)
    return {
        "machine_id": b.run.machine_id,
        "n": s.n,
        "median_s": s.median,
        "iqr_s": s.iqr,
        "min_s": s.minimum,
        "max_s": s.maximum,
    }


def _render_encode_table(b: RunBundle) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for codec in ["gifski", "h265", "av1"]:
        vals = [
            r.wall_s
            for r in measured_results(b, "render_encode")
            if r.config.get("codec") == codec and r.wall_s is not None
        ]
        s = summarize(vals)
        rows.append(
            {
                "codec": codec,
                "n": s.n,
                "median_s": s.median,
                "iqr_s": s.iqr,
                "min_s": s.minimum,
                "max_s": s.maximum,
            }
        )
    return {"machine_id": b.run.machine_id, "rows": rows}


def _telemetry_combined_table(bundles: list[RunBundle]) -> list[dict[str, Any]]:
    """Cross-machine combined-telemetry summary: power + util-during-optimal-SCP.

    Lives alongside the new combined plots so the reader can scan the numbers
    underneath without expanding the per-machine drill-downs.
    """
    rows: list[dict[str, Any]] = []
    for b in bundles:
        power_vals = [s.package_power_w for s in b.telemetry if s.package_power_w is not None]
        avg_power = float(sum(power_vals) / len(power_vals)) if power_vals else None
        peak_power = max(power_vals) if power_vals else None
        opt = optimal_scp_config(b)
        opt_np = opt[0] if opt else None
        opt_wall = opt[1] if opt else None
        util_during_opt = plots.avg_util_during_optimal(b)
        rows.append(
            {
                "machine_id": b.run.machine_id,
                "avg_power_w": avg_power,
                "peak_power_w": peak_power,
                "optimal_np": opt_np,
                "optimal_wall_s": opt_wall,
                "avg_util_optimal_pct": util_during_opt,
                "cores_physical": b.host.cores_physical,
                "cores_virtual": b.host.cores_virtual,
            }
        )
    return rows


def _telemetry_summary_table(bundles: list[RunBundle]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for b in bundles:
        per_bench: list[dict[str, Any]] = []
        for bench in _BENCHMARK_ORDER:
            window = benchmark_window(b, bench)
            if window is None:
                per_bench.append({"benchmark": bench, "samples": 0})
                continue
            start, end = window
            window_samples = [s for s in b.telemetry if start <= s.ts <= end]
            per_core_window = [s for s in b.per_core if start <= s.ts <= end]
            per_bench.append(
                {
                    "benchmark": bench,
                    "samples": len(window_samples),
                    "median_power_w": _median(
                        [s.package_power_w for s in window_samples if s.package_power_w is not None]
                    ),
                    "p95_freq_mhz": percentile(
                        [s.cpu_freq_avg_mhz for s in window_samples if s.cpu_freq_avg_mhz is not None],
                        95,
                    ),
                    "p95_max_freq_mhz": percentile(
                        [s.cpu_freq_max_mhz for s in window_samples if s.cpu_freq_max_mhz is not None],
                        95,
                    ),
                    "p95_pkg_temp_c": percentile(
                        [s.pkg_temp_c for s in window_samples if s.pkg_temp_c is not None], 95
                    ),
                    "p95_core_temp_c": percentile(
                        [s.temp_c for s in per_core_window if s.temp_c is not None], 95
                    ),
                    "median_util_pct": _median(
                        [s.util_pct for s in per_core_window if s.util_pct is not None]
                    ),
                }
            )
        out.append({"machine_id": b.run.machine_id, "benchmarks": per_bench})
    return out


def _telemetry_channels_row(b: RunBundle) -> dict[str, Any]:
    return {
        "machine_id": b.run.machine_id,
        "channels": b.telemetry_channels_available,
        "n_samples": len(b.telemetry),
        "n_per_core_samples": len(b.per_core),
        "telemetry_offset_seconds": b.telemetry_offset_seconds,
    }


def _appendix_row(b: RunBundle, results_root: Path) -> dict[str, Any]:
    return {
        "machine_id": b.run.machine_id,
        "run_dir": manifest_path_relative(b, results_root),
        "db_name": b.discovered.db_path.name,
        "manifest_name": b.discovered.manifest_path.name if b.discovered.manifest_path else "—",
        "db_bytes": b.db_bytes,
        "n_results": len(b.results),
        "n_telemetry": len(b.telemetry),
        "n_per_core": len(b.per_core),
        "schema_version": b.run.schema_version,
    }


def _geekbench_known_table(
    bundles: list[RunBundle],
    geekbench: GeekbenchData,
    fits: dict[str, LogLogFit | None],
) -> list[dict[str, Any]]:
    """Per-machine rows for the known-points Geekbench table."""
    single_fit = fits.get("single")
    multi_fit = fits.get("multi")
    rows: list[dict[str, Any]] = []
    for b in bundles:
        opt = optimal_scp_config(b)
        score_row = geekbench.scores.get(b.run.machine_id)
        wall = opt[1] if opt else None
        single = score_row.single_core if score_row else None
        multi = score_row.multi_core if score_row else None
        pred_single = (
            single_fit.predict(single) if (single_fit and single and single > 0) else None
        )
        pred_multi = (
            multi_fit.predict(multi) if (multi_fit and multi and multi > 0) else None
        )
        rows.append(
            {
                "machine_id": b.run.machine_id,
                "cpu_label": score_row.cpu_label if score_row else b.host.cpu_brand,
                "single_core": single,
                "multi_core": multi,
                "actual_wall_s": wall,
                "predicted_from_single_s": pred_single,
                "predicted_from_multi_s": pred_multi,
                "residual_single_pct": (
                    (wall - pred_single) / pred_single * 100.0
                    if (wall is not None and pred_single)
                    else None
                ),
                "residual_multi_pct": (
                    (wall - pred_multi) / pred_multi * 100.0
                    if (wall is not None and pred_multi)
                    else None
                ),
            }
        )
    return rows


def _geekbench_prospective_table(
    geekbench: GeekbenchData,
    fits: dict[str, LogLogFit | None],
) -> list[dict[str, Any]]:
    """Per-prospective-machine rows: scores + predicted SCP wall (single/multi)."""
    single_fit = fits.get("single")
    multi_fit = fits.get("multi")
    return [
        {
            "slug": p.slug,
            "cpu_label": p.cpu_label,
            "single_core": p.single_core,
            "multi_core": p.multi_core,
            "predicted_from_single_s": (
                single_fit.predict(p.single_core)
                if (single_fit and p.single_core and p.single_core > 0)
                else None
            ),
            "predicted_from_multi_s": (
                multi_fit.predict(p.multi_core)
                if (multi_fit and p.multi_core and p.multi_core > 0)
                else None
            ),
            "notes": p.notes,
        }
        for p in geekbench.prospective
    ]


def _regression_skip_count(bundles: list[RunBundle]) -> int:
    for b in bundles:
        cfg = b.run.config.get("benchmarks", {}).get("regression", {})
        skips = cfg.get("skip_tests", [])
        if isinstance(skips, list):
            return len(skips)
    return 0


def _median(values: Iterable[float]) -> float | None:
    s = summarize(list(values))
    return s.median


# ---------------------------------------------------------------- jinja filters


def _fmt_seconds(value: float | None) -> str:
    if value is None:
        return "—"
    if value < 1:
        return f"{value * 1000:.1f} ms"
    if value < 60:
        return f"{value:.2f} s"
    if value < 3600:
        m, s = divmod(value, 60)
        return f"{int(m)} m {s:.1f} s"
    h, rem = divmod(value, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)} h {int(m)} m"


def _fmt_float(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _fmt_duration(value: float | None) -> str:
    return _fmt_seconds(value)


def _fmt_bytes(value: int | None) -> str:
    if value is None:
        return "—"
    v = float(value)
    for unit in ["B", "KiB", "MiB", "GiB"]:
        if v < 1024:
            return f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} TiB"
