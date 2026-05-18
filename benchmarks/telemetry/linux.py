"""Linux telemetry sidecar — driven by `sudo turbostat --interval N --quiet`.

Turbostat emits tab-separated tables; each iteration begins with a header line
starting with `Core\\tCPU\\t...` followed by a summary row (`Core=- CPU=-`) and
one row per logical CPU. The column set varies by kernel/family (the W-1370
fixture has graphics + system power columns the W5-2545 lacks), so the parser
keys everything by column name — never by position (CLAUDE.md "Telemetry parser
robustness").
"""

from __future__ import annotations

import logging
import signal
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import override

from benchmarks.storage.writer import TelemetryWriter
from benchmarks.telemetry.base import CoreSample, TelemetrySample, TelemetrySidecar
from benchmarks.telemetry.sudo import SudoKeepalive

LOG = logging.getLogger(__name__)

_HEADER_PREFIX = "Core\tCPU\t"


_EMPTY_FIELDS = {"", "-"}


def _parse_float(value: str | None) -> float | None:
    if value is None or value in _EMPTY_FIELDS:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_int(value: str | None) -> int | None:
    if value is None or value in _EMPTY_FIELDS:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _build_sample(
    columns: list[str], rows: list[dict[str, str]], ts: str
) -> TelemetrySample | None:
    """Translate one iteration's worth of rows into a TelemetrySample."""
    if not rows:
        return None
    summary: dict[str, str] | None = None
    per_cpu_rows: list[dict[str, str]] = []
    for row in rows:
        if row.get("Core") == "-" and row.get("CPU") == "-":
            summary = row
        else:
            per_cpu_rows.append(row)
    if summary is None and per_cpu_rows:
        summary = per_cpu_rows[0]

    per_core: list[CoreSample] = []
    seen_cores: set[str] = set()
    for row in per_cpu_rows:
        cpu_idx = _parse_int(row.get("CPU"))
        if cpu_idx is None:
            continue
        core_id = row.get("Core", "")
        core_type = "virtual" if core_id in seen_cores else "physical"
        seen_cores.add(core_id)
        per_core.append(
            CoreSample(
                core_index=cpu_idx,
                core_type=core_type,
                freq_mhz=_parse_float(row.get("Bzy_MHz")) or _parse_float(row.get("Avg_MHz")),
                util_pct=_parse_float(row.get("Busy%")),
                temp_c=_parse_float(row.get("CoreTmp")),
            )
        )

    if summary is not None:
        cpu_freq_avg = _parse_float(summary.get("Avg_MHz"))
        cpu_freq_max = _parse_float(summary.get("Bzy_MHz"))
        cpu_util = _parse_float(summary.get("Busy%"))
        package_power = _parse_float(summary.get("PkgWatt"))
        pkg_temp = _parse_float(summary.get("PkgTmp"))
    else:
        cpu_freq_avg = cpu_freq_max = cpu_util = package_power = pkg_temp = None

    del columns  # column metadata is consumed via the row dicts
    return TelemetrySample(
        ts=ts,
        cpu_freq_avg_mhz=cpu_freq_avg,
        cpu_freq_max_mhz=cpu_freq_max,
        cpu_util_pct=cpu_util,
        package_power_w=package_power,
        pkg_temp_c=pkg_temp,
        per_core=tuple(per_core),
    )


class TurbostatStreamParser:
    """Incremental, line-fed parser. An iteration emits when the next header
    line arrives (or on `flush()`)."""

    def __init__(self) -> None:
        self._columns: list[str] | None = None
        self._rows: list[dict[str, str]] = []
        self._buf = ""

    def feed(self, text: str) -> list[TelemetrySample]:
        out: list[TelemetrySample] = []
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            sample = self._feed_line(line)
            if sample is not None:
                out.append(sample)
        return out

    def flush(self) -> list[TelemetrySample]:
        out: list[TelemetrySample] = []
        if self._buf:
            sample = self._feed_line(self._buf)
            self._buf = ""
            if sample is not None:
                out.append(sample)
        if self._columns is not None and self._rows:
            sample = _build_sample(self._columns, self._rows, _utc_now_iso())
            self._rows = []
            if sample is not None:
                out.append(sample)
        return out

    def _feed_line(self, raw: str) -> TelemetrySample | None:
        line = raw.rstrip("\r")
        if not line:
            return None
        if line.startswith(_HEADER_PREFIX):
            sample: TelemetrySample | None = None
            if self._columns is not None and self._rows:
                sample = _build_sample(self._columns, self._rows, _utc_now_iso())
            self._columns = line.split("\t")
            self._rows = []
            return sample
        if self._columns is None:
            return None
        fields = line.split("\t")
        n = min(len(fields), len(self._columns))
        row = {self._columns[i]: fields[i] for i in range(n)}
        self._rows.append(row)
        return None


def parse_turbostat_stream(text: str) -> list[TelemetrySample]:
    """Parse a complete turbostat text stream. Used by tests."""
    parser = TurbostatStreamParser()
    out = parser.feed(text)
    out.extend(parser.flush())
    return out


# --------------------------------------------------------- subprocess sidecar


class LinuxSidecar(TelemetrySidecar):
    """Run `sudo turbostat` as a subprocess and stream samples into SQLite."""

    def __init__(self, *, db_path: Path, sample_interval_seconds: float) -> None:
        self._db_path = db_path
        self._interval_s = max(1, round(sample_interval_seconds))
        self._proc: subprocess.Popen[str] | None = None
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
            "turbostat",
            "--interval",
            str(self._interval_s),
            "--quiet",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,
                text=True,
            )
        except (FileNotFoundError, PermissionError) as e:
            LOG.warning("turbostat unavailable, telemetry disabled: %s", e)
            self._proc = None
            return

        self._sudo = SudoKeepalive()
        self._sudo.start()
        self._reader = threading.Thread(
            target=self._reader_loop, name="turbostat-reader", daemon=True
        )
        self._reader.start()
        LOG.info("Linux telemetry started (turbostat, %d s interval)", self._interval_s)

    @override
    def stop(self) -> None:
        self._stop_event.set()
        proc = self._proc
        if proc is not None:
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                LOG.warning("turbostat did not exit on SIGTERM; killing")
                proc.kill()
                proc.wait(timeout=2)
            except OSError as e:
                LOG.warning("turbostat shutdown error: %s", e)
        reader = self._reader
        if reader is not None:
            reader.join(timeout=5)
        if self._sudo is not None:
            self._sudo.stop()
        self._proc = None
        self._reader = None
        self._sudo = None
        LOG.info("Linux telemetry stopped")

    def _reader_loop(self) -> None:
        proc = self._proc
        run_id = self._run_id
        if proc is None or proc.stdout is None or run_id is None:
            return
        parser = TurbostatStreamParser()
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
            LOG.warning("turbostat reader error: %s", e)
        finally:
            writer.close()


def _write_sample(writer: TelemetryWriter, run_id: str, sample: TelemetrySample) -> None:
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
