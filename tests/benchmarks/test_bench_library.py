"""
Macro benchmarks for library read operations.

All benchmarks share one module-scoped hermetic MusicAssistant instance with a
1000-track library, exercising the hot database query paths of the API: paged
listings, deep offsets and library search.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from music_assistant_models.enums import MediaType

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.mark.benchmark
async def test_list_tracks_page(library_mass: MusicAssistant) -> None:
    """Benchmark fetching a first page of library tracks."""
    items = await library_mass.music.tracks.library_items(limit=200)
    assert len(items) == 200


@pytest.mark.benchmark
async def test_list_tracks_deep_offset(library_mass: MusicAssistant) -> None:
    """Benchmark fetching a deep-offset page of library tracks."""
    items = await library_mass.music.tracks.library_items(limit=200, offset=800)
    assert len(items) == 200


@pytest.mark.benchmark
async def test_list_artists_page(library_mass: MusicAssistant) -> None:
    """Benchmark fetching a page of library artists."""
    items = await library_mass.music.artists.library_items(limit=50)
    assert items


@pytest.mark.benchmark
async def test_search_library(library_mass: MusicAssistant) -> None:
    """Benchmark a library search for tracks."""
    result = await library_mass.music.search_library("Test Track 1", [MediaType.TRACK], limit=25)
    assert result.tracks
