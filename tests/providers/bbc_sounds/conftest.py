"""Shared fixtures for BBC Sounds provider tests."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from music_assistant.providers.bbc_sounds import SUPPORTED_FEATURES, BBCSoundsProvider


@pytest.fixture
def provider() -> BBCSoundsProvider:
    """Create a real BBCSoundsProvider with mocked dependencies."""
    mass = Mock()
    manifest = Mock()
    manifest.domain = "bbc_sounds"
    config = Mock()
    config.instance_id = "bbc_sounds--test123"
    config.name = "BBC Sounds Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
    }.get(key, default)
    instance = BBCSoundsProvider(mass, manifest, config, SUPPORTED_FEATURES)
    instance.logged_in = True
    return instance
