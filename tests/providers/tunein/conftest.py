"""Shared fixtures for TuneIn provider tests."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from music_assistant.providers.tunein import SUPPORTED_FEATURES, TuneInProvider


@pytest.fixture
def provider() -> TuneInProvider:
    """Create a real TuneInProvider with mocked dependencies."""
    mass = Mock()
    manifest = Mock()
    manifest.domain = "tunein"
    config = Mock()
    config.instance_id = "tunein--test123"
    config.name = "TuneIn Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
    }.get(key, default)
    return TuneInProvider(mass, manifest, config, SUPPORTED_FEATURES)
