"""Sudo credential keepalive.

macOS expires the `sudo` ticket after ~5 minutes by default; Linux distros vary
but the cache is bounded. A long benchmark run plus a sudo-gated telemetry
sidecar (`powermetrics`, `turbostat`) needs the ticket re-validated periodically
or the subprocess loses privileges mid-run.

The keepalive runs `sudo -n -v` (non-interactive validate) at a steady cadence.
If the validate fails (no cached creds, password required), it logs and keeps
trying — the user is responsible for `sudo -v` before kicking off the run, and
preflight checks for it.
"""

from __future__ import annotations

import logging
import subprocess
import threading

LOG = logging.getLogger(__name__)


class SudoKeepalive:
    """Refresh the sudo credential ticket on a background thread."""

    def __init__(self, interval_seconds: float = 60.0) -> None:
        self._interval = max(10.0, float(interval_seconds))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="sudo-keepalive", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                subprocess.run(
                    ["sudo", "-n", "-v"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired) as e:
                LOG.warning("sudo keepalive failed: %s", e)
            self._stop_event.wait(self._interval)
