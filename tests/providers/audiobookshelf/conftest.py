"""Shared fixtures for Audiobookshelf provider tests."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from music_assistant.providers.audiobookshelf import SUPPORTED_FEATURES, Audiobookshelf
from music_assistant.providers.audiobookshelf.helpers import LibraryHelper
from tests.common import use_real_create_task


@pytest.fixture
def provider() -> Audiobookshelf:
    """Create a real Audiobookshelf provider with mocked dependencies."""
    mass = Mock()
    # a cold cache, so every lookup asks the server
    mass.cache = AsyncMock()
    mass.cache.get_with_freshness.return_value = (None, False, False)
    use_real_create_task(mass)
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
    provider.libraries.audiobooks["lib1"] = LibraryHelper(name="Audiobooks")
    provider._client = Mock()
    provider.library_item_lock = asyncio.Lock()
    return provider
