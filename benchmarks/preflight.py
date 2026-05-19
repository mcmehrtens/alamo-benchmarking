"""Pre-flight checks.

The script refuses to start unless every required check passes. Override with
`--force`, but the failures are recorded in the manifest either way. See README.md
"Pre-flight requirements" for the policy.

This module is observe-only: it never mutates system state.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import psutil

from benchmarks.config import PreflightConfig


@dataclass
class CheckResult:
    name: str
    passed: bool
    observed: str
    required: str
    severity: str = "required"  # 'required' or 'advisory'


def _empty_check_list() -> list[CheckResult]:
    return []


@dataclass
class PreflightReport:
    checks: list[CheckResult] = field(default_factory=_empty_check_list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks if c.severity == "required")

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "checks": [asdict(c) for c in self.checks]}


def run_preflight(
    cfg: PreflightConfig,
    *,
    output_dir: Path,
    require_alamo_clean: bool = True,
) -> PreflightReport:
    report = PreflightReport()
    add = report.checks.append

    system = platform.system()

    add(_check_ac(cfg))
    add(_check_perf_mode(cfg, system))
    add(_check_governor(cfg, system))
    add(_check_turbo(system))
    add(_check_rapl(system))
    add(_check_load(cfg))
    add(_check_disk(cfg, output_dir))
    add(_check_uptime(cfg))
    add(_check_tools())
    add(_check_sudo())
    if require_alamo_clean:
        add(_check_alamo_clean())
    return report


# ----------------------------------------------------------------------------


def _check_ac(cfg: PreflightConfig) -> CheckResult:
    try:
        bat = psutil.sensors_battery()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    except AttributeError, NotImplementedError:
        bat = None

    severity = "required" if cfg.require_ac else "advisory"
    if bat is None:
        return CheckResult("ac_power", True, "no battery (desktop/server)", "AC", severity)
    if bool(bat.power_plugged):  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        return CheckResult("ac_power", True, "plugged in", "AC", severity)
    return CheckResult("ac_power", not cfg.require_ac, "on battery", "AC", severity)


def _check_perf_mode(cfg: PreflightConfig, system: str) -> CheckResult:
    if system != "Darwin":
        return CheckResult("macos_perf_mode", True, "n/a", "n/a", "advisory")
    severity = "required" if cfg.require_macos_perf_mode else "advisory"
    try:
        out = subprocess.run(["pmset", "-g"], check=True, capture_output=True, text=True).stdout
    except subprocess.CalledProcessError, FileNotFoundError:
        return CheckResult(
            "macos_perf_mode",
            not cfg.require_macos_perf_mode,
            "pmset failed",
            "highpowermode=1",
            severity,
        )

    high = "highpowermode" in out and _pmset_value(out, "highpowermode") == "1"
    low = "lowpowermode" in out and _pmset_value(out, "lowpowermode") == "1"
    observed = f"high={'1' if high else '0'} low={'1' if low else '0'}"
    ok = (high or not cfg.require_macos_perf_mode) and not low
    return CheckResult("macos_perf_mode", ok, observed, "high=1, low=0", severity)


def _pmset_value(text: str, key: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(key):
            return stripped.split()[-1]
    return ""


def _check_governor(cfg: PreflightConfig, system: str) -> CheckResult:
    if system != "Linux" or cfg.require_governor == "any":
        observed = "n/a" if system != "Linux" else "not required"
        return CheckResult("cpu_governor", True, observed, cfg.require_governor, "advisory")
    governors = _all_cpu_governors()
    observed = ",".join(sorted(set(governors))) if governors else "unknown"
    ok = bool(governors) and all(g == cfg.require_governor for g in governors)
    return CheckResult("cpu_governor", ok, observed, cfg.require_governor)


def _all_cpu_governors() -> list[str]:
    governors: list[str] = []
    cpu_root = Path("/sys/devices/system/cpu")
    if not cpu_root.exists():
        return governors
    for cpu_dir in sorted(cpu_root.glob("cpu[0-9]*")):
        gov_file = cpu_dir / "cpufreq" / "scaling_governor"
        try:
            governors.append(gov_file.read_text().strip())
        except OSError:
            continue
    return governors


def _check_turbo(system: str) -> CheckResult:
    if system != "Linux":
        return CheckResult("turbo_boost", True, "n/a (uncontrolled on macOS)", "on", "advisory")
    no_turbo_path = Path("/sys/devices/system/cpu/intel_pstate/no_turbo")
    if not no_turbo_path.exists():
        return CheckResult("turbo_boost", True, "intel_pstate not present", "on", "advisory")
    try:
        no_turbo = no_turbo_path.read_text().strip()
    except OSError:
        return CheckResult("turbo_boost", True, "unreadable", "on", "advisory")
    return CheckResult(
        "turbo_boost", no_turbo == "0", f"no_turbo={no_turbo}", "no_turbo=0", "advisory"
    )


# Fraction of `constraint_0_max_power_uw` (the package's stated maximum / TDP-class
# value) below which we treat PL1 as "significantly throttled". Lab boxes set to
# 80% of TDP for thermal headroom are common and acceptable; anything below this
# is suspicious enough to surface in the manifest.
_RAPL_PL1_THRESHOLD = 0.80
_RAPL_ROOT = Path("/sys/class/powercap")


def _check_rapl(system: str, *, root: Path = _RAPL_ROOT) -> CheckResult:
    """Verify Intel RAPL PL1 isn't capping the package well below its rated max.

    Reads each `intel-rapl:<N>` package's `constraint_0_power_limit_uw` (PL1, the
    long-term sustained limit) and compares it to `constraint_0_max_power_uw`
    (the highest value PL1 can be set to, typically the chip's TDP). If any
    package is running PL1 below `_RAPL_PL1_THRESHOLD` of its max, that means
    something (firmware, OEM tuning, thermal cap) has dialed the sustained
    budget down — Alamo's wall times on that box would not be comparable to a
    box at TDP.

    Advisory only. Pre-flight is observe-only (see CLAUDE.md): we report,
    user decides. Skips cleanly on macOS, AMD, or kernels without RAPL exposed.
    """
    if system != "Linux":
        return CheckResult("rapl_pl1", True, "n/a (no RAPL on macOS)", "PL1 ~= max", "advisory")
    if not root.is_dir():
        return CheckResult(
            "rapl_pl1", True, "powercap sysfs missing", "PL1 ~= max", "advisory"
        )
    package_dirs = sorted(p for p in root.iterdir() if p.name.startswith("intel-rapl:"))
    # Filter to top-level package zones (the `:N` form; sub-zones look like `:N:M`).
    package_dirs = [p for p in package_dirs if p.name.count(":") == 1]
    if not package_dirs:
        return CheckResult(
            "rapl_pl1", True, "no intel-rapl packages", "PL1 ~= max", "advisory"
        )

    summaries: list[str] = []
    ok = True
    for pkg in package_dirs:
        pl1_uw = _read_int(pkg / "constraint_0_power_limit_uw")
        max_uw = _read_int(pkg / "constraint_0_max_power_uw")
        if pl1_uw is None or max_uw is None or max_uw <= 0:
            summaries.append(f"{pkg.name}=unreadable")
            continue
        ratio = pl1_uw / max_uw
        summaries.append(f"{pkg.name}=PL1 {pl1_uw / 1_000_000:.0f}W/{max_uw / 1_000_000:.0f}W")
        if ratio < _RAPL_PL1_THRESHOLD:
            ok = False
    observed = "; ".join(summaries)
    required = f"PL1 >= {int(_RAPL_PL1_THRESHOLD * 100)}% of max per package"
    return CheckResult("rapl_pl1", ok, observed, required, "advisory")


def _read_int(path: Path) -> int | None:
    """Read a single integer from a sysfs node, or None if missing/unparseable."""
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _check_load(cfg: PreflightConfig) -> CheckResult:
    load1, _, _ = psutil.getloadavg()
    return CheckResult(
        "load_average",
        load1 <= cfg.max_load_1min,
        f"{load1:.2f}",
        f"<= {cfg.max_load_1min}",
    )


def _check_disk(cfg: PreflightConfig, output_dir: Path) -> CheckResult:
    target = output_dir.resolve()
    while not target.exists() and target.parent != target:
        target = target.parent
    free_gb = psutil.disk_usage(str(target)).free / 1024**3
    return CheckResult(
        "disk_free",
        free_gb >= cfg.min_disk_free_gb,
        f"{free_gb:.1f} GB",
        f">= {cfg.min_disk_free_gb} GB",
    )


def _check_uptime(cfg: PreflightConfig) -> CheckResult:
    uptime_days = (time.time() - psutil.boot_time()) / 86400
    return CheckResult(
        "uptime",
        uptime_days <= cfg.max_uptime_days,
        f"{uptime_days:.1f} days",
        f"<= {cfg.max_uptime_days} days",
        severity="advisory",
    )


_REQUIRED_TOOLS = ("clang++", "mpiexec", "make", "git")


def _check_tools() -> CheckResult:
    missing = [t for t in _REQUIRED_TOOLS if shutil.which(t) is None]
    return CheckResult(
        "required_tools",
        not missing,
        f"missing: {missing}" if missing else "all present",
        ",".join(_REQUIRED_TOOLS),
    )


def _check_sudo() -> CheckResult:
    try:
        result = subprocess.run(
            ["sudo", "-n", "true"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        ok = result.returncode == 0
    except FileNotFoundError, subprocess.TimeoutExpired:
        ok = False
    return CheckResult(
        "sudo_cached",
        ok,
        "available" if ok else "not cached",
        "passwordless or pre-cached",
        severity="advisory",
    )


def _check_alamo_clean() -> CheckResult:
    alamo = Path("alamo")
    if not alamo.exists():
        return CheckResult("alamo_submodule", False, "missing", "submodule present at ./alamo")
    if not (alamo / ".git").exists():
        return CheckResult(
            "alamo_submodule", False, "submodule not initialized", "git submodule update --init"
        )
    try:
        out = subprocess.run(
            ["git", "-C", str(alamo), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        return CheckResult("alamo_clean", False, f"git failed: {e}", "clean")
    return CheckResult(
        "alamo_clean",
        out == "",
        "clean" if out == "" else f"dirty: {out.splitlines()[0]}",
        "clean",
    )
