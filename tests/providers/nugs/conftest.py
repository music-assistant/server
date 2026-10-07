"""Shared fixtures for Nugs.net provider tests."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from music_assistant.providers.nugs import SUPPORTED_FEATURES, NugsProvider


@pytest.fixture
def provider() -> NugsProvider:
    """Create a real NugsProvider with mocked dependencies."""
    mass = Mock()
    manifest = Mock()
    manifest.domain = "nugs"
    config = Mock()
    config.instance_id = "nugs--test123"
    config.name = "Nugs Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
    }.get(key, default)
    return NugsProvider(mass, manifest, config, SUPPORTED_FEATURES)
