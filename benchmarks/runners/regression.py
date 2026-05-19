"""Alamo's regression suite (`./scripts/runtests.py`).

We invoke `runtests.py` directly rather than `make test`. The Makefile target
chains in a docs build and style check that aren't part of what we're
benchmarking and frequently break for unrelated reasons. `runtests.py` expects
pre-built binaries under `bin/`, so this runner relies on a `compile_*` rep
having run first (and having built every dim the test suite needs — typically
2D and 3D).

Many of Alamo's tests import `yt`, `matplotlib`, `numpy`, `pandas`, and
`xmltodict` from their result-check scripts. The user is expected to create
`alamo/.venv` via `uv venv` and install those deps there (see SETUP.md step 4).
This runner prepends `alamo/.venv/bin` to PATH and sets VIRTUAL_ENV so
runtests.py and its sub-scripts resolve their `#!/usr/bin/env python3`
shebangs to the venv interpreter. If `.venv` is missing the runner fails the
rep immediately rather than burning ~30+ min running test binaries whose
result-checks will all silently `ModuleNotFoundError` — a footgun the M1 Pro
fixture machine hit in practice.

Skip mechanism: each test dir's `input` file declares its sub-tests via
`#@ [section_name]` blocks. `runtests.py` recognises `#@ skip=true` inside a
section and counts that sub-test as 'skipped' rather than 'failed'. We patch
these in just before invoking runtests.py for every entry in
`[benchmarks.regression] skip_tests` (format: `<TestDir>.<section_name>`).
The cold-cache step (`git -C alamo clean -fdx -e .venv` + `checkout`) before
the next compile rep wipes the patches, so we re-apply at the start of every
regression rep — no persistent modification of the submodule."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path

from benchmarks.runners.base import Benchmark, RunContext, RunResult, RunSpec, override, utc_now

LOG = logging.getLogger(__name__)


class RegressionSuiteBenchmark(Benchmark):
    name = "regression_suite"

    @override
    def specs(self, ctx: RunContext) -> Iterable[RunSpec]:
        warmups = ctx.config.statistics.warmup_reps
        reps = ctx.config.statistics.reps_long
        for i in range(warmups + reps):
            yield RunSpec(
                benchmark=self.name,
                config={"compiler": ctx.config.alamo.compiler},
                rep_index=i,
                is_warmup=(i < warmups),
            )

    @override
    def run_one(self, spec: RunSpec, ctx: RunContext) -> RunResult:
        log_path = ctx.log_dir / f"regression_rep{spec.rep_index}.log"
        compiler = str(spec.config["compiler"])

        venv_bin = ctx.alamo_dir / ".venv" / "bin"
        venv_python = venv_bin / "python3"
        started_at = utc_now()
        t0 = time.perf_counter()
        if not venv_python.exists():
            return RunResult(
                spec=spec,
                started_at=started_at,
                ended_at=utc_now(),
                wall_s=time.perf_counter() - t0,
                exit_code=-1,
                status="failed",
                notes=(
                    f"alamo/.venv missing (no {venv_python}); follow SETUP.md step 4 "
                    "to create it before running regression_suite"
                ),
            )

        env = os.environ.copy()
        env["PATH"] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"
        env["VIRTUAL_ENV"] = str(venv_bin.parent)

        skipped, missing = _apply_skip_patches(
            ctx.alamo_dir, ctx.config.benchmarks.regression_skip_tests
        )
        skip_note = ""
        if skipped:
            skip_note = f"skipped {len(skipped)} test sections"
            if missing:
                skip_note += f"; {len(missing)} skip entries did not match (see log)"
            LOG.info("regression: %s", skip_note)
            for entry in missing:
                LOG.warning("regression: skip_tests entry did not match: %s", entry)

        with log_path.open("wb") as logf:
            rc = subprocess.run(
                ["./scripts/runtests.py", "--comp", compiler],
                cwd=ctx.alamo_dir,
                env=env,
                check=False,
                stdout=logf,
                stderr=subprocess.STDOUT,
            ).returncode
        t1 = time.perf_counter()
        ended_at = utc_now()
        return RunResult(
            spec=spec,
            started_at=started_at,
            ended_at=ended_at,
            wall_s=t1 - t0,
            exit_code=rc,
            status="completed" if rc == 0 else "failed",
            stdout_path=str(log_path),
            stderr_path=str(log_path),
            notes=skip_note,
        )


_SECTION_HEADER_RE = re.compile(r"^#@\s*\[(?P<name>[^\]]+)\]\s*$")


def _apply_skip_patches(
    alamo_dir: Path, skip_tests: tuple[str, ...]
) -> tuple[list[str], list[str]]:
    """Inject `#@ skip=true` into the requested `(dir, section)` pairs.

    Returns `(applied, missing)`:
      - `applied` lists entries that were inserted (or already present).
      - `missing` lists entries whose file or section header was not found.

    Idempotent: if a section already has `#@ skip=true`, we leave it alone.
    """
    applied: list[str] = []
    missing: list[str] = []
    by_dir: dict[str, list[str]] = {}
    for entry in skip_tests:
        if "." not in entry:
            missing.append(entry)
            continue
        test_dir, section = entry.split(".", 1)
        by_dir.setdefault(test_dir, []).append(section)

    for test_dir, sections in by_dir.items():
        input_path = alamo_dir / "tests" / test_dir / "input"
        if not input_path.is_file():
            missing.extend(f"{test_dir}.{s}" for s in sections)
            continue
        lines = input_path.read_text().splitlines()
        changed, applied_here, missing_here = _patch_lines(lines, sections)
        applied.extend(f"{test_dir}.{s}" for s in applied_here)
        missing.extend(f"{test_dir}.{s}" for s in missing_here)
        if changed:
            input_path.write_text("\n".join(lines) + "\n")
    return applied, missing


def _patch_lines(
    lines: list[str], sections_to_skip: list[str]
) -> tuple[bool, list[str], list[str]]:
    """Insert `#@ skip=true` after the first `#@ [<section>]` for each target.

    Mutates `lines` in place. Returns (changed?, applied_sections, missing_sections).
    """
    want = set(sections_to_skip)
    found: set[str] = set()
    changed = False
    i = 0
    while i < len(lines):
        match = _SECTION_HEADER_RE.match(lines[i])
        if match and match.group("name") in want and match.group("name") not in found:
            section = match.group("name")
            found.add(section)
            # Check if `#@ skip=true` already exists in this section block.
            if not _section_already_skipped(lines, i):
                lines.insert(i + 1, "#@ skip=true")
                changed = True
                # Skip past the inserted line so we don't re-scan it.
                i += 1
        i += 1
    applied = sorted(found)
    missing = sorted(want - found)
    return changed, applied, missing


def _section_already_skipped(lines: list[str], section_start_idx: int) -> bool:
    """Return True if the section starting at `section_start_idx` already has
    a `#@ skip=true` directive before the next `#@ [section]` header."""
    for j in range(section_start_idx + 1, len(lines)):
        if _SECTION_HEADER_RE.match(lines[j]):
            return False
        stripped = lines[j].strip()
        if not stripped.startswith("#@"):
            return False  # left the section block entirely
        body = stripped[2:].strip()  # drop the "#@" prefix
        if re.match(r"^skip\s*=\s*(true|yes|1)\b", body, flags=re.IGNORECASE):
            return True
    return False
