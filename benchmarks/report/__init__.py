"""Post-processing report for committed per-machine benchmark results.

Entry point: ``alamo-benchmark report`` (wired in ``benchmarks/cli.py``).

The module is read-only: it never mutates the result DBs. It scans
``results/<machine_id>/run_*/``, picks the most-recently-started run per
machine, joins ``result``/``telemetry_sample``/``telemetry_per_core``, and
emits a single self-contained HTML report under ``--out`` (default ``report/``)
with SVG figures rendered on transparent backgrounds.
"""

from __future__ import annotations
