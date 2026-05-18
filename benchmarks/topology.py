"""Per-platform CPU core topology detection.

The cross-machine `-np` sweep depends on a consistent definition of "physical" and
"virtual" cores. Intel Xeon hyperthreading is the canonical case. Apple Silicon
complicates things because perflevel1 can be either efficiency cores (M1-M4, base
M5) — which we treat like HT threads — or performance cores (M5 Pro/Max with
Fusion Architecture) — which we treat as physical.

See CLAUDE.md "Apple Silicon core types" for the full ruleset.
"""

from __future__ import annotations

import json
import platform
import subprocess
from dataclasses import asdict, dataclass
from typing import Any

# Chips that use Apple's Fusion Architecture, where `perflevel1` cores are
# sustained-throughput "performance" cores rather than low-power efficiency cores.
# Detected by substring match against `sysctl machdep.cpu.brand_string`.
_FUSION_CHIP_MARKERS: tuple[str, ...] = ("M5 Pro", "M5 Max")


@dataclass(frozen=True)
class Topology:
    """A machine's CPU core layout for the purposes of MPI core-count sweeps."""

    physical: int  # cores that count toward `-np <physical>`
    virtual: int  # extra cores available for `-np <physical + virtual>`
    super_cores: int  # Apple Silicon "super" cores (0 elsewhere)
    perf_cores: int  # Apple Silicon performance cores (0 on x86)
    eff_cores: int  # Apple Silicon efficiency cores (0 on x86)
    cpu_brand: str
    classification_reason: str  # human-readable note for the manifest

    def core_sweep(self, extra: tuple[int, ...] = ()) -> list[int]:
        """Sweep used by the SCP -np progression.

        Powers of two up to `physical`, then `physical`, then `physical + virtual`,
        deduplicated and sorted. Additional explicit values may be supplied via
        `extra`.
        """
        sweep: set[int] = set()
        n = 1
        while n <= self.physical:
            sweep.add(n)
            n *= 2
        sweep.add(self.physical)
        if self.virtual > 0:
            sweep.add(self.physical + self.virtual)
        sweep.update(int(x) for x in extra)
        return sorted(x for x in sweep if x >= 1)

    def to_manifest(self) -> dict[str, Any]:
        return asdict(self)


def detect_topology() -> Topology:
    system = platform.system()
    if system == "Darwin":
        return _detect_macos()
    if system == "Linux":
        return _detect_linux()
    raise RuntimeError(f"Unsupported platform for topology detection: {system}")


# ---------------------------------------------------------------------------- macOS


def _sysctl(name: str) -> str:
    """Return the trimmed string value of a sysctl key, or '' if missing."""
    try:
        return subprocess.run(
            ["sysctl", "-n", name],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError, FileNotFoundError:
        return ""


def _sysctl_int(name: str) -> int:
    raw = _sysctl(name)
    return int(raw) if raw else 0


def _detect_macos() -> Topology:
    brand = _sysctl("machdep.cpu.brand_string")
    perflevel0 = _sysctl_int("hw.perflevel0.physicalcpu")
    perflevel1 = _sysctl_int("hw.perflevel1.physicalcpu")

    is_fusion = any(marker in brand for marker in _FUSION_CHIP_MARKERS)

    if is_fusion:
        # perflevel0 = super cores, perflevel1 = performance cores.
        # Both count as physical; no virtual tier.
        physical = perflevel0 + perflevel1
        virtual = 0
        reason = (
            f"Fusion Architecture chip ({brand}): perflevel0 ({perflevel0} super) + "
            f"perflevel1 ({perflevel1} performance) both treated as physical."
        )
        return Topology(
            physical=physical,
            virtual=virtual,
            super_cores=perflevel0,
            perf_cores=perflevel1,
            eff_cores=0,
            cpu_brand=brand,
            classification_reason=reason,
        )

    # Non-Fusion M-series: perflevel0 = performance, perflevel1 = efficiency.
    # Efficiency cores treated like Xeon HT threads (virtual).
    return Topology(
        physical=perflevel0,
        virtual=perflevel1,
        super_cores=0,
        perf_cores=perflevel0,
        eff_cores=perflevel1,
        cpu_brand=brand,
        classification_reason=(
            f"Non-Fusion Apple Silicon ({brand}): {perflevel0} performance cores "
            f"treated as physical, {perflevel1} efficiency cores treated as virtual."
        ),
    )


# ---------------------------------------------------------------------------- Linux


def _detect_linux() -> Topology:
    try:
        out = subprocess.run(
            ["lscpu", "-J"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise RuntimeError(f"lscpu unavailable: {e}") from e

    parsed: dict[str, list[dict[str, str]]] = json.loads(out)
    fields = {entry["field"].rstrip(":"): entry["data"] for entry in parsed["lscpu"]}

    sockets = int(fields.get("Socket(s)", "1"))
    cores_per_socket = int(fields.get("Core(s) per socket", "1"))
    threads_per_core = int(fields.get("Thread(s) per core", "1"))
    brand = fields.get("Model name", "Unknown CPU")

    physical = sockets * cores_per_socket
    virtual = physical * (threads_per_core - 1)

    return Topology(
        physical=physical,
        virtual=virtual,
        super_cores=0,
        perf_cores=physical,
        eff_cores=0,
        cpu_brand=brand,
        classification_reason=(
            f"Linux x86_64 ({brand}): {sockets} socket(s) x {cores_per_socket} "
            f"cores = {physical} physical, {threads_per_core} threads/core "
            f"= {virtual} virtual (HT)."
        ),
    )
