"""macOS telemetry sidecar — driven by `sudo powermetrics --format plist`.

Powermetrics emits one plist per sample interval, separated on the wire by
`\\n\\x00`. We split on `</plist>` (which closes each plist), parse via
`plistlib`, and translate into the `TelemetrySample` shape. Power values arrive
in milliwatts and are converted to watts. Cluster names map to topology core
types: `S-Cluster` → super (Fusion-Architecture chips), `P*-Cluster` →
performance, `E-Cluster` → efficiency.

Per-core temperatures are not exposed by powermetrics; `temp_c` is left None.
Package thermal state is reported by powermetrics as a string
(`thermal_pressure` ∈ {Nominal, Fair, Serious, Critical}) rather than a
temperature, so `pkg_temp_c` is also None for the macOS path.
"""

from __future__ import annotations

import logging
import plistlib
import signal
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast, override

from benchmarks.storage.writer import TelemetryWriter
from benchmarks.telemetry.base import CoreSample, TelemetrySample, TelemetrySidecar
from benchmarks.telemetry.sudo import SudoKeepalive

LOG = logging.getLogger(__name__)

_PLIST_TERMINATOR = b"</plist>"

_CLUSTER_TYPE_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("S-", "super"),
    ("P", "performance"),
    ("E", "efficiency"),
)


def _cluster_core_type(name: str) -> str:
    for prefix, core_type in _CLUSTER_TYPE_BY_PREFIX:
        if name.startswith(prefix):
            return core_type
    return "unknown"


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _mw_to_w(value: Any) -> float | None:
    f = _float_or_none(value)
    return None if f is None else f / 1000.0


def _parse_cpu(cpu_any: Any, core_type: str) -> CoreSample | None:
    """Translate one entry from `cluster.cpus` into a CoreSample. None for junk."""
    if not isinstance(cpu_any, dict):
        return None
    cpu = cast("dict[str, Any]", cpu_any)
    idx = cpu.get("cpu")
    if not isinstance(idx, int):
        return None
    down_ratio = _float_or_none(cpu.get("down_ratio")) or 0.0
    if down_ratio >= 1.0:
        return CoreSample(idx, core_type, freq_mhz=0.0, util_pct=0.0, temp_c=None)
    idle_ratio = _float_or_none(cpu.get("idle_ratio")) or 0.0
    freq_hz = _float_or_none(cpu.get("freq_hz"))
    freq_mhz = None if freq_hz is None else freq_hz / 1e6
    util_pct = max(0.0, min(100.0, (1.0 - idle_ratio) * 100.0))
    return CoreSample(idx, core_type, freq_mhz=freq_mhz, util_pct=util_pct, temp_c=None)


def _ts_from_doc(doc: dict[str, Any]) -> str:
    ts = doc.get("timestamp")
    if isinstance(ts, datetime):
        return ts.astimezone(UTC).isoformat(timespec="microseconds")
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _parse_plist(blob: bytes) -> TelemetrySample | None:
    """Parse one complete plist into a TelemetrySample. None on malformed input."""
    blob = blob.lstrip(b"\x00\n\r ")
    if not blob:
        return None
    try:
        doc: dict[str, Any] = plistlib.loads(blob)
    except Exception as e:
        # plistlib + expat raise varied types; never let one bad sample kill the sidecar.
        LOG.warning("powermetrics: skipping malformed plist (%s)", e)
        return None

    processor = cast("dict[str, Any]", doc.get("processor", {}))
    per_core: list[CoreSample] = []
    cluster_freqs_mhz: list[float] = []
    cluster_utils: list[float] = []
    clusters_any: Any = processor.get("clusters", [])
    clusters: list[Any] = cast("list[Any]", clusters_any) if isinstance(clusters_any, list) else []
    for cluster_any in clusters:
        if not isinstance(cluster_any, dict):
            continue
        cluster = cast("dict[str, Any]", cluster_any)
        core_type = _cluster_core_type(str(cluster.get("name", "")))
        cluster_freq_hz = _float_or_none(cluster.get("freq_hz"))
        if cluster_freq_hz:
            cluster_freqs_mhz.append(cluster_freq_hz / 1e6)
        cpus_any: Any = cluster.get("cpus", [])
        cpus: list[Any] = cast("list[Any]", cpus_any) if isinstance(cpus_any, list) else []
        for cpu in cpus:
            sample = _parse_cpu(cpu, core_type)
            if sample is None:
                continue
            per_core.append(sample)
            if sample.util_pct is not None:
                cluster_utils.append(sample.util_pct)

    freq_avg_mhz = sum(cluster_freqs_mhz) / len(cluster_freqs_mhz) if cluster_freqs_mhz else None
    freq_max_mhz = max(cluster_freqs_mhz) if cluster_freqs_mhz else None
    cpu_util_pct = sum(cluster_utils) / len(cluster_utils) if cluster_utils else None

    return TelemetrySample(
        ts=_ts_from_doc(doc),
        cpu_freq_avg_mhz=freq_avg_mhz,
        cpu_freq_max_mhz=freq_max_mhz,
        cpu_util_pct=cpu_util_pct,
        package_power_w=_mw_to_w(processor.get("combined_power")),
        pkg_temp_c=None,
        per_core=tuple(per_core),
    )


def parse_powermetrics_stream(data: bytes) -> list[TelemetrySample]:
    """Parse a complete byte stream of one or more concatenated plists.

    Tolerates the `\\n\\x00` separator powermetrics emits between samples. Used
    by tests and by the sidecar's final-drain on shutdown.
    """
    samples: list[TelemetrySample] = []
    parser = PowermetricsStreamParser()
    samples.extend(parser.feed(data))
    samples.extend(parser.flush())
    return samples


class PowermetricsStreamParser:
    """Incremental parser. Feed chunks of bytes; pulls out complete plists."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[TelemetrySample]:
        self._buf.extend(chunk)
        out: list[TelemetrySample] = []
        while True:
            idx = self._buf.find(_PLIST_TERMINATOR)
            if idx < 0:
                break
            end = idx + len(_PLIST_TERMINATOR)
            one = bytes(self._buf[:end])
            del self._buf[:end]
            sample = _parse_plist(one)
            if sample is not None:
                out.append(sample)
        return out

    def flush(self) -> list[TelemetrySample]:
        """Parse any trailing plist that wasn't terminator-followed (rare)."""
        if not self._buf:
            return []
        leftover = bytes(self._buf)
        self._buf.clear()
        sample = _parse_plist(leftover)
        return [sample] if sample is not None else []


# --------------------------------------------------------- subprocess sidecar


_DEFAULT_SAMPLERS = "cpu_power,gpu_power,thermal"


class MacosSidecar(TelemetrySidecar):
    """Run `sudo powermetrics` as a subprocess and stream samples into SQLite."""

    def __init__(
        self,
        *,
        db_path: Path,
        sample_interval_seconds: float,
        samplers: str = _DEFAULT_SAMPLERS,
    ) -> None:
        self._db_path = db_path
        self._interval_ms = max(100, int(sample_interval_seconds * 1000))
        self._samplers = samplers
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._sudo: SudoKeepalive | None = None
        self._run_id: str | None = None

    @override
    def start(self, run_id: str) -> None:
        if self._proc is not None:
            return
        self._run_id = run_id
        self._stop_event.clear()
        cmd = [
            "sudo",
            "-n",
            "powermetrics",
            "--format",
            "plist",
            "--samplers",
            self._samplers,
            "-i",
            str(self._interval_ms),
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except (FileNotFoundError, PermissionError) as e:
            LOG.warning("powermetrics unavailable, telemetry disabled: %s", e)
            self._proc = None
            return

        self._sudo = SudoKeepalive()
        self._sudo.start()
        self._reader = threading.Thread(
            target=self._reader_loop, name="powermetrics-reader", daemon=True
        )
        self._reader.start()
        LOG.info("macOS telemetry started (powermetrics, %d ms interval)", self._interval_ms)

    @override
    def stop(self) -> None:
        self._stop_event.set()
        proc = self._proc
        if proc is not None:
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                LOG.warning("powermetrics did not exit on SIGTERM; killing")
                proc.kill()
                proc.wait(timeout=2)
            except OSError as e:
                LOG.warning("powermetrics shutdown error: %s", e)
        reader = self._reader
        if reader is not None:
            reader.join(timeout=5)
        if self._sudo is not None:
            self._sudo.stop()
        self._proc = None
        self._reader = None
        self._sudo = None
        LOG.info("macOS telemetry stopped")

    def _reader_loop(self) -> None:
        proc = self._proc
        run_id = self._run_id
        if proc is None or proc.stdout is None or run_id is None:
            return
        parser = PowermetricsStreamParser()
        try:
            writer = TelemetryWriter(self._db_path)
        except OSError as e:
            LOG.warning("could not open telemetry writer: %s", e)
            return
        try:
            while not self._stop_event.is_set():
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                for sample in parser.feed(chunk):
                    _write_sample(writer, run_id, sample)
            for sample in parser.flush():
                _write_sample(writer, run_id, sample)
        except OSError as e:
            LOG.warning("powermetrics reader error: %s", e)
        finally:
            writer.close()


def _write_sample(writer: TelemetryWriter, run_id: str, sample: TelemetrySample) -> None:
    """Persist one sample + its per-core rows. Telemetry failures never raise."""
    try:
        writer.write_sample(
            run_id=run_id,
            ts=sample.ts,
            cpu_freq_avg_mhz=sample.cpu_freq_avg_mhz,
            cpu_freq_max_mhz=sample.cpu_freq_max_mhz,
            cpu_util_pct=sample.cpu_util_pct,
            package_power_w=sample.package_power_w,
            pkg_temp_c=sample.pkg_temp_c,
            mem_used_gb=sample.mem_used_gb,
            swap_used_gb=sample.swap_used_gb,
            load1=sample.load1,
            load5=sample.load5,
            load15=sample.load15,
        )
        for core in sample.per_core:
            writer.write_per_core(
                run_id=run_id,
                ts=sample.ts,
                core_index=core.core_index,
                core_type=core.core_type,
                freq_mhz=core.freq_mhz,
                util_pct=core.util_pct,
                temp_c=core.temp_c,
            )
    except Exception as e:
        # Telemetry must never kill a rep; swallow and log.
        LOG.warning("telemetry write failed: %s", e)
