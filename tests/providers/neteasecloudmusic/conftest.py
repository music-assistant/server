"""Shared fixtures for NetEase Cloud Music provider tests."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from music_assistant.providers.neteasecloudmusic import (
    SUPPORTED_FEATURES,
    NeteaseCloudMusicProvider,
)


@pytest.fixture
def provider() -> NeteaseCloudMusicProvider:
    """Create a real NeteaseCloudMusicProvider with mocked dependencies."""
    mass = Mock()
    manifest = Mock()
    manifest.domain = "neteasecloudmusic"
    config = Mock()
    config.instance_id = "ncm--test123"
    config.name = "NetEase Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
    }.get(key, default)
    prov = NeteaseCloudMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)
    # normally set in handle_async_init
    prov._cookie = "MUSIC_U=test"
    prov._uid = "42"
    return prov
