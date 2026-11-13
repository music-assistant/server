"""
Tests for the player-queue artist track selection (``get_artist_tracks``).

A library-only artist (no streaming provider mappings) stands in for a file-only
library: with the bare ``mass`` fixture (no streaming/metadata providers loaded),
its top tracks resolve to nothing, so only the library can yield tracks. The
integration tests use that fixture (a real MusicAssistant with a real SQLite
database); the unit tests at the bottom exercise the selection with mocked sources.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from music_assistant_models.enums import AlbumType
from music_assistant_models.helpers import set_global_cache_values
from music_assistant_models.media_items import Album, Artist, ProviderMapping, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.player_queues.constants import (
    CONF_DEFAULT_ENQUEUE_SELECT_ARTIST,
    ArtistOrder,
)
from music_assistant.controllers.player_queues.media_resolver import MediaResolver

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


async def _add_track(
    mass: MusicAssistant, name: str, artists: list[Artist], album: Album | None = None
) -> Track:
    """Add a minimal library track credited to the given artists and return the stored item."""
    added = await mass.music.tracks.add_item_to_library(
        Track(
            item_id="0",
            provider="library",
            name=name,
            provider_mappings=_library_mapping(),
            artists=UniqueList(artists),
            album=album,
        )
    )
    return await mass.music.tracks.get_library_item(added.item_id)


async def _add_album(
    mass: MusicAssistant, name: str, artist: Artist, album_type: AlbumType = AlbumType.ALBUM
) -> Album:
    """Add a minimal library album by the given artist and return the stored item."""
    return await mass.music.albums.add_item_to_library(
        Album(
            item_id="0",
            provider="library",
            name=name,
            album_type=album_type,
            provider_mappings=_library_mapping(),
            artists=UniqueList([artist]),
        )
    )


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

    result = await mass.player_queues._media_resolver.get_artist_tracks(artist)

    assert {t.name for t in result} == {track_a.name, track_b.name}


async def test_the_default_falls_back_to_the_top_tracks(mass: MusicAssistant) -> None:
    """The default, prefer_library, plays the top tracks of an artist without library tracks."""
    artist = await _add_artist(mass, "ABBA")
    mass.music.artists.top_tracks = AsyncMock(return_value=[_track_obj("Top")])  # type: ignore[method-assign]

    result = await mass.player_queues._media_resolver.get_artist_tracks(artist)

    assert {t.name for t in result} == {"Top"}


async def test_prefer_library_returns_library_tracks(mass: MusicAssistant) -> None:
    """prefer_library returns the artist's in-library tracks when present."""
    artist = await _add_artist(mass, "ABBA")
    track = await _add_track(mass, "Waterloo", [artist])
    _select(mass, "prefer_library")

    result = await mass.player_queues._media_resolver.get_artist_tracks(artist)

    assert {t.name for t in result} == {track.name}


async def test_an_unknown_selection_plays_like_prefer_library(mass: MusicAssistant) -> None:
    """A stored selection that is not offered plays like the default, prefer_library."""
    artist = await _add_artist(mass, "ABBA")
    track = await _add_track(mass, "SOS", [artist])
    _select(mass, "all_tracks")

    result = await mass.player_queues._media_resolver.get_artist_tracks(artist)

    assert {t.name for t in result} == {track.name}


async def test_library_album_tracks_plays_the_tracks_of_the_library_albums(
    mass: MusicAssistant,
) -> None:
    """library_album_tracks plays the tracks of the artist's library albums, singles included."""
    # an album plays only its available tracks
    await set_global_cache_values({"available_providers": {"library"}})
    artist = await _add_artist(mass, "ABBA")
    waterloo = await _add_album(mass, "Waterloo", artist)
    single = await _add_album(mass, "Fernando", artist, AlbumType.SINGLE)
    await _add_track(mass, "Waterloo", [artist], waterloo)
    await _add_track(mass, "Honey Honey", [artist], waterloo)
    await _add_track(mass, "Fernando", [artist], single)
    # a saved song without an album of the artist is not part of its albums
    await _add_track(mass, "Chiquitita", [artist])
    _select(mass, "library_album_tracks")

    result = await mass.player_queues._media_resolver.get_artist_tracks(artist)

    assert sorted(t.name for t in result) == ["Fernando", "Honey Honey", "Waterloo"]


async def test_top_tracks_excludes_library_tracks(mass: MusicAssistant) -> None:
    """top_tracks returns only provider top tracks, never the in-library tracks."""
    artist = await _add_artist(mass, "ABBA")
    await _add_track(mass, "Honey Honey", [artist])
    _select(mass, "top_tracks")

    result = await mass.player_queues._media_resolver.get_artist_tracks(artist)

    assert "Honey Honey" not in {t.name for t in result}


# --- unit tests for the artist selection (mocked, no DB) ---


def _track_obj(name: str, version: str = "") -> Track:
    """Create a minimal track with the given name and version (for dedup-key tests)."""
    return Track(item_id=name, provider="test", name=name, version=version, provider_mappings=set())


def _prov_mapping(instance: str, item_id: str, in_library: bool = False) -> ProviderMapping:
    """Create a provider mapping for the given (streaming) provider instance."""
    return ProviderMapping(
        item_id=item_id, provider_domain="test", provider_instance=instance, in_library=in_library
    )


def _artist_obj(mappings: set[ProviderMapping] | None = None) -> Artist:
    """Create a minimal library artist for the resolution unit tests."""
    return Artist(item_id="1", provider="library", name="ABBA", provider_mappings=mappings or set())


def _fake_queues(selection: str) -> MagicMock:
    """Create a mock standing in for the media resolver, with the artist option preselected."""
    fake = MagicMock()
    fake.mass.config.get_raw_core_config_value = MagicMock(return_value=selection)
    fake._resolve_library_artist = AsyncMock(
        return_value=_artist_obj({_prov_mapping("p1", "a1", in_library=True)})
    )
    fake.get_artist_order = MagicMock(return_value=ArtistOrder.SHUFFLED_TRACKS)
    fake._album_order_applies = MagicMock(return_value=False)
    # the real resolution under the shuffled order
    fake.resolve_artist_tracks = partial(MediaResolver.resolve_artist_tracks, fake)
    fake._shuffled_artist_tracks = partial(MediaResolver._shuffled_artist_tracks, fake)
    return fake


async def test_prefer_library_falls_back_to_top_tracks() -> None:
    """prefer_library falls back to top tracks when the artist has no in-library tracks."""
    fake = _fake_queues("prefer_library")
    fake._library_artist_tracks = AsyncMock(return_value=[])
    fake.mass.music.artists.top_tracks = AsyncMock(return_value=[_track_obj("Top")])

    result = await MediaResolver.get_artist_tracks(cast("MediaResolver", fake), _artist_obj())

    assert {t.name for t in result} == {"Top"}


@pytest.mark.parametrize(
    "selection", ["top_tracks", "library_tracks", "prefer_library", "library_album_tracks"]
)
@pytest.mark.parametrize("stored_artist", [None, _artist_obj({_prov_mapping("p1", "a1")})])
async def test_artist_not_in_the_library_plays_its_top_tracks(
    selection: str, stored_artist: Artist | None
) -> None:
    """An artist not in the library, even when stored for a saved track, plays its top tracks."""
    fake = _fake_queues(selection)
    fake._resolve_library_artist = AsyncMock(return_value=stored_artist)
    fake.mass.music.artists.top_tracks = AsyncMock(return_value=[_track_obj("Top")])
    fake.mass.music.artists.tracks = AsyncMock(return_value=[_track_obj("All")])
    fake.mass.music.artists.albums = AsyncMock(return_value=[])
    artist = Artist(item_id="a1", provider="spotify", name="ABBA", provider_mappings=set())

    result = await MediaResolver.get_artist_tracks(cast("MediaResolver", fake), artist)

    assert {t.name for t in result} == {"Top"}
    fake.mass.music.artists.tracks.assert_not_awaited()
    fake.mass.music.artists.albums.assert_not_awaited()


async def test_library_tracks_keeps_a_library_artist_to_its_library_tracks() -> None:
    """library_tracks never falls back to other tracks for an artist that is in the library."""
    fake = _fake_queues("library_tracks")
    fake._library_artist_tracks = AsyncMock(return_value=[])
    fake.mass.music.artists.top_tracks = AsyncMock(return_value=[_track_obj("Top")])

    result = await MediaResolver.get_artist_tracks(cast("MediaResolver", fake), _artist_obj())

    assert result == []
    fake.mass.music.artists.top_tracks.assert_not_awaited()
