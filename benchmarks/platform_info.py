"""Capture full host metadata for the manifest and the `host` table.

Anything that could conceivably matter for reproducing a benchmark goes here.

`machine_id` is the canonical per-machine identifier used for the results
directory name. It is REQUIRED for `run`: a machine has to declare one before
any benchmarks execute. We don't derive it from hostname because macOS mDNS
reports a different short hostname depending on which network the machine is
on (`foo.local` at home, `foo.lab.example.edu` on the campus VPN), which would
silently fork the same machine's results into multiple directories.

The machine_id is sourced from (in priority order):
  1. `ALAMO_BENCHMARK_MACHINE_ID` environment variable.
  2. `~/.alamo-benchmark/machine_id` (single line of text).
If neither is set, `machine_id` is None and the CLI refuses to start `run`.
"""

from __future__ import annotations

import os
import platform
import re
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import psutil

_MACHINE_ID_ENV = "ALAMO_BENCHMARK_MACHINE_ID"
_MACHINE_ID_FILE = Path.home() / ".alamo-benchmark" / "machine_id"
_MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_MACHINE_ID_MAX_LEN = 64


def _empty_str_dict() -> dict[str, str]:
    return {}


@dataclass(frozen=True)
class PlatformInfo:
    hostname: str
    machine_id: str | None
    machine_id_source: str
    os_name: str
    os_version: str
    kernel: str
    arch: str
    cpu_brand: str
    ram_gb: float
    disk_free_gb: float
    fs_type: str
    on_ac: bool | None
    governor: str
    perf_mode: str
    uptime_seconds: int
    tool_versions: dict[str, str] = field(default_factory=_empty_str_dict)
    raw_env: dict[str, str] = field(default_factory=_empty_str_dict)

    def to_manifest(self) -> dict[str, Any]:
        return asdict(self)


_TOOLS = ("clang", "clang++", "mpiexec", "make", "ffmpeg", "gifski", "git", "uv")
_ENV_KEYS = (
    "PATH",
    "CC",
    "CXX",
    "CFLAGS",
    "CXXFLAGS",
    "LDFLAGS",
    "OMPI_MCA_btl",
    "OMPI_MCA_orte_report_bindings",
    "MPICH_CC",
    "MPICH_CXX",
    "CCACHE_DISABLE",
    "CCACHE_DIR",
    "HOMEBREW_PREFIX",
)


def collect(output_dir: Path) -> PlatformInfo:
    """Gather everything we want to record for this machine, right now."""
    machine_id, source = _machine_id()
    return PlatformInfo(
        hostname=socket.gethostname(),
        machine_id=machine_id,
        machine_id_source=source,
        os_name=platform.system(),
        os_version=_os_version(),
        kernel=platform.release(),
        arch=platform.machine(),
        cpu_brand=_cpu_brand(),
        ram_gb=round(psutil.virtual_memory().total / 1024**3, 2),
        disk_free_gb=round(
            psutil.disk_usage(str(_existing_ancestor(output_dir))).free / 1024**3, 2
        ),
        fs_type=_fs_type(output_dir),
        on_ac=_on_ac(),
        governor=_governor(),
        perf_mode=_perf_mode(),
        uptime_seconds=int(_uptime_seconds()),
        tool_versions=_tool_versions(),
        raw_env=_collect_env(),
    )


# ----------------------------------------------------------- platform-aware helpers


def _os_version() -> str:
    if platform.system() == "Darwin":
        return f"macOS {platform.mac_ver()[0]}"
    return platform.version()


def _cpu_brand() -> str:
    if platform.system() == "Darwin":
        try:
            return subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        except subprocess.CalledProcessError, FileNotFoundError:
            return platform.processor()

    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor()


def _existing_ancestor(path: Path) -> Path:
    """Walk up `path` until we find a directory that exists.

    Useful for asking about disk space of a yet-to-be-created output directory.
    """
    p = path.resolve()
    while not p.exists():
        if p.parent == p:
            return p
        p = p.parent
    return p


def _fs_type(path: Path) -> str:
    """Filesystem type for `path` — answers 'am I on a network FS by accident?'"""
    target = str(_existing_ancestor(path))
    try:
        best_match = ""
        best_type = ""
        for part in psutil.disk_partitions(all=False):
            mp = part.mountpoint
            if target.startswith(mp) and len(mp) > len(best_match):
                best_match = mp
                best_type = part.fstype
        return best_type or "unknown"
    except OSError:
        return "unknown"


def _on_ac() -> bool | None:
    """True/False if we can determine power source, None otherwise."""
    try:
        bat = psutil.sensors_battery()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    except AttributeError, NotImplementedError:
        return None
    if bat is None:
        return True  # no battery -> desktop/server, assume AC
    return bool(bat.power_plugged)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]


def _governor() -> str:
    """CPU scaling governor on Linux; 'n/a' elsewhere. Reports the first CPU's
    governor — the preflight checker verifies all CPUs match."""
    if platform.system() != "Linux":
        return "n/a"
    try:
        return Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read_text().strip()
    except OSError:
        return "unknown"


def _perf_mode() -> str:
    """macOS High Power Mode status; 'n/a' elsewhere."""
    if platform.system() != "Darwin":
        return "n/a"
    try:
        out = subprocess.run(
            ["pmset", "-g"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith("highpowermode"):
                return f"high_power={stripped.split()[-1]}"
            if stripped.startswith("lowpowermode"):
                return f"low_power={stripped.split()[-1]}"
        return "unknown"
    except subprocess.CalledProcessError, FileNotFoundError:
        return "unknown"


def _uptime_seconds() -> float:
    boot = psutil.boot_time()
    return max(0.0, time.time() - boot) if boot else 0.0


def _tool_versions() -> dict[str, str]:
    versions: dict[str, str] = {tool: _tool_version(tool) for tool in _TOOLS}
    versions["python"] = sys.version.replace("\n", " ")
    return versions


def _tool_version(tool: str) -> str:
    """Best-effort one-line version capture for an external tool."""
    candidates: list[list[str]] = [[tool, "--version"], [tool, "-version"], [tool, "-V"]]
    for args in candidates:
        try:
            res = subprocess.run(
                args,
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            text = (res.stdout or res.stderr).strip()
            if not text:
                continue
            return text.splitlines()[0].strip()
        except FileNotFoundError, subprocess.TimeoutExpired:
            continue
    return "not_found"


def _collect_env() -> dict[str, str]:
    return {k: os.environ[k] for k in _ENV_KEYS if k in os.environ}


def _machine_id() -> tuple[str | None, str]:
    """Resolve the per-machine identifier and report where it came from.

    Returns ``(machine_id, source)`` where source is one of:
      - ``"env"``  — from `$ALAMO_BENCHMARK_MACHINE_ID`
      - ``"file"`` — from `~/.alamo-benchmark/machine_id`
      - ``"unset"`` — neither set; caller must refuse to write results
      - ``"invalid:<reason>"`` — a value was provided but rejected
    """
    raw_env = os.environ.get(_MACHINE_ID_ENV)
    if raw_env is not None:
        return _validate_id(raw_env, source_label="env")

    if _MACHINE_ID_FILE.is_file():
        try:
            raw_file = _MACHINE_ID_FILE.read_text().strip()
        except OSError as e:
            return None, f"invalid:file_read_error:{e}"
        if raw_file:
            return _validate_id(raw_file, source_label="file")

    return None, "unset"


def _validate_id(raw: str, *, source_label: str) -> tuple[str | None, str]:
    candidate = raw.strip()
    if not candidate:
        return None, f"invalid:{source_label}_empty"
    if len(candidate) > _MACHINE_ID_MAX_LEN:
        return None, f"invalid:{source_label}_too_long"
    if not _MACHINE_ID_RE.match(candidate):
        return None, f"invalid:{source_label}_bad_chars"
    return candidate, source_label
