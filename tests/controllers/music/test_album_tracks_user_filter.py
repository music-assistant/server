"""Tests for user provider filtering on album track listings."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest
from music_assistant_models.enums import AlbumType
from music_assistant_models.media_items import (
    Album,
    Artist,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.mass import MusicAssistant

pytestmark = pytest.mark.asyncio


def _mapping(provider_instance: str, item_id: str) -> ProviderMapping:
    return ProviderMapping(
        item_id=item_id,
        provider_domain=provider_instance.removesuffix("_inst"),
        provider_instance=provider_instance,
        in_library=True,
    )


async def _seed_album_with_mixed_provider_tracks(mass: MusicAssistant) -> Album:
    """Seed a library album with one track per provider (prov_a/prov_b)."""
    artist = Artist(
        item_id="0",
        provider="library",
        name="Test Artist",
        provider_mappings={_mapping("prov_a_inst", "artist_a")},
    )
    db_artist = await mass.music.artists.add_item_to_library(artist)
    album = Album(
        item_id="0",
        provider="library",
        name="Test Album",
        album_type=AlbumType.ALBUM,
        provider_mappings={_mapping("prov_a_inst", "album_a")},
        artists=UniqueList([db_artist]),
    )
    db_album = await mass.music.albums.add_item_to_library(album)
    for idx, (name, provider_instance, item_id) in enumerate(
        [
            ("Track Local", "prov_a_inst", "track_a"),
            ("Track Streaming", "prov_b_inst", "track_b"),
        ],
        start=1,
    ):
        track = Track(
            item_id="0",
            provider="library",
            name=name,
            provider_mappings={_mapping(provider_instance, item_id)},
            artists=UniqueList([db_artist]),
            album=db_album,
            disc_number=1,
            track_number=idx,
        )
        await mass.music.tracks.add_item_to_library(track)
    return db_album


async def test_album_tracks_respect_user_provider_filter(mass: MusicAssistant) -> None:
    """A restricted user must not see album tracks that only exist on other providers."""
    db_album = await _seed_album_with_mixed_provider_tracks(mass)

    # without a user provider filter, both tracks are returned
    all_tracks = await mass.music.albums.tracks(db_album.item_id, "library")
    assert {track.name for track in all_tracks} == {"Track Local", "Track Streaming"}

    # a user restricted to prov_a must not see the prov_b-only track
    with patch(
        "music_assistant.controllers.music.media.base.get_current_user",
        return_value=Mock(provider_filter=["prov_a_inst"]),
    ):
        filtered_tracks = await mass.music.albums.tracks(db_album.item_id, "library")
        assert {track.name for track in filtered_tracks} == {"Track Local"}
        # the in_library_only variant must apply the same filter
        filtered_library_tracks = await mass.music.albums.tracks(
            db_album.item_id, "library", in_library_only=True
        )
        assert {track.name for track in filtered_library_tracks} == {"Track Local"}
