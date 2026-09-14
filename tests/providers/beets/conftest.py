"""Fixtures for the beets provider tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.providers.beets.beets_db import BeetsDb


@pytest.fixture
def beets_db(tmp_path: Path) -> BeetsDb:
    """Return an empty current-schema beets database."""
    return BeetsDb(tmp_path / "library.db")


@pytest.fixture
def legacy_beets_db(tmp_path: Path) -> BeetsDb:
    """Return an empty beets database with the pre-multi-value columns."""
    return BeetsDb(tmp_path / "legacy.db", legacy=True)
