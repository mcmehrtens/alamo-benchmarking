# Alamo benchmark — per-machine setup checklist

Run through this once per lab machine **before** kicking off `alamo-benchmark run`. The goal is "results my PI will trust" — same baseline everywhere, no background noise, vanilla per-OS toolchain.

The script itself is observe-only (preflight checks system state, doesn't change it). Everything below you do yourself, once, then keep it ticking off before each overnight run.

---

## 0. Dedicated benchmark user account

A clean user profile keeps random LaunchAgents / autostart apps / dotfile cruft from polluting the run. Skip only if the machine is already a single-purpose box.

**macOS:** System Settings → Users & Groups → Add Account → "Administrator". Log in as that user. Don't sign into iCloud.

**Ubuntu/Linux:**
```bash
sudo adduser benchmark
sudo usermod -aG sudo benchmark
# log out, log back in as `benchmark`
```

Both: the account must have `sudo` (needed for `powermetrics` / `turbostat`).

---

## 1. System update + reboot

You want a known kernel, current security patches, no pending updates the OS will silently apply mid-run.

**macOS:** System Settings → General → Software Update → install everything pending → reboot.

**Ubuntu/Linux:**
```bash
sudo apt update && sudo apt upgrade -y && sudo apt autoremove -y
sudo reboot
```

After the reboot, **log in fresh** before doing anything else.

---

## 2. Install dependencies

Python is managed by `uv` end-to-end — no system pip or Homebrew Python anywhere.

### 2a. macOS

```bash
# Xcode Command Line Tools (Apple Clang, make, git)
xcode-select --install

# Homebrew — https://brew.sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Alamo build deps
brew update
brew install openmpi eigen libpng git

# ffmpeg with AV1 + H.265 encoders for the render benchmark
brew install ffmpeg
ffmpeg -hide_banner -encoders 2>/dev/null | grep -E "libsvtav1|libx265"
# Must show both. If either is missing, switch to the wider build:
#   brew tap homebrew-ffmpeg/ffmpeg
#   brew install homebrew-ffmpeg/ffmpeg/ffmpeg --with-svt-av1
# and re-verify.

# uv — the only Python toolchain you need
curl -LsSf https://astral.sh/uv/install.sh | sh
# (or `brew install uv` if you prefer)
exec $SHELL -l                    # pick up the new PATH
uv python install 3.14.5          # one-time download
```

`powermetrics` ships with macOS; nothing to install for telemetry.

### 2b. Ubuntu 24.04

Mirrors `alamo/.github/workflows/dependencies-ubuntu-24.04.sh` except Python — we use `uv`, not `apt`'s `python3-yt` etc.

```bash
sudo apt update
sudo apt install -y build-essential g++ git curl xz-utils

# Alamo build deps
sudo apt install -y libopenmpi-dev libeigen3-dev libpng-dev

# LLVM clang — the vanilla Linux compiler for our purposes
sudo apt install -y clang libstdc++-14-dev

# ffmpeg with AV1 + H.265 encoders
sudo apt install -y ffmpeg
ffmpeg -hide_banner -encoders 2>/dev/null | grep -E "libsvtav1|libx265"
# Must show both. If missing on your kernel/release, build from source or
# add a PPA like ppa:ubuntu-toolchain-r/ppa.

# turbostat — telemetry sidecar on Linux
sudo apt install -y linux-tools-common linux-tools-generic "linux-tools-$(uname -r)"
which turbostat || sudo ln -s "/usr/lib/linux-tools/$(uname -r)/turbostat" /usr/local/bin/turbostat

# uv — the only Python toolchain you need
curl -LsSf https://astral.sh/uv/install.sh | sh
exec $SHELL -l                    # pick up the new PATH
uv python install 3.14.5
```

### 2c. Both platforms — gifski (binary release)

```bash
curl -LO https://github.com/ImageOptim/gifski/releases/download/1.34.0/gifski-1.34.0.tar.xz
tar -xJf gifski-1.34.0.tar.xz
# Inspect — release contains prebuilt binaries under per-platform subdirs.
ls gifski-1.34.0/
# Install the binary for your platform:
sudo install -m 0755 gifski-1.34.0/mac/gifski   /usr/local/bin/gifski   # macOS
sudo install -m 0755 gifski-1.34.0/linux/gifski /usr/local/bin/gifski   # Ubuntu
gifski --version    # verify
```

(If the tarball layout differs from what the URL suggests, `find gifski-1.34.0 -name gifski -type f` will surface the binaries.)

---

## 3. Clone the benchmark repo + submodule

```bash
git clone <repo-url> alamo-benchmarking
cd alamo-benchmarking
git submodule update --init --recursive
```

The submodule pins Alamo at the chosen `development` SHA. Don't update it mid-experiment.

---

## 4. Python environment for Alamo's regression tests

`scripts/runtests.py` and its per-test scripts import `yt`, `matplotlib`, `numpy`, `pandas`, and `xmltodict`. We create an isolated venv inside the alamo submodule via `uv`; the regression runner picks it up automatically (prepends `alamo/.venv/bin` to PATH for the subprocess).

```bash
cd alamo
uv venv --python 3.14.5
uv pip install --python .venv/bin/python yt matplotlib numpy pandas xmltodict
cd ..
```

Sanity check:
```bash
alamo/.venv/bin/python -c "import yt, matplotlib, numpy, pandas, xmltodict; print('ok')"
```

The benchmark script itself runs under `uv run alamo-benchmark ...` and uses the project's own venv (auto-managed via the top-level `pyproject.toml`). The two venvs don't conflict — the regression runner explicitly hands the alamo venv's PATH to the `runtests.py` subprocess and nothing else.

---

## 4b. Set this machine's `machine_id` (REQUIRED, one-time)

The benchmark refuses to start `run` unless this machine has declared a stable identifier. `socket.gethostname()` is unreliable on macOS in particular — mDNS / Bonjour reports different short hostnames depending on which network the machine is on (`foo.local` at home, `foo.lab.example.edu` on the campus VPN), and we'd silently fork the same machine's results into multiple `results/<host>/` dirs.

Pick a short, stable, **human-readable** identifier. Allowed characters are `[A-Za-z0-9._-]`, max 64 chars. Examples: `iastate-m1pro-01`, `lab-xeon-w-1370`, `mehrtens-m1`.

Set it via **one** of the following (the env var wins if both are set):

```bash
# Option A — persistent across shells/reboots. Recommended.
mkdir -p ~/.alamo-benchmark
echo "iastate-m1pro-01" > ~/.alamo-benchmark/machine_id

# Option B — env var. Add to ~/.zshrc or ~/.bashrc on the benchmark user.
export ALAMO_BENCHMARK_MACHINE_ID=iastate-m1pro-01
```

Verify:

```bash
uv run alamo-benchmark describe | head -2
# Should print:
#   machine_id:     iastate-m1pro-01  [file]    (or  [env])
#   Hostname:       <whatever the OS reports>
```

If `machine_id:` shows `(unset)`, `run` will refuse with a clear error pointing back here. Once set, all results land under `results/<machine_id>/run_<ts>/` regardless of which network or hostname the machine is on.

---

## 4c. (Optional, per new chip family) Capture a telemetry parser fixture

Skip if this machine's chip is already covered under `tests/fixtures/`. Today: **macOS** — M1 Pro, M4 Pro, M5 Pro. **Linux** — Xeon W-1370, Xeon W5-2545.

If you're adding a NEW chip family (e.g. an M3 Max, an Epyc, a different Xeon SKU), capture a short telemetry sample so the parser unit tests pick up its layout. The capture commands use the same flags as the live telemetry sidecar so the test sees byte-identical formatting, plus a sample-count cap (5) so the fixture file stays small — the unit tests assert exactly 5 samples.

Keep the machine otherwise idle while the capture runs so the output is representative.

### 4c-1. macOS — powermetrics

```bash
sudo powermetrics \
    --format plist \
    --samplers cpu_power,gpu_power,thermal \
    -i 1000 \
    --sample-count 5 \
  > tests/fixtures/powermetrics_macos_<chip-slug>.plist
```

`<chip-slug>` should be a short lowercase identifier — match the existing names (`m1pro`, `m4pro`, `m5pro`). Capture takes ~5 s.

### 4c-2. Linux — turbostat

```bash
sudo turbostat \
    --interval 1 \
    --quiet \
    --num_iterations 5 \
  > tests/fixtures/turbostat_linux_<cpu-slug>.txt
```

`<cpu-slug>` should encode the CPU family/model — match the existing names (`xeon-w-1370`, `xeon-w5-2545`). Capture takes ~5 s.

### 4c-3. Add parser assertions for the new fixture

Drop the fixture into `tests/fixtures/`, then add a couple of assertions to `tests/test_telemetry_macos.py` or `tests/test_telemetry_linux.py` modeled on the existing per-chip tests — minimum:

- `assert len(samples) == 5` — capture produced 5 samples
- `assert len(samples[0].per_core) == <logical CPU count>` — per-chip per-core count matches what you expect
- For macOS: `_core_types(samples[0])` includes the right cluster types (`super`/`performance`/`efficiency` per the chip's design)
- For Linux: `package_power_w` is non-None and within a plausible TDP range

Then:

```bash
uv run pytest tests/test_telemetry_macos.py     # or _linux
```

If the test fails without parser changes, the parser is mis-handling the new chip — fix the parser, don't loosen the test.

---

## 5. Pre-run system configuration

These are the settings preflight checks **for** but does NOT set. You set them yourself; preflight verifies.

### 5a. macOS

- **Power:** Plug into AC. System Settings → Battery:
  - "Low Power Mode" = Never
  - "High Power Mode" = "Always" (Pro/Max/Ultra Macs only)
  - Uncheck "Slightly dim the display while on battery"
- **Sleep / wake:**
  ```bash
  sudo pmset -a sleep 0 displaysleep 0 disksleep 0 powernap 0
  sudo pmset -a tcpkeepalive 0
  ```
  Optional belt-and-braces: leave `caffeinate -dimsu &` running for the duration, `kill` it after.
- **Background activity — quit or disable:**
  - Time Machine: System Settings → General → Time Machine → off
  - Spotlight: let pending indexing finish, then `sudo mdutil -a -i off`
  - iCloud sync: sign out on this user (or pause syncing on every category)
  - Browser, Slack, email, IDE — quit
  - System Settings → General → AirDrop & Handoff → both off
- **Bluetooth:** off (Control Center → Bluetooth → off). Sometimes adds thermal noise.
- **Wi-Fi:** off if the run doesn't need network. Telemetry is local; only keep Wi-Fi on for SSH.
- **Display:** never sleep, no screensaver, don't lock the screen.

### 5b. Ubuntu/Linux

```bash
# AC power: 1 = plugged in (file is absent on desktops/servers with no battery)
cat /sys/class/power_supply/AC*/online 2>/dev/null

# CPU governor → performance, all cores
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor    # must read "performance"

# Turbo boost must be ON (intel_pstate)
cat /sys/devices/system/cpu/intel_pstate/no_turbo            # must read "0"

# Disable GNOME sleep / screen lock for THIS user
gsettings set org.gnome.desktop.session idle-delay 0
gsettings set org.gnome.desktop.screensaver lock-enabled false
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-battery-type 'nothing'

# Disable suspend / hibernate system-wide
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target

# Stop the noisiest background services + timers (kills CPU wakeups mid-run)
sudo systemctl stop unattended-upgrades.service
sudo systemctl stop apt-daily.timer apt-daily-upgrade.timer
sudo systemctl stop fstrim.timer logrotate.timer man-db.timer motd-news.timer mlocate.timer 2>/dev/null
# Optional: mask them so a reboot doesn't bring them back
sudo systemctl mask apt-daily.timer apt-daily-upgrade.timer

# Hold snap auto-refresh for 24 h (longest single hold the snap tool accepts)
sudo snap refresh --hold=24h 2>/dev/null || true

# Audit what's still scheduled to fire — kill anything you don't recognize
systemctl list-timers --all

# Bluetooth + Wi-Fi if not needed
sudo systemctl stop bluetooth.service
sudo rfkill block bluetooth
nmcli radio wifi off            # leave ethernet on if you SSH'd in

# Verify NTP sync (timestamps join telemetry to results across machines)
timedatectl status               # "System clock synchronized: yes"

# Optional — disable swap if you have ≥ 32 GB RAM and want to avoid swap noise
sudo swapoff -a
# Re-enable later: sudo swapon -a
```

After the run, undo the bits you want back:
```bash
sudo systemctl unmask apt-daily.timer apt-daily-upgrade.timer
sudo systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target
sudo systemctl start bluetooth.service
nmcli radio wifi on
sudo swapon -a
```

### 5c. Both

- **NTP synced:** see above. Clock skew across machines breaks cross-machine aggregation.
- **No other heavy users on the box.** SSH sessions are fine, but don't run anything else.

---

## 6. Sanity check before kicking off

```bash
# Refresh the sudo ticket — telemetry sidecar's keepalive can't get the
# initial credential without you typing the password.
sudo -v

# Topology + tool versions printed in one go
uv run alamo-benchmark describe

# Preflight verifies everything above. PASS = ready.
uv run alamo-benchmark preflight
```

Read the output. Every required check should show `[OK ]`. Advisory failures are fine to ignore if you understand them.

Don't `--force` past a real failure; the manifest will record the override but your PI will ask about it.

---

## 7. Run the suite

```bash
# Short shakedown (~30–45 min on an M1 Pro). Run this before every overnight on a new box.
uv run alamo-benchmark run --config configs/validate.toml

# (Optional) Time the regression suite in isolation before the overnight on a NEW machine.
# Regression is the single largest unknown in the overnight budget — if you've never run
# Alamo's regression on this hardware before, use this to size the budget.
uv run alamo-benchmark run --config configs/regression_only.toml

# The real overnight run. Budgeted to fit within 12 h on the slowest expected lab box.
uv run alamo-benchmark run --config configs/default.toml
```

Walk away. The overnight run's timing breakdown on the M1 Pro reference:

- noise floor: < 1 min
- compile_serial + compile_parallel (2 reps each, 2D + 3D): ~30 min total
- regression_suite (2 reps): 1–3 h (the most variable piece — depends on machine speed and which tests touch network)
- scp_elastic (1 warmup + 5 reps × 5 core counts): ~7 h at the configured `stop_time = 0.0003_s`
- render_frames + render_encode: ~10 min

Total: ~9–11 h. SCP and telemetry sample continuously across all of them.

The run writes everything under `results/<hostname>/run_<ts>/`:
- `alamo-benchmark.log` — full run log (mirrors stdout)
- `run_<ts>.db` — SQLite results
- `run_<ts>.manifest.json` — platform + preflight + git SHAs
- `logs/` — per-rep compile, regression, SCP, and render-frame subprocess logs
- `render/` — `frames_rep*/` PNGs and `encode_<codec>_rep*.{gif,webm,mp4}` outputs

After it finishes, **commit the results**:
```bash
git add results/<hostname>/
git commit -m "<hostname>: $(date -u +%Y-%m-%d) benchmark run"
git push
```

The DB + manifest are tiny (≤ few MB per machine even for an overnight run). Rendered frames and encoded videos are gitignored — they're per-machine artifacts, not canonical data.

---

## 8. Re-run prep (every subsequent time on the same machine)

Redo steps **5** (system config) and **6** (sanity check). Skip 0–4 unless something material changed (OS upgrade, new user account, new dependency, new Alamo SHA).

---

## Troubleshooting

- **`alamo-benchmark` exits with `Pre-flight failed`.** Read the failed-check rows. Common causes: not plugged in, `git -C alamo status` is dirty (run `git -C alamo clean -fdx`), or a required tool isn't on `$PATH`.
- **SCP reps fail in < 1 s with exit code 213 (macOS).** OpenMPI/PRRTE on macOS can't bind processes to cores. The runner skips `--bind-to core --map-by core` on Darwin; if you still see this, your `openmpi` install is broken. `brew reinstall openmpi`.
- **Regression suite fails with `ModuleNotFoundError: yt`.** `alamo/.venv` isn't set up. Re-do step 4.
- **`telemetry_sample` is empty after the run.** Sudo ticket expired (despite the keepalive) or `powermetrics` / `turbostat` isn't reachable. Verify `which powermetrics` (mac) / `which turbostat` (linux) and `sudo -v` immediately before kicking off.
- **Compile reps fail at AMReX clone.** Alamo's configure clones AMReX from GitHub each rep (cold cache). If GitHub is rate-limiting you, expect occasional failed reps — the run continues. If it's every rep, check network and `git ls-remote https://github.com/AMReX-Codes/amrex.git`.
