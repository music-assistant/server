"""Integration tests for non-destructive library item merges."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.enums import AlbumType, ArtistType, ExternalID, MediaType
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError, MusicAssistantError
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
    DB_TABLE_TRACKS,
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


async def test_genre_merge_rejects_different_taxonomies(mass: MusicAssistant) -> None:
    """A genre merge cannot cross music and podcast taxonomies."""
    target = await mass.music.genres.add_item_to_library(
        Genre(
            item_id="0",
            provider="library",
            name="Podcast Genre",
            content_type=MediaType.PODCAST,
            provider_mappings=set(),
        )
    )
    source = await mass.music.genres.add_item_to_library(
        Genre(item_id="0", provider="library", name="Music Genre", provider_mappings=set())
    )

    with pytest.raises(InvalidDataError, match="same taxonomy"):
        await mass.music.genres.merge_library_items(target.item_id, source.item_id)

    assert await mass.music.genres.get_library_item(target.item_id)
    assert await mass.music.genres.get_library_item(source.item_id)


async def test_artist_merge_rejects_different_roles(mass: MusicAssistant) -> None:
    """An artist merge cannot cross music and spoken-word roles."""
    target = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name="Singer",
            artist_type=ArtistType.SINGER,
            provider_mappings={_mapping("target_instance", "target-artist")},
        )
    )
    source = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name="Author",
            artist_type=ArtistType.AUTHOR,
            provider_mappings={_mapping("source_instance", "source-artist")},
        )
    )

    with pytest.raises(InvalidDataError, match="same role"):
        await mass.music.artists.merge_library_items(target.item_id, source.item_id)

    assert await mass.music.artists.get_library_item(target.item_id)
    assert await mass.music.artists.get_library_item(source.item_id)


async def test_merge_retry_does_not_double_play_count(mass: MusicAssistant) -> None:
    """A retry after a later transfer failure preserves the merged play count."""
    artist = await _add_artist(mass, "Artist", "target_instance", "target-artist")
    target_album = await _add_album(
        mass,
        "Target Album",
        "target_instance",
        "target-album",
        artist,
        "target-barcode",
    )
    source_album = await _add_album(
        mass,
        "Source Album",
        "source_instance",
        "source-album",
        artist,
        "source-barcode",
    )
    target = await _add_track(
        mass,
        "Target Track",
        "target_instance",
        "target-track",
        artist,
        target_album,
    )
    source = await _add_track(
        mass,
        "Source Track",
        "source_instance",
        "source-track",
        artist,
        source_album,
    )
    await mass.music.database.update(
        DB_TABLE_TRACKS, {"item_id": int(target.item_id)}, {"play_count": 2}
    )
    await mass.music.database.update(
        DB_TABLE_TRACKS, {"item_id": int(source.item_id)}, {"play_count": 3}
    )

    with (
        patch.object(
            mass.music.tracks,
            "_merge_genre_mappings",
            AsyncMock(side_effect=MusicAssistantError("merge failure")),
        ),
        pytest.raises(MusicAssistantError, match="merge failure"),
    ):
        await mass.music.tracks.merge_library_items(target.item_id, source.item_id)

    target_row = await mass.music.database.get_row(
        DB_TABLE_TRACKS, {"item_id": int(target.item_id)}
    )
    source_row = await mass.music.database.get_row(
        DB_TABLE_TRACKS, {"item_id": int(source.item_id)}
    )
    assert target_row is not None
    assert source_row is not None
    assert target_row["play_count"] == 5
    assert source_row["play_count"] == 0

    await mass.music.tracks.merge_library_items(target.item_id, source.item_id)

    merged_row = await mass.music.database.get_row(
        DB_TABLE_TRACKS, {"item_id": int(target.item_id)}
    )
    assert merged_row is not None
    assert merged_row["play_count"] == 5


async def test_merge_interrupted_after_relation_transfer_leaves_source_reconcilable(
    mass: MusicAssistant,
) -> None:
    """A merge cut short right after the relation transfer keeps the source row repairable."""
    artist = await _add_artist(mass, "Shared Artist", "target_instance", "shared-artist")
    album = await _add_album(
        mass, "Shared Album", "target_instance", "shared-album", artist, "shared-barcode"
    )
    target = await _add_track(
        mass, "Target Track", "target_instance", "target-track", artist, album
    )
    source = await _add_track(
        mass, "Source Track", "source_instance", "source-track", artist, album
    )

    original = MediaControllerBase._copy_library_item_relations

    async def _fail_after_relation_transfer(
        self: MediaControllerBase[Track], target_id: int, source_id: int
    ) -> None:
        await original(self, target_id, source_id)
        raise MusicAssistantError("interrupted")

    with (
        patch.object(
            MediaControllerBase, "_copy_library_item_relations", _fail_after_relation_transfer
        ),
        pytest.raises(MusicAssistantError, match="interrupted"),
    ):
        await mass.music.tracks.merge_library_items(target.item_id, source.item_id)

    # the source still owns the album and artist relations that identify it, so the
    # duplicate reconciliation pass can still see the pair and finish the merge later
    assert await mass.music.database.get_rows(
        DB_TABLE_ALBUM_TRACKS, {"track_id": int(source.item_id)}
    )
    assert await mass.music.database.get_rows(
        DB_TABLE_TRACK_ARTISTS, {"track_id": int(source.item_id)}
    )
    assert await mass.music.database.get_rows(
        DB_TABLE_PROVIDER_MAPPINGS,
        {"media_type": MediaType.TRACK.value, "item_id": int(source.item_id)},
    )

    await mass.music.tracks.merge_library_items(target.item_id, source.item_id)

    with pytest.raises(MediaNotFoundError):
        await mass.music.tracks.get_library_item(source.item_id)
    assert {
        int(row["album_id"])
        for row in await mass.music.database.get_rows(
            DB_TABLE_ALBUM_TRACKS, {"track_id": int(target.item_id)}
        )
    } == {int(album.item_id)}
    assert not await mass.music.database.get_rows(
        DB_TABLE_ALBUM_TRACKS, {"track_id": int(source.item_id)}
    )
    assert not await mass.music.database.get_rows(
        DB_TABLE_TRACK_ARTISTS, {"track_id": int(source.item_id)}
    )
    assert {
        int(row["item_id"])
        for row in await mass.music.database.get_rows(
            DB_TABLE_PROVIDER_MAPPINGS, {"media_type": MediaType.TRACK.value}
        )
    } == {int(target.item_id)}


async def test_merge_interrupted_before_relation_drop_keeps_target_complete(
    mass: MusicAssistant,
) -> None:
    """A merge cut short before the source relations are dropped leaves nothing to lose."""
    artist = await _add_artist(mass, "Shared Artist", "target_instance", "shared-artist")
    album = await _add_album(
        mass, "Shared Album", "target_instance", "shared-album", artist, "shared-barcode"
    )
    source_album = await _add_album(
        mass, "Source Album", "source_instance", "source-album", artist, "source-barcode"
    )
    target = await _add_track(
        mass, "Target Track", "target_instance", "target-track", artist, album
    )
    source = await _add_track(
        mass, "Source Track", "source_instance", "source-track", artist, source_album
    )

    with (
        patch.object(
            MediaControllerBase,
            "_drop_library_item_relations",
            AsyncMock(side_effect=MusicAssistantError("interrupted")),
        ),
        pytest.raises(MusicAssistantError, match="interrupted"),
    ):
        await mass.music.tracks.merge_library_items(target.item_id, source.item_id)

    # the source has already handed over its provider mappings, so the source-less item
    # cleanup may delete it at any time: the target must hold every relation by now
    assert not await mass.music.database.get_rows(
        DB_TABLE_PROVIDER_MAPPINGS,
        {"media_type": MediaType.TRACK.value, "item_id": int(source.item_id)},
    )
    assert {
        int(row["album_id"])
        for row in await mass.music.database.get_rows(
            DB_TABLE_ALBUM_TRACKS, {"track_id": int(target.item_id)}
        )
    } == {int(album.item_id), int(source_album.item_id)}
    assert {
        int(row["artist_id"])
        for row in await mass.music.database.get_rows(
            DB_TABLE_TRACK_ARTISTS, {"track_id": int(target.item_id)}
        )
    } == {int(artist.item_id)}


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
