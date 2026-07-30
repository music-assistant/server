"""Shared fixtures for Open Subsonic provider tests."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from music_assistant.providers.opensubsonic import SUPPORTED_FEATURES
from music_assistant.providers.opensubsonic.sonic_provider import OpenSonicProvider


@pytest.fixture
def provider() -> OpenSonicProvider:
    """Create a real OpenSonicProvider with mocked dependencies."""
    mass = Mock()
    manifest = Mock()
    manifest.domain = "opensubsonic"
    config = Mock()
    config.instance_id = "opensubsonic--test123"
    config.name = "Open Subsonic Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
    }.get(key, default)
    return OpenSonicProvider(mass, manifest, config, SUPPORTED_FEATURES)
