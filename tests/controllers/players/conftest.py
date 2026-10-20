"""Shared fixtures for the players controller tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.common import MockProvider

if TYPE_CHECKING:
    from unittest.mock import MagicMock


@pytest.fixture
def provider(mock_mass: MagicMock) -> MockProvider:
    """Create a mock provider."""
    # mock_mass is defined per test module and differs between them, so a module
    # requesting this fixture must bring its own
    return MockProvider("test_provider", instance_id="test_prov", mass=mock_mass)
