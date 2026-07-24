"""Shared fixtures for Audiobookshelf provider tests."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from music_assistant.providers.audiobookshelf import SUPPORTED_FEATURES, Audiobookshelf
from music_assistant.providers.audiobookshelf.helpers import LibrariesHelper, LibraryHelper


@pytest.fixture
def provider() -> Audiobookshelf:
    """Create a real Audiobookshelf provider with mocked dependencies."""
    mass = Mock()
    manifest = Mock()
    manifest.domain = "audiobookshelf"
    config = Mock()
    config.instance_id = "audiobookshelf--test123"
    config.name = "Audiobookshelf Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
    }.get(key, default)
    provider = Audiobookshelf(mass, manifest, config, SUPPORTED_FEATURES)
    # attributes normally set in handle_async_init
    provider.libraries = LibrariesHelper()
    provider.libraries.audiobooks["lib1"] = LibraryHelper(name="Audiobooks")
    provider._client = Mock()
    return provider
