"""
Tests for album track ordering with mixed disc numbers.

Some providers (e.g. YT Music) don't expose disc info and hardcode
disc_number=0 for all album tracks. Mixed with disc 1 tracks from another
provider, a naive (disc_number, track_number) sort puts those disc-0
tracks before disc 1.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest
from music_assistant_models.enums import AlbumType
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.controllers.music import MusicController
from music_assistant.mass import MusicAssistant

FILESYSTEM_INSTANCE = "filesystem_local_1"
YTMUSIC_INSTANCE = "ytmusic_1"


@pytest.fixture
async def music(mass_minimal: MusicAssistant) -> AsyncGenerator[MusicController]:
    """Return a music controller with a real library database."""
    controller = MusicController(mass_minimal)
    mass_minimal.music = controller
    await controller._setup_database()
    yield controller
    if controller._database:
        await controller._database.close()


def _provider_mapping(provider_instance: str, item_id: str) -> ProviderMapping:
    """Create a provider mapping for the given provider instance and item id."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain=provider_instance.split("_", maxsplit=1)[0],
        provider_instance=provider_instance,
        audio_format=AudioFormat(),
    )


def _create_artist(provider_instance: str) -> Artist:
    """Create an Artist as it would be received from a music provider."""
    return Artist(
        item_id="artist_1",
        provider=provider_instance,
        name="Test Artist",
        provider_mappings={_provider_mapping(provider_instance, "artist_1")},
    )


def _create_track(
    provider_instance: str,
    item_id: str,
    name: str,
    disc_number: int,
    track_number: int,
    album: Album | None = None,
) -> Track:
    """Create a Track as it would be received from a music provider."""
    return Track(
        item_id=item_id,
        provider=provider_instance,
        name=name,
        provider_mappings={_provider_mapping(provider_instance, item_id)},
        artists=UniqueList([_create_artist(provider_instance)]),
        album=album,
        disc_number=disc_number,
        track_number=track_number,
    )


async def _add_library_album(
    music: MusicController, provider_mappings: set[ProviderMapping]
) -> Album:
    """Add an album with the given provider mappings and return the library item."""
    return await music.albums.add_item_to_library(
        Album(
            item_id="album_1",
            provider=FILESYSTEM_INSTANCE,
            name="Test Album",
            album_type=AlbumType.ALBUM,
            provider_mappings=provider_mappings,
            artists=UniqueList([_create_artist(FILESYSTEM_INSTANCE)]),
        )
    )


async def test_album_tracks_merge_sorts_unknown_disc_with_disc_one(
    music: MusicController,
) -> None:
    """Provider tracks with disc_number=0 sort by track number among disc 1 tracks."""
    library_album = await _add_library_album(
        music,
        {
            _provider_mapping(FILESYSTEM_INSTANCE, "album_1"),
            _provider_mapping(YTMUSIC_INSTANCE, "yt_album_1"),
        },
    )
    # local files: disc 1, tracks 1-7
    for track_number in range(1, 8):
        await music.tracks.add_item_to_library(
            _create_track(
                FILESYSTEM_INSTANCE,
                f"fs_{track_number}",
                f"Track {track_number}",
                disc_number=1,
                track_number=track_number,
                album=library_album,
            )
        )
    # YT Music exposes the full album (tracks 1-9) but hardcodes disc_number=0
    yt_tracks = [
        _create_track(YTMUSIC_INSTANCE, f"yt_{n}", f"Track {n}", disc_number=0, track_number=n)
        for n in range(1, 10)
    ]

    async def fake_provider_tracks(_item_id: str, provider_instance: str) -> list[Track]:
        """Return the provider album tracks for the fake YT Music instance."""
        return yt_tracks if provider_instance == YTMUSIC_INSTANCE else []

    with patch.object(music.albums, "_get_provider_album_tracks", fake_provider_tracks):
        result = await music.albums.tracks(library_album.item_id, "library")

    assert [track.track_number for track in result] == list(range(1, 10))


async def test_album_tracks_in_library_only_sorts_unknown_disc_with_disc_one(
    music: MusicController,
) -> None:
    """Library tracks with disc_number=0 sort by track number among disc 1 tracks."""
    library_album = await _add_library_album(
        music, {_provider_mapping(FILESYSTEM_INSTANCE, "album_1")}
    )
    # disc 1, tracks 1-7 plus disc-unknown (0) tracks 8-9
    disc_track_numbers = [(1, n) for n in range(1, 8)] + [(0, 8), (0, 9)]
    for disc_number, track_number in disc_track_numbers:
        await music.tracks.add_item_to_library(
            _create_track(
                FILESYSTEM_INSTANCE,
                f"fs_{track_number}",
                f"Track {track_number}",
                disc_number=disc_number,
                track_number=track_number,
                album=library_album,
            )
        )

    result = await music.albums.tracks(library_album.item_id, "library", in_library_only=True)

    assert [track.track_number for track in result] == list(range(1, 10))
