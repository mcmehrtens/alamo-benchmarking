# alamo-benchmarking

Cross-platform benchmarking suite for [Alamo](https://github.com/solidsgroup/alamo), the lab's phase-field solid-mechanics solver. Runs on macOS 26+ (Apple Silicon) and Ubuntu 24.04+ (Intel Xeon). Designed for research-quality results that hold up to PI-level scrutiny.

## Quick start

After installing the [prerequisites](#prerequisites) and completing [SETUP.md](SETUP.md) (which includes declaring this machine's stable `machine_id` — required):

```bash
git clone --recurse-submodules <this-repo-url>
cd alamo-benchmarking
uv sync
uv run alamo-benchmark run
```

`uv run alamo-benchmark run` is the **single command** that drives the entire end-to-end suite — pre-flight verification, noise-floor calibration, every benchmark with all warmups and repetitions, 1 Hz hardware telemetry, full metadata capture, and per-machine SQLite output. Designed to fit in a ≤12 h overnight on the slowest expected lab machine (M1 Pro, ~9–11 h end-to-end); faster hardware finishes proportionally sooner.

Results land in `results/<machine_id>/run_<UTC-timestamp>/` — that subdir contains the per-machine SQLite DB, JSON manifest, top-level run log, per-rep subprocess logs, and rendered frames/videos. `machine_id` is the user-declared stable identifier from SETUP.md §4b (not the OS hostname, because mDNS-reported hostnames drift across networks on macOS). Commit and push your machine's results once the run finishes. Aggregating multiple machines is a SQLite `ATTACH` away (see [Aggregating across machines](#aggregating-across-machines)).

### Diagnostic commands

```bash
uv run alamo-benchmark preflight   # run only the pre-flight checks, no benchmarks
uv run alamo-benchmark describe    # dump topology + tool versions for this machine
uv run alamo-benchmark dry-run     # show what would run, don't execute
```

## Prerequisites

Install these manually before cloning. The benchmark does **not** install dependencies — we measure the system as you'll actually use it. The compiler is the per-OS vanilla toolchain (Apple Clang on macOS, LLVM clang on Linux) — see [Design decisions](#design-decisions).

**macOS (26+)**:

```bash
xcode-select --install                                  # Apple Clang, make, git
brew install open-mpi eigen libpng ffmpeg uv tmux       # Alamo deps + ffmpeg + uv + tmux
# gifski: download a binary release from github.com/ImageOptim/gifski
```

`powermetrics` ships with macOS — nothing to install for telemetry.

**Ubuntu (24.04+)** via apt:

```bash
sudo apt install build-essential clang libstdc++-14-dev \
                 libopenmpi-dev libeigen3-dev libpng-dev \
                 ffmpeg linux-tools-generic "linux-tools-$(uname -r)" \
                 tmux
# gifski: download a binary release from github.com/ImageOptim/gifski
# uv: install from astral.sh/uv
```

`turbostat` ships in `linux-tools-*` — required for telemetry. `tmux` is for the SSH-detached overnight workflow (SETUP.md §7a).

Both platforms additionally require `git`, `sudo` (telemetry uses `powermetrics`/`turbostat` which require root), and Python 3.14.5 (managed by `uv sync`). See [SETUP.md](SETUP.md) for the full per-machine checklist.

## Pre-flight requirements

The script refuses to start unless the machine is in benchmark-ready state. Configure manually first:

| Setting           | macOS                                                 | Linux                                            |
| ----------------- | ----------------------------------------------------- | ------------------------------------------------ |
| AC power          | Plugged in                                            | Plugged in                                       |
| Performance mode  | High Power Mode on (System Settings → Battery)        | `sudo cpupower frequency-set -g performance`     |
| Low-power mode    | Off                                                   | n/a                                              |
| Turbo boost       | Left on (recorded, not controlled)                    | `/sys/devices/system/cpu/intel_pstate/no_turbo=0`|
| Background apps   | Closed; load avg below threshold                      | Same                                             |
| Disk space        | ≥ 50 GB free                                          | Same                                             |

`uv run alamo-benchmark preflight` verifies without running the suite. Override with `--force` (logged in the manifest) only if you know what you're doing.

## What gets benchmarked

| # | Benchmark                                                  | Reps                  | What it stresses              |
| - | ---------------------------------------------------------- | --------------------- | ----------------------------- |
| 0 | Noise floor (`numpy` matmul)                               | 20                    | Per-machine variance baseline |
| 1 | Serial compile, all dims (`./configure && make -j1`)       | 2                     | Single-thread compiler perf   |
| 2 | Parallel compile, all dims (`make -j<physical>`)           | 2                     | Parallel build scaling        |
| 3 | Regression suite (`./scripts/runtests.py`)                 | 2                     | Mixed workload                |
| 4 | SCPSpheresElastic across `-np` sweep                       | 5 per `np`            | MPI strong scaling            |
| 5 | `render_frames` — yt SlicePlot → PNG, one per plotfile     | 5                     | I/O, numpy, matplotlib        |
| 6 | `render_encode` — gifski / AV1 / H.265 over rendered PNGs  | 5 per codec           | CPU video codec               |

Compile reps build every dimension listed in `[alamo] dims` (production default: `[2, 3]`, since the regression suite needs both 2D and 3D binaries). The reported wall time covers all configured dims combined.

The compiler is **Apple Clang on macOS, LLVM clang on Linux** — both invoked as `clang++` via `$PATH`. This is deliberate: the PI wants benchmark numbers from each machine's vanilla toolchain, not a unified cross-platform stack. The exact compiler version is captured per-machine in the manifest's `platform.tool_versions["clang"]`.

The `-np` sweep is `1, 2, 4, 8, …, physical, physical + virtual`, deduplicated. See [Core topology](#core-topology) for the per-platform definition of physical vs virtual.

`render_frames` and `render_encode` are wired so the encoder always sees a deterministic frame set: `render_frames` writes PNGs to `run_dir/render/frames_rep<N>/`, and `render_encode` picks the most-recently-written `frames_rep*` dir as input. The default config lists `render_frames` immediately before `render_encode` in `[benchmarks].enabled` so the dependency is satisfied without spec ordering tricks.

Per-rep mechanics:

- 1 warmup rep (timed and recorded, flagged as `is_warmup`).
- 30 s cooldown between reps.
- Run order randomized within a sweep (defeats slow drift over the night).
- 1 Hz telemetry: per-core frequency, package power, thermals, memory, load avg.
- Compile benchmarks: cold cache enforced (`git clean -fdx alamo/` + `CCACHE_DISABLE=1`) before each rep.
- SCP runs: a SHA-256 of canonical output fields is recorded — reps producing different hashes are flagged.

## Core topology

Different chips disagree on what "physical" and "virtual" mean. The sweep adapts:

| Platform                        | Physical (`perflevel0`)              | Virtual (`perflevel1`)             |
| ------------------------------- | ------------------------------------ | ---------------------------------- |
| Intel Xeon (HT enabled)         | `sockets × cores/socket`             | `physical × (threads/core − 1)`    |
| Apple M5 Pro / M5 Max           | super cores                          | performance cores                  |
| Apple base M5                   | super cores                          | efficiency cores                   |
| Apple M1–M4 (all variants)      | performance cores                    | efficiency cores                   |

Detected from `sysctl hw.perflevel*` + `machdep.cpu.brand_string` on macOS, and `lscpu -J` on Linux. Across the fleet the rule is uniform: the dominant core class on a chip counts as physical; the secondary tier (whether labeled "performance" on Fusion chips, "efficiency" elsewhere, or HT sibling on Xeon) fills the `physical + virtual` sweep slot. Per-core telemetry still reports the Apple-native cluster name (`super` / `performance` / `efficiency`).

## Architecture

```
benchmarks/
├── cli.py              # entry point: alamo-benchmark
├── config.py
├── platform_info.py    # OS, kernel, compiler, MPI versions
├── topology.py         # P/E/super core detection
├── preflight.py        # refuse-to-start gate
├── telemetry/
│   ├── macos.py        # powermetrics sidecar (sudo)
│   ├── linux.py        # turbostat sidecar (sudo)
│   └── sudo.py         # background sudo-ticket keepalive
├── runners/            # one file per benchmark
│   ├── noise_floor.py
│   ├── compile_runner.py
│   ├── regression.py
│   ├── scp_elastic.py
│   └── render.py       # render_frames + render_encode
└── storage/            # SQLite schema + writer

configs/
├── default.toml          # full overnight run (≤12 h budget)
├── validate.toml         # ~30 min end-to-end sanity check before the overnight
├── regression_only.toml  # isolate regression-suite timing on a new machine
└── quick.toml            # noise-floor-only smoke test (harness check)

alamo/                  # git submodule, pinned SHA on `development`
results/<machine_id>/   # see SETUP.md §4b — user-declared, network-stable
└── run_<ts>/           # per-run output dir
    ├── alamo-benchmark.log   # full run log (mirrors stdout)
    ├── run_<ts>.db           # SQLite results
    ├── run_<ts>.manifest.json
    ├── logs/                 # per-rep subprocess logs (compile, SCP, regression, render)
    └── render/               # frames_rep*/ + encoded gif/webm/mp4 outputs
```

### Data model

One SQLite database per `run` invocation:

- `run` — UUID, timestamps, repo SHAs (this repo + Alamo), config snapshot, schema version.
- `host` — OS, kernel, CPU brand, core counts by type, RAM, tool versions, governor / performance-mode state, full pre-flight diagnostic.
- `result` — one row per (benchmark, config, rep): wall/user/sys time, max RSS, exit code, status, log paths, output-hash for determinism check.
- `telemetry_sample` — 1 Hz package-level samples (avg/max frequency, package power, package temp, memory, load avg).
- `telemetry_per_core` — 1 Hz per-core samples (frequency, temp, util, core type).

Joining telemetry to a benchmark rep is a time-range query on `(run_id, ts)` within `result.started_at`–`result.ended_at`.

A JSON manifest sits beside each `.db` with the same metadata in a more human-readable form — useful for `git diff`s when an environment changes.

## Design decisions

The choices most likely to come under scrutiny, each with a rationale:

| Decision                                                                                       | Rationale                                                                                                                                                                                       |
| ---------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| PI-grade statistical rigor: 5 reps for short benchmarks (SCP, render), 2 reps for long benchmarks (compile, regression), median + IQR, σ reported | Single-run timings are unreliable. Mean obscures bimodal distributions. Median + IQR is the standard for noisy systems benchmarks. Long benchmarks are capped at 2 reps because the regression suite alone can take 30–90 min/rep on lab hardware — a third rep would push the overnight run past 12 h.                                                                       |
| Cold build cache between compile reps                                                          | Measures true compile time. Warm-cache benchmarks compare cache hits, not compiler work.                                                                                                        |
| Sudo-based telemetry (`powermetrics` / `turbostat`)                                            | Per-core frequency and package power are unavailable to user-space. These are the only signals that prove or disprove thermal throttling.                                                       |
| Turbo boost left ON, characterized in telemetry                                                | Matches how Alamo is actually used in the lab. The telemetry stream shows whether each machine sustained boost or throttled, so the data answers "did this machine throttle?" empirically.       |
| Run-order randomization within sweeps                                                          | Drift over an 8-hour run (thermals, background load) doesn't correlate with `-np` if order is shuffled.                                                                                          |
| Noise-floor characterization at start                                                          | 20 reps of a tight microbenchmark establish a per-machine, per-night σ. Confidence intervals are anchored to observed noise instead of being assumed.                                            |
| Per-machine SQLite + JSON manifest committed to git                                            | Diffable, queryable, survives `git pull`. Large artifacts (build dirs, plotfiles, rendered frames, raw logs) are gitignored.                                                                     |
| Force-fail pre-flight checks                                                                   | A run on battery with Low Power Mode is not the same experiment as one on AC + High Power Mode. Refusing to start is safer than silently capturing comparable-looking but actually-different data.|
| Determinism check on SCP output                                                                | Two runs that produce different physics aren't comparable, however nice their timings look.                                                                                                     |
| Restart fresh on crash, no auto-resume                                                         | Partial data from a half-run is a footgun for aggregation. Better to discard and re-run cleanly than to stitch.                                                                                  |
| Single end-to-end command                                                                      | Multi-step recipes drift across machines. One command means every machine ran the same protocol.                                                                                                |

## Multi-machine workflow

1. Clone this repo on each lab machine (with submodules).
2. Declare this machine's `machine_id` once (SETUP.md §4b).
3. Verify the pre-flight passes (`uv run alamo-benchmark preflight`).
4. Kick off the full run on each machine in parallel (`uv run alamo-benchmark run`).
5. Each machine writes to `results/<machine_id>/` — no merge conflicts as long as every machine picks a unique `machine_id`.
6. Commit and push per-machine results once each run completes.
7. Aggregate from any machine after pulling.

## Aggregating across machines

After every machine has pushed its results:

```bash
git pull
sqlite3 <<'SQL'
.mode column
.headers on
ATTACH DATABASE 'results/iastate-m1pro-01/run_2026-05-19T03-00-00Z/run_2026-05-19T03-00-00Z.db' AS host_a;
ATTACH DATABASE 'results/iastate-xeon-w5-2545/run_2026-05-19T03-00-00Z/run_2026-05-19T03-00-00Z.db' AS host_b;
-- ...

-- Per-(host, benchmark, config) min / median / max wall time. Median via
-- PERCENTILE; if your sqlite lacks it, the rank trick (ORDER BY wall_s LIMIT)
-- is equivalent. Bare AVG is intentionally omitted — see CLAUDE.md.
SELECT 'host-a' AS host, benchmark, config_json,
       MIN(wall_s) AS min_s,
       (SELECT wall_s FROM host_a.result r2
        WHERE r2.benchmark = r1.benchmark AND r2.config_json = r1.config_json
              AND r2.is_warmup = 0 AND r2.status = 'completed'
        ORDER BY wall_s LIMIT 1 OFFSET (COUNT(*) - 1) / 2) AS median_s,
       MAX(wall_s) AS max_s
FROM host_a.result r1
WHERE is_warmup = 0 AND status = 'completed'
GROUP BY benchmark, config_json;
SQL
```

A formal aggregation/reporting tool is deliberately out of scope for v1 — the schema is small enough that a notebook is the right shape (pandas `read_sql` straight from the attached DBs).

## License

See `LICENSE`.
