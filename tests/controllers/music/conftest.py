"""Shared fixtures for the music controller tests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from music_assistant.controllers.streams.audio_analysis import AudioAnalysisController

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant.mass import MusicAssistant


@pytest.fixture
async def music_mass_with_cache(music_mass: MusicAssistant) -> MusicAssistant:
    """Return a library-only instance with a real cache, for tests that list a container."""
    await music_mass.cache._setup_database()
    return music_mass


@pytest.fixture
async def music_mass_with_audio_analysis(
    music_mass: MusicAssistant,
) -> AsyncGenerator[MusicAssistant]:
    """
    Return a library-only instance with a real audio analysis database and cache.

    For tests whose library removals reach the stored audio analysis.
    """
    music_mass.streams = MagicMock()
    music_mass.streams.mass = music_mass
    music_mass.streams.audio_analysis = AudioAnalysisController(music_mass.streams)
    await music_mass.streams.audio_analysis.setup_database()
    await music_mass.cache._setup_database()
    try:
        yield music_mass
    finally:
        await music_mass.streams.audio_analysis.close()
