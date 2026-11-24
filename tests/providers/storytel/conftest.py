"""Shared fixtures for Storytel provider tests."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from music_assistant.providers.storytel import Storytel


@pytest.fixture
def provider() -> Storytel:
    """Create a real Storytel provider with mocked dependencies and no persisted setup_data."""
    mass = Mock()
    mass.config.get.return_value = {}
    manifest = Mock()
    manifest.domain = "storytel"
    config = Mock()
    config.instance_id = "storytel--test"
    config.name = "Storytel Test"
    config.enabled = True
    config.values = {}
    config.get_value.side_effect = lambda _key, default=None: default
    return Storytel(mass, manifest, config)
