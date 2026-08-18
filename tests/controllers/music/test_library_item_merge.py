"""Integration tests for non-destructive library item merges."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.enums import AlbumType, ExternalID, MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    Genre,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.constants import (
    DB_TABLE_ALBUM_ARTISTS,
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION,
    DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
    DB_TABLE_PLAYLOG,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_TRACK_ARTISTS,
)
from music_assistant.controllers.music.media.base import (
    SUPPRESS_MEDIA_ITEM_UPDATES,
    MediaControllerBase,
)
from music_assistant.mass import MusicAssistant


def _mapping(provider_instance: str, item_id: str) -> ProviderMapping:
    """Create a provider mapping for a library fixture item."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain=provider_instance.removesuffix("_instance"),
        provider_instance=provider_instance,
        in_library=True,
    )


async def _add_artist(
    mass: MusicAssistant, name: str, provider_instance: str, provider_item_id: str
) -> Artist:
    """Create and store a fixture artist."""
    return await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name=name,
            provider_mappings={_mapping(provider_instance, provider_item_id)},
        )
    )


async def _add_album(
    mass: MusicAssistant,
    name: str,
    provider_instance: str,
    provider_item_id: str,
    artist: Artist,
    external_id: str,
) -> Album:
    """Create and store a fixture album."""
    return await mass.music.albums.add_item_to_library(
        Album(
            item_id="0",
            provider="library",
            name=name,
            album_type=AlbumType.ALBUM,
            provider_mappings={_mapping(provider_instance, provider_item_id)},
            external_ids={(ExternalID.BARCODE, external_id)},
            artists=UniqueList([artist]),
        )
    )


async def _add_track(
    mass: MusicAssistant,
    name: str,
    provider_instance: str,
    provider_item_id: str,
    artist: Artist,
    album: Album,
) -> Track:
    """Create and store a fixture track."""
    return await mass.music.tracks.add_item_to_library(
        Track(
            item_id="0",
            provider="library",
            name=name,
            provider_mappings={_mapping(provider_instance, provider_item_id)},
            artists=UniqueList([artist]),
            album=album,
            disc_number=1,
            track_number=1,
        )
    )


async def _add_playlog(
    mass: MusicAssistant,
    item_id: str | int,
    provider: str,
    media_type: MediaType,
    *,
    timestamp: int,
    seconds_played: int,
    user_initiated: bool,
) -> None:
    """Insert a playlog row for merge coverage."""
    await mass.music.database.insert(
        DB_TABLE_PLAYLOG,
        {
            "item_id": item_id,
            "provider": provider,
            "media_type": media_type.value,
            "name": "Merge test",
            "timestamp": timestamp,
            "seconds_played": seconds_played,
            "userid": "test-user",
            "user_initiated": user_initiated,
        },
    )


async def test_mapping_conflict_merges_albums_without_deleting_tracks(
    mass: MusicAssistant,
) -> None:
    """A mapping conflict merges albums without recursively deleting their tracks."""
    target_artist = await _add_artist(mass, "Target Artist", "target_instance", "target-artist")
    source_artist = await _add_artist(mass, "Source Artist", "source_instance", "source-artist")
    target = await _add_album(
        mass,
        "Target Album",
        "target_instance",
        "target-album",
        target_artist,
        "target-barcode",
    )
    source = await _add_album(
        mass,
        "Source Album",
        "source_instance",
        "source-album",
        source_artist,
        "source-barcode",
    )
    shared_track = await _add_track(
        mass,
        "Shared Track",
        "shared_instance",
        "shared-track",
        target_artist,
        target,
    )
    source_track = await _add_track(
        mass,
        "Source Track",
        "source-track_instance",
        "source-track",
        source_artist,
        source,
    )
    await mass.music.database.insert(
        DB_TABLE_ALBUM_TRACKS,
        {
            "track_id": int(shared_track.item_id),
            "album_id": int(source.item_id),
            "disc_number": 1,
            "track_number": 1,
        },
    )
    await mass.music.database.update(
        DB_TABLE_ALBUMS,
        {"item_id": int(target.item_id)},
        {
            "favorite": False,
            "play_count": 2,
            "last_played": 20,
            "timestamp_added": 20,
            "timestamp_modified": 20,
        },
    )
    await mass.music.database.update(
        DB_TABLE_ALBUMS,
        {"item_id": int(source.item_id)},
        {
            "favorite": True,
            "play_count": 3,
            "last_played": 30,
            "timestamp_added": 10,
            "timestamp_modified": 30,
        },
    )
    genre = await mass.music.genres.add_item_to_library(
        Genre(item_id="0", provider="library", name="Merge Genre", provider_mappings=set())
    )
    await mass.music.database.insert(
        DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
        {
            "genre_id": int(genre.item_id),
            "media_id": int(target.item_id),
            "media_type": MediaType.ALBUM.value,
            "alias": "target",
            "is_derived": True,
            "is_manual": False,
        },
    )
    await mass.music.database.insert(
        DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
        {
            "genre_id": int(genre.item_id),
            "media_id": int(source.item_id),
            "media_type": MediaType.ALBUM.value,
            "alias": "source",
            "is_derived": False,
            "is_manual": True,
        },
    )
    await mass.music.database.insert(
        DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION,
        {
            "genre_id": int(genre.item_id),
            "media_id": int(source.item_id),
            "media_type": MediaType.ALBUM.value,
        },
    )
    await _add_playlog(
        mass,
        target.item_id,
        "library",
        MediaType.ALBUM,
        timestamp=20,
        seconds_played=20,
        user_initiated=False,
    )
    await _add_playlog(
        mass,
        source.item_id,
        "library",
        MediaType.ALBUM,
        timestamp=30,
        seconds_played=30,
        user_initiated=True,
    )
    await _add_playlog(
        mass,
        "source-album",
        "source_instance",
        MediaType.ALBUM,
        timestamp=30,
        seconds_played=30,
        user_initiated=True,
    )

    original_remove = MediaControllerBase.remove_item_from_library

    async def assert_transferred_before_delete(
        controller: MediaControllerBase[Album], item_id: str | int, recursive: bool = True
    ) -> None:
        if int(item_id) == int(source.item_id):
            assert await mass.music.database.get_row(
                DB_TABLE_PROVIDER_MAPPINGS,
                {
                    "media_type": MediaType.ALBUM.value,
                    "item_id": int(target.item_id),
                    "provider_instance": "source_instance",
                    "provider_item_id": "source-album",
                },
            )
            assert await mass.music.database.get_row(
                DB_TABLE_ALBUM_TRACKS,
                {"track_id": int(source_track.item_id), "album_id": int(target.item_id)},
            )
        await original_remove(controller, item_id, recursive)

    with (
        patch.object(
            MediaControllerBase, "remove_item_from_library", assert_transferred_before_delete
        ),
        patch.object(
            mass.music.albums,
            "update_item_in_library",
            wraps=mass.music.albums.update_item_in_library,
        ) as update_item,
    ):
        await mass.music.albums.add_provider_mappings(target.item_id, source.provider_mappings)
    update_item.assert_not_awaited()

    await _assert_album_merge_result(
        mass,
        target,
        source,
        shared_track,
        source_track,
        target_artist,
        source_artist,
        genre,
    )


async def test_track_merge_preserves_album_and_artist_relations(mass: MusicAssistant) -> None:
    """A track merge transfers all album and artist relations without deleting albums."""
    target_artist = await _add_artist(mass, "Target Artist", "target_instance", "target-artist")
    source_artist = await _add_artist(mass, "Source Artist", "source_instance", "source-artist")
    target_album = await _add_album(
        mass,
        "Target Album",
        "target_instance",
        "target-album",
        target_artist,
        "target-barcode",
    )
    source_album = await _add_album(
        mass,
        "Source Album",
        "source_instance",
        "source-album",
        source_artist,
        "source-barcode",
    )
    target = await _add_track(
        mass,
        "Target Track",
        "target_instance",
        "target-track",
        target_artist,
        target_album,
    )
    source = await _add_track(
        mass,
        "Source Track",
        "source_instance",
        "source-track",
        source_artist,
        source_album,
    )
    await mass.music.database.insert(
        DB_TABLE_ALBUM_TRACKS,
        {
            "track_id": int(target.item_id),
            "album_id": int(source_album.item_id),
            "disc_number": 7,
            "track_number": 8,
        },
    )

    await mass.music.tracks.merge_library_items(target.item_id, source.item_id)

    with pytest.raises(MediaNotFoundError):
        await mass.music.tracks.get_library_item(source.item_id)
    assert await mass.music.albums.get_library_item(target_album.item_id)
    assert await mass.music.albums.get_library_item(source_album.item_id)
    assert {
        int(row["album_id"])
        for row in await mass.music.database.get_rows(
            DB_TABLE_ALBUM_TRACKS, {"track_id": int(target.item_id)}
        )
    } == {int(target_album.item_id), int(source_album.item_id)}
    assert {
        int(row["artist_id"])
        for row in await mass.music.database.get_rows(
            DB_TABLE_TRACK_ARTISTS, {"track_id": int(target.item_id)}
        )
    } == {int(target_artist.item_id), int(source_artist.item_id)}
    source_album_track = await mass.music.database.get_row(
        DB_TABLE_ALBUM_TRACKS,
        {"track_id": int(target.item_id), "album_id": int(source_album.item_id)},
    )
    assert source_album_track is not None
    assert source_album_track["disc_number"] == 7
    assert source_album_track["track_number"] == 8
    with pytest.raises(MediaNotFoundError):
        await mass.music.tracks.merge_library_items(target.item_id, source.item_id)


async def test_audiobook_merge_keeps_per_user_resume_state(mass: MusicAssistant) -> None:
    """An audiobook merge leaves resume transfer to the library playlog merge."""
    target = await mass.music.audiobooks.add_item_to_library(
        Audiobook(
            item_id="0",
            provider="library",
            name="Target Book",
            duration=3600,
            provider_mappings={_mapping("target_instance", "target-book")},
        )
    )
    source = await mass.music.audiobooks.add_item_to_library(
        Audiobook(
            item_id="0",
            provider="library",
            name="Source Book",
            duration=3600,
            provider_mappings={_mapping("source_instance", "source-book")},
        )
    )
    await _add_playlog(
        mass,
        target.item_id,
        "library",
        MediaType.AUDIOBOOK,
        timestamp=20,
        seconds_played=20,
        user_initiated=False,
    )
    await _add_playlog(
        mass,
        source.item_id,
        "library",
        MediaType.AUDIOBOOK,
        timestamp=30,
        seconds_played=30,
        user_initiated=True,
    )

    with patch.object(
        mass.music.audiobooks,
        "_set_playlog",
        wraps=mass.music.audiobooks._set_playlog,
    ) as set_playlog:
        await mass.music.audiobooks.merge_library_items(target.item_id, source.item_id)

    assert isinstance(set_playlog, AsyncMock)
    set_playlog.assert_not_awaited()
    playlog = await mass.music.database.get_row(
        DB_TABLE_PLAYLOG,
        {
            "item_id": target.item_id,
            "provider": "library",
            "media_type": MediaType.AUDIOBOOK.value,
            "userid": "test-user",
        },
    )
    assert playlog is not None
    assert playlog["timestamp"] == 30
    assert playlog["seconds_played"] == 30
    assert playlog["user_initiated"] == 1


async def test_genre_merge_transfers_genre_references(mass: MusicAssistant) -> None:
    """A genre merge reassigns every media mapping and exclusion to the target genre."""
    target = await mass.music.genres.add_item_to_library(
        Genre(item_id="0", provider="library", name="Target Genre", provider_mappings=set())
    )
    source = await mass.music.genres.add_item_to_library(
        Genre(item_id="0", provider="library", name="Source Genre", provider_mappings=set())
    )
    await mass.music.database.insert(
        DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
        {
            "genre_id": int(target.item_id),
            "media_id": 1,
            "media_type": MediaType.TRACK.value,
            "alias": "target",
            "is_derived": True,
            "is_manual": False,
        },
    )
    await mass.music.database.insert(
        DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
        {
            "genre_id": int(source.item_id),
            "media_id": 1,
            "media_type": MediaType.TRACK.value,
            "alias": "source",
            "is_derived": False,
            "is_manual": True,
        },
    )
    await mass.music.database.insert(
        DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION,
        {
            "genre_id": int(source.item_id),
            "media_id": 2,
            "media_type": MediaType.TRACK.value,
        },
    )

    await mass.music.genres.merge_library_items(target.item_id, source.item_id)

    genre_mapping = await mass.music.database.get_row(
        DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
        {
            "genre_id": int(target.item_id),
            "media_id": 1,
            "media_type": MediaType.TRACK.value,
        },
    )
    assert genre_mapping is not None
    assert dict(genre_mapping) == {
        "genre_id": int(target.item_id),
        "media_id": 1,
        "media_type": MediaType.TRACK.value,
        "alias": "source",
        "is_derived": 1,
        "is_manual": 1,
    }
    assert await mass.music.database.get_row(
        DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION,
        {
            "genre_id": int(target.item_id),
            "media_id": 2,
            "media_type": MediaType.TRACK.value,
        },
    )
    assert not await mass.music.database.get_rows(
        DB_TABLE_GENRE_MEDIA_ITEM_MAPPING, {"genre_id": int(source.item_id)}
    )
    assert not await mass.music.database.get_rows(
        DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION, {"genre_id": int(source.item_id)}
    )


async def test_merge_honors_outer_event_suppression(mass: MusicAssistant) -> None:
    """A merge inside a suppressed update scope does not emit item events."""
    target = await mass.music.genres.add_item_to_library(
        Genre(item_id="0", provider="library", name="Target Genre", provider_mappings=set())
    )
    source = await mass.music.genres.add_item_to_library(
        Genre(item_id="0", provider="library", name="Source Genre", provider_mappings=set())
    )
    token = SUPPRESS_MEDIA_ITEM_UPDATES.set(True)
    try:
        with patch.object(mass, "signal_event") as signal_event:
            await mass.music.genres.merge_library_items(target.item_id, source.item_id)
    finally:
        SUPPRESS_MEDIA_ITEM_UPDATES.reset(token)
    signal_event.assert_not_called()


async def _assert_album_merge_result(
    mass: MusicAssistant,
    target: Album,
    source: Album,
    shared_track: Track,
    source_track: Track,
    target_artist: Artist,
    source_artist: Artist,
    genre: Genre,
) -> None:
    """Assert that an album merge preserved the source state on the target."""
    with pytest.raises(MediaNotFoundError):
        await mass.music.albums.get_library_item(source.item_id)
    assert await mass.music.tracks.get_library_item(shared_track.item_id)
    assert await mass.music.tracks.get_library_item(source_track.item_id)
    merged = await mass.music.albums.get_library_item(target.item_id)
    assert merged.favorite is True
    merged_row = await mass.music.database.get_row(
        DB_TABLE_ALBUMS, {"item_id": int(target.item_id)}
    )
    assert merged_row is not None
    assert merged_row["play_count"] == 5
    assert merged_row["last_played"] == 30
    assert merged_row["timestamp_added"] == 10
    assert {(kind, value) for kind, value in merged.external_ids} == {
        (ExternalID.BARCODE, "target-barcode"),
        (ExternalID.BARCODE, "source-barcode"),
    }
    assert {
        int(row["track_id"])
        for row in await mass.music.database.get_rows(
            DB_TABLE_ALBUM_TRACKS, {"album_id": int(target.item_id)}
        )
    } == {int(shared_track.item_id), int(source_track.item_id)}
    assert not await mass.music.database.get_rows(
        DB_TABLE_ALBUM_TRACKS, {"album_id": int(source.item_id)}
    )
    assert {
        int(row["artist_id"])
        for row in await mass.music.database.get_rows(
            DB_TABLE_ALBUM_ARTISTS, {"album_id": int(target.item_id)}
        )
    } == {int(target_artist.item_id), int(source_artist.item_id)}
    genre_mapping = await mass.music.database.get_row(
        DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
        {
            "genre_id": int(genre.item_id),
            "media_id": int(target.item_id),
            "media_type": MediaType.ALBUM.value,
        },
    )
    assert genre_mapping is not None
    assert dict(genre_mapping) == {
        "genre_id": int(genre.item_id),
        "media_id": int(target.item_id),
        "media_type": MediaType.ALBUM.value,
        "alias": "source",
        "is_derived": 1,
        "is_manual": 1,
    }
    assert await mass.music.database.get_row(
        DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION,
        {
            "genre_id": int(genre.item_id),
            "media_id": int(target.item_id),
            "media_type": MediaType.ALBUM.value,
        },
    )
    playlog = await mass.music.database.get_row(
        DB_TABLE_PLAYLOG,
        {
            "item_id": target.item_id,
            "provider": "library",
            "media_type": MediaType.ALBUM.value,
            "userid": "test-user",
        },
    )
    assert playlog is not None
    assert playlog["timestamp"] == 30
    assert playlog["seconds_played"] == 30
    assert playlog["user_initiated"] == 1
    assert await mass.music.database.get_row(
        DB_TABLE_PLAYLOG,
        {
            "item_id": "source-album",
            "provider": "source_instance",
            "media_type": MediaType.ALBUM.value,
            "userid": "test-user",
        },
    )
