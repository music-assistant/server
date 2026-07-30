"""Shared fixtures for MusicMe provider tests."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from music_assistant.providers.musicme.constants import SUPPORTED_FEATURES
from music_assistant.providers.musicme.provider import MusicMeProvider


@pytest.fixture
def provider() -> MusicMeProvider:
    """Create a real MusicMeProvider with mocked dependencies."""
    mass = Mock()
    manifest = Mock()
    manifest.domain = "musicme"
    config = Mock()
    config.instance_id = "musicme--test123"
    config.name = "MusicMe Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
    }.get(key, default)
    return MusicMeProvider(mass, manifest, config, SUPPORTED_FEATURES)
