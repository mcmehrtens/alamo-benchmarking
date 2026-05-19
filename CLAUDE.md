# CLAUDE.md

Context and conventions for AI agents working in this repo. The README is the user-facing source of truth for structure, architecture, and design decisions. This file captures the implementation-level details and "things you'll regret if you forget."

## What this is

A cross-platform Python benchmarking suite for Alamo (lab's phase-field solid-mechanics solver, `github.com/solidsgroup/alamo`, pinned as a git submodule at `alamo/` on the `development` branch). Targets macOS 26+ (Apple Silicon) and Ubuntu 24.04+ (Intel Xeon, HT enabled). The user runs it manually on each lab machine, commits per-machine results to git, and aggregates later. Time budget per machine: overnight (8–12 hours). Results must be defensible to a PI.

## Single entry point — non-negotiable

`uv run alamo-benchmark run` is THE command that drives the full suite. Every other CLI subcommand (`preflight`, `describe`, `dry-run`) is diagnostic. Do not split the end-to-end into multiple required steps. If you find yourself adding a new "step 2" the user has to remember, fold it into `run` instead.

## Sharp edges

### Machine identity is user-declared, not derived from hostname
The benchmark refuses to start `run` unless `ALAMO_BENCHMARK_MACHINE_ID` is set (env var) or `~/.alamo-benchmark/machine_id` exists. `socket.gethostname()` is unreliable on macOS — the same physical Mac reports `foo.local` on one network and `foo.lab.example.edu` on another via mDNS/DNS, so two networks would silently fork the same machine's results into two `results/<host>/` dirs. `machine_id` is stored in the run table (schema v2+), used for the results dir name, and validated to `[A-Za-z0-9._-]{1,64}` to keep it filesystem-safe. The original OS hostname is still recorded in the `hostname` column for context but is no longer the canonical key.

### Sudo over long runs
macOS `sudo` cache expires (~5 min default). The telemetry sidecar keeps it alive via a background `sudo -v` keepalive loop. Don't assume the sudo cache is valid hours into a run — re-validate. Same applies on Linux but the default cache is sometimes longer; assume nothing.

### Apple Silicon core types
Detect via `sysctl machdep.cpu.brand_string`, then map perflevels:

- **M5 Pro / M5 Max** (Fusion Architecture): `perflevel0 = super`, `perflevel1 = performance`. Both count as physical; no "virtual" tier. Performance cores here are designed for sustained MT throughput, not low-power background work.
- **Base M5**: `perflevel0 = super`, `perflevel1 = efficiency`. Treat super as physical, efficiency as virtual.
- **M1 / M2 / M3 / M4** (all variants): `perflevel0 = performance`, `perflevel1 = efficiency`. Performance is physical, efficiency is virtual.

Heuristic if a future chip's brand string is unrecognized: if `perflevel1` cores have comparable L2 cache size to `perflevel0` (within ~2×), assume both are "physical-class" and warn loudly. If `perflevel1` is much smaller, it's an efficiency tier — treat as virtual.

### MPI affinity is platform-specific
On Linux, OpenMPI honors `--bind-to core --map-by core` and pinning is reliable — `scp_elastic.run_one` passes those flags explicitly, plus `--use-hwthread-cpus` so the topology's `physical + virtual` sweep target isn't rejected as "not enough slots" by OpenMPI 4.x's physical-cores-only slot accounting. On macOS, OpenMPI/PRRTE has no hwloc binding support and an `mpiexec` invocation that includes any of these aborts with exit 213; the runner therefore omits the flags on Darwin and accepts whatever placement the OS scheduler chooses. In both cases we set `OMPI_MCA_orte_report_bindings=1` so the runtime prints one `MCW rank N bound to socket S[core C[hwt H]]` line per rank to stderr. After the subprocess exits, `parse_mpi_bindings` extracts a structured summary (ranks bound vs unbound, distinct sockets/cores, `one_rank_per_core` flag) and `result.notes` carries a one-line readout like `affinity: bound=8/8 sockets=[0] cores=[0,1,2,3,4,5,6,7]`. The full stderr is also preserved in the per-rep log file for forensic detail.

### Cold cache means cold cache
Before each compile rep:
1. `git -C alamo clean -fdx -e .venv` (wipe everything untracked, but preserve `alamo/.venv` — see below).
2. `git -C alamo checkout -- .` to undo any stray edits.
3. Set `CCACHE_DISABLE=1` in the env passed to `make`.
4. Verify `git -C alamo status --porcelain` is empty.

A half-warm cache produces compile timings that aren't comparable across reps and aren't defensible.

**Why preserve `.venv`:** Alamo's regression suite (`runtests.py`) needs `alamo/.venv/bin/python3` with `yt`, `pandas`, etc. That venv is created once via SETUP.md step 4 and lives across runs. It's untracked-and-gitignored, so a bare `git clean -fdx` deletes it on every compile rep — which then fails the next `regression_suite` rep. The `-e .venv` exclude keeps it alive.

### Telemetry parser robustness
- `powermetrics --format plist` emits multiple plists in a stream; parse incrementally, skip malformed plists with a logged warning.
- `turbostat` output format varies by kernel; the header line tells you which columns exist — parse by column name, never by position.
- A telemetry failure must NEVER kill a benchmark rep. Log, mark a gap in the manifest, continue. Telemetry is observation, not control.

### Schema versioning
Bump `schema_version` in the `run` table on ANY breaking schema change. The aggregation pattern (`ATTACH DATABASE`) requires that callers know what schema each per-machine DB uses. Older DBs do not need to be migrated forward — aggregation queries can branch on `schema_version`. Current version: **2** (added `run.machine_id` in v2; older v1 DBs lack the column and shouldn't be aggregated alongside v2 without translation).

### Timestamps
All UTC, all ISO-8601, all `YYYY-MM-DDTHH:MM:SS.ffffffZ` format. Never store local time. Telemetry samples and benchmark windows are joined by time range — mixing timezones across lab machines is a debugging nightmare.

### Subprocess hygiene
Use `subprocess.run` with `check=False, capture_output=False, text=True`, and stream stdout/stderr to log files via Popen pipes — large compile/test logs should not live in memory. Path to the log file is stored in `result.stdout_path` / `result.stderr_path`.

### Random seeding & rep ordering
The within-sweep run-order shuffle uses a seeded RNG. The seed is stored in `run.config_json` so a given run is reproducible if someone wants to verify ordering effects.

**Warmups always run before measured reps.** `cli._order_specs` partitions a runner's specs into `is_warmup=True` and `is_warmup=False`, shuffles each bucket independently with the same RNG, and emits warmups first. Without this carve-out a shuffle could put the warmup mid-sequence, leaving the first measured rep to pay the cold-cache / BLAS-init cost — that breaks the warmup contract. Within each bucket the shuffle still defeats drift-vs-sweep-position correlation. For multi-warmup runners (e.g. `scp_elastic` with `warmup_reps=1` and a 5-point np sweep produces 5 warmups + 5 measured per `reps_short`), the warmups themselves are shuffled so the thermal state inherited by the first measured rep isn't pinned to the last-inserted warmup spec.

### Pre-flight is observe-only
Pre-flight checks REPORT system state and refuse to start if conditions aren't met. They do NOT mutate the system (don't flip governors, don't toggle High Power Mode, don't kill background processes). The user configures the machine themselves; pre-flight just verifies. This is explicit by user choice — don't backslide into auto-configuration even if it would be convenient.

### Compiler per OS: vanilla, not unified
The benchmark builds Alamo with the **default per-OS** compiler the PI considers "vanilla":

- **macOS**: Apple Clang (Xcode CLT). Invoked as `clang++` via `$PATH`.
- **Linux**: LLVM clang (standard distro package). Also invoked as `clang++` via `$PATH`.

Config `[alamo] compiler = "clang++"` is correct for **both** — `$PATH` resolves to the right binary. Don't try to pin paths or unify. The actual compiler version (Apple vs LLVM, version number) is captured in `platform_info.tool_versions["clang"]` and surfaces in the manifest, so the distinction is recorded unambiguously per-machine. An earlier draft of the requirements said "LLVM on both"; that's superseded — do not standardize.

### Multi-dim compile builds
`compile_serial` and `compile_parallel` build every dimension in `[alamo] dims` per rep (`./configure --dim=<D> --comp=<compiler>` then `make`, repeated for each `D`). Production (`default.toml`) is `[2, 3]` because:

- `scp_elastic` currently runs in 2D (`[benchmarks.scp_elastic] dim = 2` — see "Timing budget" below for why) → 2D binary required
- `regression_suite` exercises both 2D and 3D test sections → both binaries required

If `dim = 3` ever becomes viable for `scp_elastic` again, leave production at `[2, 3]`; if `regression_suite` is disabled and SCP stays 2D, `[2]` alone is sufficient (`validate.toml` does exactly this).

The wall-clock for a rep covers all dims combined — that's the "build Alamo from scratch" timing a new lab user would experience. Cold cache (`git clean -fdx -e .venv`) wipes everything including cloned AMReX once per rep, BEFORE the first dim; subsequent dims in the same rep reuse the per-dim build output dirs Alamo creates (e.g., `ext/AMReX-Codes/amrex/2d-clang++-26.02` vs `3d-...`).

### Regression suite invokes runtests.py directly
The `regression_suite` benchmark runs `./scripts/runtests.py --comp <compiler>`, **not** `make test`. The Makefile's `test` target chains in `check_tabs.py` and `make docs`, which are unrelated to what we're benchmarking and frequently break for unrelated reasons. `runtests.py` does not build Alamo — it expects pre-built binaries at `bin/{exe}-{dim}d-{comp}`, so a `compile_*` rep MUST run first and MUST have built every dim the regression suite needs. FFT tests are opt-in via `--fft`; we don't add that flag.

**Skipping known-bad sub-tests.** Some sub-tests are flaky or platform-specific (e.g. the Voronoi / ThermoElastic 2D suites tolerance-bust on macOS as of 2026-05-19). `[benchmarks.regression] skip_tests = ["TestDir.section", ...]` is converted at the start of every regression rep into `#@ skip=true` injections inside `alamo/tests/<TestDir>/input`. `runtests.py` honors the directive and counts the section as 'skipped', not 'failed'. The next compile rep's `git checkout -- .` wipes the patches — the alamo submodule is never persistently modified. Implementation: `benchmarks/runners/regression.py:_apply_skip_patches`. Adding a new skip is config-only — no code change.

### Render runs as two cooperating benchmarks
`render_frames` (yt → PNG) and `render_encode` (ffmpeg/gifski) are two separate runners. Their order in `[benchmarks].enabled` matters: `render_frames` must come before `render_encode` so the encoder can pick up the most-recently-written `frames_rep<N>/` dir under `run_dir/render/`. Within each runner, `cli._cmd_run_inner` still shuffles spec ordering — that's safe because every encode spec reads from the same canonical frames dir (chosen by mtime). A missing prerequisite (no SCP output for frames, no frames for encode) produces a `notes="…"` failure row, NOT an exception — same shape as `scp_elastic`'s "no Alamo binary" failure mode.

Encoder commands include `-vf "pad=ceil(iw/2)*2:ceil(ih/2)*2"` because yt's matplotlib output has odd-dimension borders that libx265 rejects under `yuv420p`. Don't drop the pad filter unless you've also dropped `yuv420p`.

`render_frames` uses `from yt import SlicePlot, load, set_log_level` with per-symbol `# pyright: ignore[reportPrivateImportUsage]`; yt doesn't declare them in `__all__` even though they're documented public API.

### File logging
Every `run` invocation tees the root logger to `run_dir/alamo-benchmark.log`. The handler is attached BEFORE preflight runs so a preflight failure is captured on disk too. `_print_preflight` deliberately calls both `print` (for the operator watching stdout) and `LOG.info` (so the file gets the same lines). Per-rep subprocess output (compile logs, regression logs, SCP logs, frame-render logs) lives separately under `run_dir/logs/`.

### Timing budget on the slowest machine
On the M1 Pro fixture machine, a full **3D** `scp_elastic` sweep at `stop_time = 0.001_s` (1000 timesteps) took 229 min wall: np=1 → 91 min, np=2 → 57 min, np=4 → 34 min, np=8 → 20 min, np=10 → 27 min.

These numbers are **stale as of 2026-05-19** — `scp_elastic` now runs in 2D (`[benchmarks.scp_elastic] dim = 2`) to avoid an upstream Alamo bug in `BC/Operator/Elastic/Constant.H` that reads `AMREX_SPACEDIM` BC entries when the `tests/SCPSpheresElastic/input` file only supplies 2 (correct for 2D, OOB on 3D). The 2D path is much cheaper per step. Re-baseline against `validate.toml` output on each lab machine before tightening `default.toml`'s `stop_time` — don't extrapolate from the 3D table above.

The 3D reference is preserved here for the day the Alamo bug is fixed and `dim = 3` becomes viable again. At that point, restore the 3D budget calculation.

If a future agent considers raising `stop_time` "to be more thorough", verify the new total wall time against a fresh `validate.toml` run first.

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
- SQLite + JSON manifest output under `results/<machine_id>/run_<ts>/` (per-run subdir for DB, manifest, top-level run log, per-rep subprocess logs, and rendered media). `machine_id` is required and read from `$ALAMO_BENCHMARK_MACHINE_ID` or `~/.alamo-benchmark/machine_id`; the run refuses to start if unset.
- `noise_floor` runner — fully implemented and validated; matmul size 4000 lands ~100–300 ms per rep on Apple Silicon and Xeon (M1 Pro reference: ~269 ms).
- `compile_serial`, `compile_parallel`, `regression_suite`, `scp_elastic` runners — implemented and exercised end-to-end against a real Alamo build on M1 Pro.
- `render_frames` + `render_encode` runners — yt SlicePlot for frames, gifski / libsvtav1 / libx265 for encodes. Reads the latest `scp_elastic` output_bench dir; renders the `eta` field as a z-slice at the configured resolution.
- **Decomposition-invariant SCP output hash:** `_hash_output` loads the final cell plotfile via yt and hashes `(current_time, per-field (n, min, max))` quantized to 4 sig figs. Verified to produce identical hashes across the full `np=1,2,4,8,10` sweep at the same `stop_time`, so cross-`np` physics divergence is now detectable (previous Header-bytes hash differed per decomposition).
- **Telemetry sidecars:** `MacosSidecar` (`powermetrics --format plist`) and `LinuxSidecar` (`turbostat --quiet`). One sidecar per run, lifecycle managed in `_cmd_run`. Background sudo keepalive (`SudoKeepalive`). Parsers tested against real captures for M1 Pro / M4 Pro / M5 Pro and Xeon W-1370 / W5-2545 — fixtures live in `tests/fixtures/`. Telemetry-to-result join is by time range. Validated end-to-end on M1 Pro (14,043 samples × 10 cores during a 4 h run).
- File logging: every `run` invocation tees the root logger to `run_dir/alamo-benchmark.log`; per-rep subprocess output goes to `run_dir/logs/`. Handler is attached BEFORE preflight so a preflight failure lands on disk too.
- Ruff (strict ruleset) and Pyright (strict mode) both pass.
- Unit tests for the topology sweep generator, telemetry parsers, and render helpers under `tests/`.
- Alamo pinned as a submodule on the `development` branch.

**Deliberately deferred to a follow-up:**
- **Multi-node MPI:** single-node only.
- **Aggregation CLI:** SQLite `ATTACH` is the v1 interface — see README "Aggregating across machines".
- **End-to-end telemetry validation on a real lab machine:** parsers are fixture-verified (M1 Pro / M4 Pro / M5 Pro / Xeon W-1370 / W5-2545 / E3-1240v6), but the `sudo + subprocess + writer` path has only been smoke-tested locally. First overnight run on each lab box will exercise it for real.
- **MPI affinity instrumentation:** ✅ **Done** — `parse_mpi_bindings` extracts a structured `MpiBindings` from the OpenMPI stderr; summary goes into `result.notes`. Unit-tested against bound / unbound / oversubscribed / dual-socket / noisy-stderr cases.
- **Power-cap awareness on Linux:** ✅ **Done** — `_check_rapl` reads `/sys/class/powercap/intel-rapl:<N>/constraint_0_power_limit_uw` vs `constraint_0_max_power_uw` per package and flags advisory if PL1 < 80% of max. Skips cleanly on macOS / AMD / kernels without RAPL.
