"""Tests for repository-independent provider test setup."""

from pathlib import Path

import pytest


@pytest.mark.parametrize("filename", ["__init__.py", "conftest.py"])
def test_synced_test_setup_has_no_external_repository_paths(filename: str) -> None:
    """Keep test setup independent of the workflow checkout layout."""
    source = Path(__file__).with_name(filename).read_text(encoding="utf-8")

    assert "ma-server" not in source
    assert "Path.cwd()" not in source
