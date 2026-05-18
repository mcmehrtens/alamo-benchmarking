# alamo-benchmarking

Cross-platform benchmarking suite for [Alamo](https://github.com/solidsgroup/alamo), the lab's phase-field solid-mechanics solver. Runs on macOS 26+ (Apple Silicon) and Ubuntu 24.04+ (Intel Xeon). Designed for research-quality results that hold up to PI-level scrutiny.

## Quick start

After installing the [prerequisites](#prerequisites):

```bash
git clone --recurse-submodules <this-repo-url>
cd alamo-benchmarking
uv sync
uv run alamo-benchmark run
```

`uv run alamo-benchmark run` is the **single command** that drives the entire end-to-end suite — pre-flight verification, noise-floor calibration, every benchmark with all warmups and repetitions, 1 Hz hardware telemetry, full metadata capture, and per-machine SQLite output. Expect **6–10 hours** on a typical workstation; designed to be run overnight.

Results land in `results/<hostname>/run_<UTC-timestamp>.{db,manifest.json}`. Commit and push your machine's results once the run finishes. Aggregating multiple machines is a SQLite `ATTACH` away (see [Aggregating across machines](#aggregating-across-machines)).

### Diagnostic commands

```bash
uv run alamo-benchmark preflight   # run only the pre-flight checks, no benchmarks
uv run alamo-benchmark describe    # dump topology + tool versions for this machine
uv run alamo-benchmark dry-run     # show what would run, don't execute
```

## Prerequisites

Install these manually before cloning. The benchmark does **not** install dependencies — we measure the system as you'll actually use it.

**macOS (26+)** via Homebrew:

```bash
brew install llvm open-mpi ffmpeg gifski uv
```

**Ubuntu (24.04+)** via apt:

```bash
sudo apt install clang openmpi-bin libopenmpi-dev make build-essential ffmpeg
# gifski: install from cargo or the release tarball
# uv: install from astral.sh/uv
```

Both platforms additionally require `git`, `sudo` (telemetry uses `powermetrics`/`turbostat` which require root), and Python 3.14.5 (managed by `uv sync`).

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

| # | Benchmark                                  | Reps    | What it stresses              |
| - | ------------------------------------------ | ------- | ----------------------------- |
| 0 | Noise floor                                | 20      | Per-machine variance baseline |
| 1 | Serial compile (`./configure && make -j1`) | 3       | Single-thread compiler perf   |
| 2 | Parallel compile (`make -j<physical>`)     | 3       | Parallel build scaling        |
| 3 | Full regression suite (`make test`)        | 3       | Mixed workload                |
| 4 | SCPSpheresElastic across `-np` sweep       | 5 each  | MPI strong scaling            |
| 5 | Frame generation (yt + matplotlib)         | 3       | I/O, numpy, matplotlib        |
| 6 | gifski encode                              | 3       | CPU codec                     |
| 7 | ffmpeg AV1 (libsvtav1)                     | 3       | CPU codec                     |
| 8 | ffmpeg H.265 (libx265)                     | 3       | CPU codec                     |

The `-np` sweep is `1, 2, 4, 8, …, physical, physical + virtual`, deduplicated. See [Core topology](#core-topology) for the per-platform definition of physical vs virtual.

Per-rep mechanics:

- 1 warmup rep (timed and recorded, flagged as `is_warmup`).
- 30 s cooldown between reps.
- Run order randomized within a sweep (defeats slow drift over the night).
- 1 Hz telemetry: per-core frequency, package power, thermals, memory, load avg.
- Compile benchmarks: cold cache enforced (`git clean -fdx alamo/` + `CCACHE_DISABLE=1`) before each rep.
- SCP runs: a SHA-256 of canonical output fields is recorded — reps producing different hashes are flagged.

## Core topology

Different chips disagree on what "physical" and "virtual" mean. The sweep adapts:

| Platform                        | Physical                                         | Virtual                                  |
| ------------------------------- | ------------------------------------------------ | ---------------------------------------- |
| Intel Xeon (HT enabled)         | `sockets × cores/socket`                         | `physical × (threads/core − 1)`          |
| Apple M5 Pro / M5 Max           | super + performance cores (both `perflevel`s)    | 0                                        |
| Apple M1–M4, base M5            | performance cores (`perflevel0`)                 | efficiency cores (`perflevel1`)          |

Detected from `sysctl hw.perflevel*` + `machdep.cpu.brand_string` on macOS, and `lscpu -J` on Linux. The M5 Pro/Max rule reflects Apple's Fusion Architecture, where the "performance cores" are designed for sustained multithreaded throughput rather than the low-power role of previous E-cores.

## Architecture

```
benchmarks/
├── cli.py              # entry point: alamo-benchmark
├── config.py
├── platform_info.py    # OS, kernel, compiler, MPI versions
├── topology.py         # P/E/super core detection
├── preflight.py        # refuse-to-start gate
├── telemetry/
│   ├── macos.py        # powermetrics + psutil sidecar
│   └── linux.py        # turbostat + RAPL + psutil sidecar
├── runners/            # one file per benchmark
└── storage/            # SQLite schema + writer

configs/
├── default.toml        # full overnight run
└── quick.toml          # smoke-test mode

alamo/                  # git submodule, pinned SHA on `development`
results/<hostname>/     # per-machine output, committed to git
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
| PI-grade statistical rigor: 3 reps for long benchmarks, 5+ for short, median + IQR, σ reported | Single-run timings are unreliable. Mean obscures bimodal distributions. Median + IQR is the standard for noisy systems benchmarks.                                                              |
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
2. Verify the pre-flight passes (`uv run alamo-benchmark preflight`).
3. Kick off the full run on each machine in parallel (`uv run alamo-benchmark run`).
4. Each machine writes to `results/<hostname>/` — no merge conflicts.
5. Commit and push per-machine results once each run completes.
6. Aggregate from any machine after pulling.

## Aggregating across machines

After every machine has pushed its results:

```bash
git pull
sqlite3 <<'SQL'
.mode column
.headers on
ATTACH DATABASE 'results/host-a/run_2026-05-19T03-00-00Z.db' AS host_a;
ATTACH DATABASE 'results/host-b/run_2026-05-19T03-00-00Z.db' AS host_b;
-- ...

SELECT 'host-a' AS host, benchmark, config_json,
       AVG(wall_s) AS mean_s, MIN(wall_s) AS min_s
FROM host_a.result WHERE is_warmup = 0 AND status = 'completed'
GROUP BY benchmark, config_json
UNION ALL
SELECT 'host-b', benchmark, config_json, AVG(wall_s), MIN(wall_s)
FROM host_b.result WHERE is_warmup = 0 AND status = 'completed'
GROUP BY benchmark, config_json;
SQL
```

A formal aggregation/reporting tool is deliberately out of scope for v1 — the schema is small enough that a notebook is the right shape.

## License

See `LICENSE`.
