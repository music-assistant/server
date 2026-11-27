"""Shared fixtures for Live Phish provider tests."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.livephish import (
    CONF_QUALITY,
    QUALITY_LOSSLESS,
    SUPPORTED_FEATURES,
    LivePhishProvider,
)


@pytest.fixture
def provider() -> LivePhishProvider:
    """Create a real LivePhishProvider with mocked dependencies."""
    mass = Mock()
    manifest = Mock()
    manifest.domain = "livephish"
    config = Mock()
    config.instance_id = "livephish--test123"
    config.name = "Live Phish Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
        CONF_QUALITY: QUALITY_LOSSLESS,
    }.get(key, default)
    provider = LivePhishProvider(mass, manifest, config, SUPPORTED_FEATURES)
    # the class-level legacy search gate would carry its one-per-second budget across tests
    provider.search_throttler = ThrottlerManager(rate_limit=1000)
    return provider
