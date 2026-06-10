"""
Tests for the player-queue artist track selection (``get_artist_tracks``).

A library-only artist (no streaming provider mappings) stands in for a file-only
library: its top-tracks and per-provider discography legs resolve to nothing, so
only the in-library leg can yield tracks. These tests use the ``mass`` fixture
from ``tests/conftest.py`` (a real MusicAssistant with a real SQLite database).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from music_assistant_models.media_items import Artist, ProviderMapping, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.player_queues import CONF_DEFAULT_ENQUEUE_SELECT_ARTIST

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def _library_mapping() -> set[ProviderMapping]:
    """Create a single library provider mapping with a unique provider item id."""
    return {
        ProviderMapping(
            item_id=uuid4().hex,
            provider_domain="library",
            provider_instance="library",
            in_library=True,
        )
    }


async def _add_artist(mass: MusicAssistant, name: str) -> Artist:
    """Add a minimal library artist and return the stored item."""
    return await mass.music.artists.add_item_to_library(
        Artist(item_id="0", provider="library", name=name, provider_mappings=_library_mapping())
    )


async def _add_track(mass: MusicAssistant, name: str, artists: list[Artist]) -> Track:
    """Add a minimal library track credited to the given artists and return the stored item."""
    added = await mass.music.tracks.add_item_to_library(
        Track(
            item_id="0",
            provider="library",
            name=name,
            provider_mappings=_library_mapping(),
            artists=UniqueList(artists),
        )
    )
    return await mass.music.tracks.get_library_item(added.item_id)


def _select(mass: MusicAssistant, value: str) -> None:
    """Set the artist enqueue-selection option."""
    mass.config.set_raw_core_config_value(
        "player_queues", CONF_DEFAULT_ENQUEUE_SELECT_ARTIST, value
    )


async def test_default_resolves_library_only_artist(mass: MusicAssistant) -> None:
    """The default option resolves a library-only artist to its in-library tracks."""
    artist = await _add_artist(mass, "ABBA")
    track_a = await _add_track(mass, "Dancing Queen", [artist])
    track_b = await _add_track(mass, "Mamma Mia", [artist])

    result = await mass.player_queues.get_artist_tracks(artist)

    assert {t.name for t in result} == {track_a.name, track_b.name}


async def test_prefer_library_returns_library_tracks(mass: MusicAssistant) -> None:
    """prefer_library returns the artist's in-library tracks when present."""
    artist = await _add_artist(mass, "ABBA")
    track = await _add_track(mass, "Waterloo", [artist])
    _select(mass, "prefer_library")

    result = await mass.player_queues.get_artist_tracks(artist)

    assert {t.name for t in result} == {track.name}


async def test_legacy_all_album_tracks_maps_to_all_tracks(mass: MusicAssistant) -> None:
    """A stored legacy 'all_album_tracks' resolves like 'all_tracks' (no empty result)."""
    artist = await _add_artist(mass, "ABBA")
    track = await _add_track(mass, "SOS", [artist])
    _select(mass, "all_album_tracks")

    result = await mass.player_queues.get_artist_tracks(artist)

    assert {t.name for t in result} == {track.name}


async def test_legacy_library_album_tracks_maps_to_library_tracks(mass: MusicAssistant) -> None:
    """A stored legacy 'library_album_tracks' resolves like 'library_tracks'."""
    artist = await _add_artist(mass, "ABBA")
    track = await _add_track(mass, "Fernando", [artist])
    _select(mass, "library_album_tracks")

    result = await mass.player_queues.get_artist_tracks(artist)

    assert {t.name for t in result} == {track.name}


async def test_top_tracks_excludes_library_tracks(mass: MusicAssistant) -> None:
    """top_tracks returns only provider top tracks, never the in-library tracks."""
    artist = await _add_artist(mass, "ABBA")
    await _add_track(mass, "Honey Honey", [artist])
    _select(mass, "top_tracks")

    result = await mass.player_queues.get_artist_tracks(artist)

    assert "Honey Honey" not in {t.name for t in result}
