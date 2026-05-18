"""Allow `python -m benchmarks` as a fallback entry point."""

from benchmarks.cli import main

raise SystemExit(main())
