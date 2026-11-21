"""Shared fixtures for Nugs.net provider tests."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.nugs import (
    CONF_QUALITY,
    QUALITY_LOSSLESS,
    SUPPORTED_FEATURES,
    NugsProvider,
)


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
        CONF_QUALITY: QUALITY_LOSSLESS,
    }.get(key, default)
    provider = NugsProvider(mass, manifest, config, SUPPORTED_FEATURES)
    # the class-level legacy search gate would carry its one-per-second budget across tests
    provider.search_throttler = ThrottlerManager(rate_limit=1000)
    return provider
