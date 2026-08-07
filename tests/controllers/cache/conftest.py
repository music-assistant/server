"""Shared fixtures for the cache controller tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from music_assistant.controllers.cache import CacheController
    from music_assistant.mass import MusicAssistant


@pytest.fixture
async def cache(mass_minimal: MusicAssistant) -> CacheController:
    """Return an initialized cache controller."""
    await mass_minimal.cache._setup_database()
    return mass_minimal.cache
