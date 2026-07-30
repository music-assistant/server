"""Shared fixtures for Soundcloud provider tests."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from music_assistant.providers.soundcloud import SUPPORTED_FEATURES, SoundcloudMusicProvider


@pytest.fixture
def provider() -> SoundcloudMusicProvider:
    """Create a real SoundcloudMusicProvider with mocked dependencies."""
    mass = Mock()
    manifest = Mock()
    manifest.domain = "soundcloud"
    config = Mock()
    config.instance_id = "soundcloud--test123"
    config.name = "Soundcloud Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
    }.get(key, default)
    return SoundcloudMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)
