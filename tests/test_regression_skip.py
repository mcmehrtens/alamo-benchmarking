"""Tests for the regression-suite skip-patcher.

The patcher mutates Alamo's `tests/<dir>/input` files in place to inject
`#@ skip=true` into specific sections so runtests.py marks them as skipped
instead of failed. We verify:
  - section header matching
  - idempotency (re-applying doesn't double-insert)
  - missing-file / missing-section reporting
  - leaving other sections in the same file untouched."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path

from benchmarks.runners import regression

_SAMPLE_INPUT = """#@
#@ [2d]
#@ dim=2
#@ check=true
#@ exe=thermoelastic
#@ args=stop_time=10
#@
#@ [2d-coverage]
#@ dim=2
#@ check=false
#@ exe=thermoelastic
#@ args=stop_time=1
#@ coverage=true
#@

alamo.program = thermoelastic
plot_file = tests/ThermoElastic/output
"""


def _write_alamo_input(alamo_dir: Path, test_dir: str, content: str) -> Path:
    target = alamo_dir / "tests" / test_dir / "input"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return target


def test_inserts_skip_into_named_section(tmp_path: Path) -> None:
    path = _write_alamo_input(tmp_path, "ThermoElastic", _SAMPLE_INPUT)
    applied, missing = regression._apply_skip_patches(tmp_path, ("ThermoElastic.2d",))
    assert applied == ["ThermoElastic.2d"]
    assert missing == []
    after = path.read_text().splitlines()
    # `#@ skip=true` should sit right after the `#@ [2d]` header.
    idx_2d = next(i for i, line in enumerate(after) if line.strip() == "#@ [2d]")
    assert after[idx_2d + 1].strip() == "#@ skip=true"
    # The other section must remain unchanged.
    idx_cov = next(i for i, line in enumerate(after) if line.strip() == "#@ [2d-coverage]")
    assert after[idx_cov + 1].strip() == "#@ dim=2"


def test_is_idempotent(tmp_path: Path) -> None:
    _write_alamo_input(tmp_path, "ThermoElastic", _SAMPLE_INPUT)
    regression._apply_skip_patches(tmp_path, ("ThermoElastic.2d",))
    second = (tmp_path / "tests" / "ThermoElastic" / "input").read_text()
    regression._apply_skip_patches(tmp_path, ("ThermoElastic.2d",))
    third = (tmp_path / "tests" / "ThermoElastic" / "input").read_text()
    assert second == third, "second skip patch should not append another skip line"


def test_missing_test_dir_reported(tmp_path: Path) -> None:
    applied, missing = regression._apply_skip_patches(tmp_path, ("Nonexistent.foo",))
    assert applied == []
    assert missing == ["Nonexistent.foo"]


def test_missing_section_in_existing_dir_reported(tmp_path: Path) -> None:
    _write_alamo_input(tmp_path, "ThermoElastic", _SAMPLE_INPUT)
    applied, missing = regression._apply_skip_patches(
        tmp_path, ("ThermoElastic.does-not-exist",)
    )
    assert applied == []
    assert missing == ["ThermoElastic.does-not-exist"]


def test_entry_without_dot_reported_as_missing(tmp_path: Path) -> None:
    applied, missing = regression._apply_skip_patches(tmp_path, ("malformed-no-dot",))
    assert applied == []
    assert missing == ["malformed-no-dot"]


def test_multiple_sections_in_same_dir(tmp_path: Path) -> None:
    _write_alamo_input(tmp_path, "ThermoElastic", _SAMPLE_INPUT)
    applied, missing = regression._apply_skip_patches(
        tmp_path, ("ThermoElastic.2d", "ThermoElastic.2d-coverage")
    )
    assert set(applied) == {"ThermoElastic.2d", "ThermoElastic.2d-coverage"}
    assert missing == []
    after = (tmp_path / "tests" / "ThermoElastic" / "input").read_text().splitlines()
    headers = [i for i, line in enumerate(after) if line.startswith("#@ [")]
    for h in headers:
        assert after[h + 1].strip() == "#@ skip=true"


def test_multiple_dirs(tmp_path: Path) -> None:
    _write_alamo_input(tmp_path, "ThermoElastic", _SAMPLE_INPUT)
    _write_alamo_input(
        tmp_path,
        "Voronoi",
        "#@\n#@ [2D-100grain-serial]\n#@ dim=2\n#@ check=true\n#@\n",
    )
    applied, missing = regression._apply_skip_patches(
        tmp_path, ("ThermoElastic.2d", "Voronoi.2D-100grain-serial")
    )
    assert set(applied) == {"ThermoElastic.2d", "Voronoi.2D-100grain-serial"}
    assert missing == []


def test_empty_skip_list_no_op(tmp_path: Path) -> None:
    path = _write_alamo_input(tmp_path, "ThermoElastic", _SAMPLE_INPUT)
    original = path.read_text()
    applied, missing = regression._apply_skip_patches(tmp_path, ())
    assert applied == []
    assert missing == []
    assert path.read_text() == original


def test_skip_true_with_yes_or_1_also_counts_as_already_skipped(tmp_path: Path) -> None:
    """runtests.py accepts skip=true|yes|1; the patcher must recognise all
    three as 'already skipped' to stay idempotent."""
    for skip_value in ("true", "yes", "1", "True", "YES"):
        content = (
            "#@\n#@ [2d]\n#@ skip="
            + skip_value
            + "\n#@ dim=2\n#@\n"
        )
        path = _write_alamo_input(tmp_path / skip_value, "ThermoElastic", content)
        applied, _ = regression._apply_skip_patches(
            tmp_path / skip_value, ("ThermoElastic.2d",)
        )
        assert applied == ["ThermoElastic.2d"]
        # Original `skip=<value>` left as-is; we don't append a duplicate.
        assert path.read_text().count("skip=") == 1, f"failed for skip={skip_value}"
