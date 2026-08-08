"""Shared fixtures for the webserver controller tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock Music Assistant instance."""
    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    return mass
