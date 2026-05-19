"""Tests for the machine_id resolver.

We test the helper directly (with env/file isolation via pytest fixtures)
rather than the full `collect()` path, because `collect()` hits sysctl, psutil,
etc. that aren't relevant here."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from benchmarks import platform_info as pinfo


@pytest.fixture(autouse=True)
def _isolate_machine_id_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each test starts with no env var and a tmp-path file that doesn't exist."""
    monkeypatch.delenv(pinfo._MACHINE_ID_ENV, raising=False)
    monkeypatch.setattr(pinfo, "_MACHINE_ID_FILE", tmp_path / "machine_id")


def test_unset_returns_none_with_source_unset() -> None:
    assert pinfo._machine_id() == (None, "unset")


def test_env_var_takes_priority(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(pinfo._MACHINE_ID_ENV, "from-env")
    (tmp_path / "machine_id").write_text("from-file\n")
    assert pinfo._machine_id() == ("from-env", "env")


def test_file_used_when_env_absent(tmp_path: Path) -> None:
    (tmp_path / "machine_id").write_text("iastate-m1pro-01\n")
    assert pinfo._machine_id() == ("iastate-m1pro-01", "file")


def test_file_whitespace_stripped(tmp_path: Path) -> None:
    (tmp_path / "machine_id").write_text("  trimmed  \n\n")
    assert pinfo._machine_id() == ("trimmed", "file")


def test_invalid_chars_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(pinfo._MACHINE_ID_ENV, "has spaces")
    mid, source = pinfo._machine_id()
    assert mid is None
    assert source == "invalid:env_bad_chars"


def test_slash_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """No path separators — machine_id becomes a directory NAME, not a sub-path."""
    monkeypatch.setenv(pinfo._MACHINE_ID_ENV, "foo/bar")
    mid, source = pinfo._machine_id()
    assert mid is None
    assert source == "invalid:env_bad_chars"


def test_empty_env_var_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(pinfo._MACHINE_ID_ENV, "")
    assert pinfo._machine_id() == (None, "invalid:env_empty")


def test_too_long_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(pinfo._MACHINE_ID_ENV, "x" * (pinfo._MACHINE_ID_MAX_LEN + 1))
    mid, source = pinfo._machine_id()
    assert mid is None
    assert source == "invalid:env_too_long"


def test_at_max_length_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    max_id = "x" * pinfo._MACHINE_ID_MAX_LEN
    monkeypatch.setenv(pinfo._MACHINE_ID_ENV, max_id)
    assert pinfo._machine_id() == (max_id, "env")


def test_allowed_punctuation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The allowed set [A-Za-z0-9._-] should support reasonable lab naming."""
    for ok in ("m1pro", "iastate-m1-01", "lab.box-02", "M5_Max"):
        monkeypatch.setenv(pinfo._MACHINE_ID_ENV, ok)
        mid, source = pinfo._machine_id()
        assert mid == ok, f"expected {ok!r}, got {mid!r}"
        assert source == "env"


def test_empty_file_falls_through_to_unset(tmp_path: Path) -> None:
    (tmp_path / "machine_id").write_text("\n")
    assert pinfo._machine_id() == (None, "unset")


def test_file_read_error_surfaces_in_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "machine_id").write_text("doesnt-matter")
    # Patch is_file to True, but make read_text raise — simulates a permissions
    # issue or a race where the file is rm'd between checks.
    with patch.object(Path, "read_text", side_effect=OSError("simulated")):
        mid, source = pinfo._machine_id()
    assert mid is None
    assert source.startswith("invalid:file_read_error")
