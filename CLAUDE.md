# CLAUDE.md

Context and conventions for AI agents working in this repo. The README is the user-facing source of truth for structure, architecture, and design decisions. This file captures the implementation-level details and "things you'll regret if you forget."

## What this is

A cross-platform Python benchmarking suite for Alamo (lab's phase-field solid-mechanics solver, `github.com/solidsgroup/alamo`, pinned as a git submodule at `alamo/` on the `development` branch). Targets macOS 26+ (Apple Silicon) and Ubuntu 24.04+ (Intel Xeon, HT enabled). The user runs it manually on each lab machine, commits per-machine results to git, and aggregates later. Time budget per machine: overnight (8–12 hours). Results must be defensible to a PI.

## Single entry point — non-negotiable

`uv run alamo-benchmark run` is THE command that drives the full suite. Every other CLI subcommand (`preflight`, `describe`, `dry-run`) is diagnostic. Do not split the end-to-end into multiple required steps. If you find yourself adding a new "step 2" the user has to remember, fold it into `run` instead.

## Sharp edges

### Sudo over long runs
macOS `sudo` cache expires (~5 min default). The telemetry sidecar keeps it alive via a background `sudo -v` keepalive loop. Don't assume the sudo cache is valid hours into a run — re-validate. Same applies on Linux but the default cache is sometimes longer; assume nothing.

### Apple Silicon core types
Detect via `sysctl machdep.cpu.brand_string`, then map perflevels:

- **M5 Pro / M5 Max** (Fusion Architecture): `perflevel0 = super`, `perflevel1 = performance`. Both count as physical; no "virtual" tier. Performance cores here are designed for sustained MT throughput, not low-power background work.
- **Base M5**: `perflevel0 = super`, `perflevel1 = efficiency`. Treat super as physical, efficiency as virtual.
- **M1 / M2 / M3 / M4** (all variants): `perflevel0 = performance`, `perflevel1 = efficiency`. Performance is physical, efficiency is virtual.

Heuristic if a future chip's brand string is unrecognized: if `perflevel1` cores have comparable L2 cache size to `perflevel0` (within ~2×), assume both are "physical-class" and warn loudly. If `perflevel1` is much smaller, it's an efficiency tier — treat as virtual.

### MPI affinity on macOS is best-effort
We ask for `--bind-to core --map-by core` but macOS may ignore it. Always set `OMPI_MCA_orte_report_bindings=1` and capture the resulting stderr into the result row so we know what actually happened, not what was requested. On Linux, affinity is reliable; record it anyway for the same reason.

### Cold cache means cold cache
Before each compile rep:
1. `git -C alamo clean -fdx` (everything except submodule's `.git`).
2. `git -C alamo checkout -- .` to undo any stray edits.
3. Set `CCACHE_DISABLE=1` in the env passed to `make`.
4. Verify `git -C alamo status --porcelain` is empty.

A half-warm cache produces compile timings that aren't comparable across reps and aren't defensible.

### Telemetry parser robustness
- `powermetrics --format plist` emits multiple plists in a stream; parse incrementally, skip malformed plists with a logged warning.
- `turbostat` output format varies by kernel; the header line tells you which columns exist — parse by column name, never by position.
- A telemetry failure must NEVER kill a benchmark rep. Log, mark a gap in the manifest, continue. Telemetry is observation, not control.

### Schema versioning
Bump `schema_version` in the `run` table on ANY breaking schema change. The aggregation pattern (`ATTACH DATABASE`) requires that callers know what schema each per-machine DB uses. Older DBs do not need to be migrated forward — aggregation queries can branch on `schema_version`.

### Timestamps
All UTC, all ISO-8601, all `YYYY-MM-DDTHH:MM:SS.ffffffZ` format. Never store local time. Telemetry samples and benchmark windows are joined by time range — mixing timezones across lab machines is a debugging nightmare.

### Subprocess hygiene
Use `subprocess.run` with `check=False, capture_output=False, text=True`, and stream stdout/stderr to log files via Popen pipes — large compile/test logs should not live in memory. Path to the log file is stored in `result.stdout_path` / `result.stderr_path`.

### Random seeding
The within-sweep run-order shuffle uses a seeded RNG. The seed is stored in `run.config_json` so a given run is reproducible if someone wants to verify ordering effects.

### Pre-flight is observe-only
Pre-flight checks REPORT system state and refuse to start if conditions aren't met. They do NOT mutate the system (don't flip governors, don't toggle High Power Mode, don't kill background processes). The user configures the machine themselves; pre-flight just verifies. This is explicit by user choice — don't backslide into auto-configuration even if it would be convenient.

## Conventions

- Python 3.14.5+. Standard library first: `tomllib`, `pathlib`, `subprocess`, `sqlite3`, `plistlib`, `uuid`, `logging`, `datetime` with `timezone.utc`. Third-party deps minimal — `psutil`, `numpy` for the noise-floor microbenchmark, `yt-project` + `matplotlib` for rendering. Avoid heavyweight frameworks (no `click`, prefer `argparse`; no `pydantic`, prefer dataclasses).
- All internal file paths absolute via `pathlib.Path`. Only the CLI surface accepts relative paths.
- No `print` for diagnostics — `logging` with a UTC-timestamped formatter so Python output lines up with telemetry samples.
- One rep failure → log, mark `status='failed'` in the result row, continue. A bad rep should not kill an 8-hour run.
- No mean-only summaries anywhere. Always median + IQR (or min/max for completeness). The PI will catch a bare mean instantly.

## What NOT to do

- Don't add dependency installation. The user explicitly chose "benchmark only — no installer." Adding `apt install` or `brew install` calls into the script is a regression.
- Don't add aggregation/reporting commands. Same answer. SQLite + a notebook is the intended aggregation interface.
- Don't try to be clever about resuming an interrupted run. Restart-fresh is the chosen policy — partial data should be discarded, not stitched.
- Don't enable ccache "just for the warm-cache benchmark." Cold cache only.
- Don't store a mean alone in any summary table or report.
- Don't mutate system state in pre-flight (see above).
- Don't pin Alamo on `main` — it's `development`. Re-check at implementation time but the branch was chosen deliberately.

## Extending

### Adding a new benchmark
1. Implement `benchmarks/runners/<name>.py` with a class that conforms to the `Benchmark` protocol in `runners/base.py` (`name`, `configs()`, `run(config, rep_index) -> RawResult`).
2. Register in `runners/__init__.py`.
3. Add to the default config under `[benchmarks.<name>]`.
4. Update the README "What gets benchmarked" table.

### Supporting a new platform
1. Add `benchmarks/telemetry/<platform>.py` (must expose the same `start()` / `stop()` interface as `macos.py` / `linux.py`).
2. Extend `topology.py` with the new core-detection logic.
3. Extend `preflight.py` with platform-specific checks.
4. Update both the README and this file with the platform-specific rules.

## Testing

Unit-test what's testable in isolation: telemetry parsers, topology detection, schema writers. Use real captured fixtures for parser tests (sample `powermetrics --format plist` and `turbostat` outputs in `tests/fixtures/`). Mocks rot — real captures stay correct as long as you keep them.

Runners themselves are integration-tested via `--mode quick`, which exercises the same code paths against tiny inputs. Run `quick` mode locally before committing any change to the runner logic.

## v0 status — what's shipped vs deferred

**Shipped and working end-to-end:**
- Full package scaffold; `alamo-benchmark` CLI with `run`, `preflight`, `describe`, `dry-run`.
- Topology detection on macOS (Apple Silicon M1-M5 with Fusion-Architecture awareness) and Linux Xeon (with HT).
- Pre-flight gating; `--force` override is recorded.
- SQLite + JSON manifest output under `results/<hostname>/`.
- `noise_floor` runner — fully implemented and validated.
- `compile_serial`, `compile_parallel`, `regression_suite`, `scp_elastic` runners — implemented, tested at the framework level (real Alamo build not yet exercised end-to-end).
- Ruff (strict ruleset) and Pyright (strict mode) both pass with zero errors/warnings.
- Unit tests for the topology sweep generator under `tests/`.
- Alamo pinned as a submodule on the `development` branch.

**Deliberately deferred to a follow-up:**
- **Telemetry sidecars:** currently a `NoOpSidecar` no-op. macOS `powermetrics` and Linux `turbostat` integrations are the next priority — implementation slots are `benchmarks/telemetry/{macos,linux}.py`, with the `TelemetrySidecar` base class already in place.
- **Rendering pipeline:** `RenderBenchmark` yields no specs. Wait for a successful end-to-end SCP run that produces plotfiles before designing the renderer.
- **Output determinism hash on SCP runs:** `_hash_output` assumes Alamo writes to `alamo/output/`. Confirm against a real Alamo run before relying on it.
- **MPI affinity instrumentation:** we set `OMPI_MCA_orte_report_bindings=1` but don't yet parse the resulting stderr — TODO in `scp_elastic.run_one`.
- **Multi-node MPI:** single-node only.
- **Aggregation CLI:** SQLite `ATTACH` is the v1 interface — see README "Aggregating across machines".
- **Power-cap awareness on Linux:** add a pre-flight check that reads RAPL caps.
