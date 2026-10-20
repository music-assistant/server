"""Shared fixtures for the metadata controller tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from music_assistant.controllers.metadata import MetaDataController

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


@pytest.fixture
async def cache_database(mass_minimal: MusicAssistant) -> None:
    """Set up the cache database, for tests that read or write cached metadata."""
    await mass_minimal.cache._setup_database()


@pytest.fixture
async def metadata_controller(mass_minimal: MusicAssistant) -> MetaDataController:
    """Construct a MetaDataController with the minimal MA fixture."""
    controller = MetaDataController(mass_minimal)
    mass_minimal.metadata = controller
    return controller
