"""Find the per-machine benchmark DB to feed the report.

The discovery rule is intentionally simple — and matches what we tell users:
each ``results/<machine_id>/run_*/`` is one run, and *the last one wins*. The
earlier dirs are usually `validate.toml` smoke-tests, which we don't want to
mix into a default-config report. We pick the latest by directory name
(timestamps sort lexicographically as ISO-8601).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DiscoveredRun:
    """Pointer to a single per-machine run directory."""

    machine_id: str
    run_dir: Path
    db_path: Path
    manifest_path: Path | None


def discover_runs(results_root: Path) -> list[DiscoveredRun]:
    """Return one ``DiscoveredRun`` per machine directory under ``results_root``.

    Skips machine dirs that contain no ``run_*`` subdirectories, and skips
    runs whose ``*.db`` is missing (e.g. an LFS pointer was never pulled).
    Sorted by ``machine_id`` so the report is stable across invocations.
    """
    if not results_root.exists():
        return []
    out: list[DiscoveredRun] = []
    for machine_dir in sorted(p for p in results_root.iterdir() if p.is_dir()):
        runs = sorted(p for p in machine_dir.glob("run_*") if p.is_dir())
        if not runs:
            continue
        latest = runs[-1]
        dbs = list(latest.glob("*.db"))
        if not dbs:
            continue
        db = dbs[0]
        manifest_candidates = list(latest.glob("*.manifest.json"))
        manifest = manifest_candidates[0] if manifest_candidates else None
        out.append(
            DiscoveredRun(
                machine_id=machine_dir.name,
                run_dir=latest,
                db_path=db,
                manifest_path=manifest,
            )
        )
    return out
