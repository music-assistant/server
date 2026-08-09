"""Tests for cleanup of library items/images when a provider is removed."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.enums import EventType, ImageType
from music_assistant_models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant_models.media_items import (
    Artist,
    Audiobook,
    MediaItemImage,
    Playlist,
    Podcast,
    ProviderMapping,
    UniqueList,
)

from music_assistant.constants import DB_TABLE_PROVIDER_MAPPINGS
from music_assistant.controllers.music.media.base import MediaControllerBase
from music_assistant.mass import MusicAssistant

FS_INSTANCE = "filesystem_local--AbCd"
STREAM_INSTANCE = "spotify--EfGh"


def _artist_with_two_providers(name: str) -> Artist:
    """Build an artist that exists on both a filesystem and a streaming provider."""
    artist = Artist(
        item_id="fs1",
        provider=FS_INSTANCE,
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id="fs1", provider_domain="filesystem_local", provider_instance=FS_INSTANCE
            ),
            ProviderMapping(
                item_id="sp1", provider_domain="spotify", provider_instance=STREAM_INSTANCE
            ),
        },
    )
    artist.metadata.images = UniqueList(
        [
            # local file path image: unresolvable once the filesystem provider is gone
            MediaItemImage(
                type=ImageType.THUMB,
                path="Artist/folder.jpg",
                provider=FS_INSTANCE,
                remotely_accessible=False,
            ),
            # streaming image: stays valid, must be kept
            MediaItemImage(
                type=ImageType.THUMB,
                path="https://i.scdn.co/image/abc",
                provider=STREAM_INSTANCE,
                remotely_accessible=True,
            ),
        ]
    )
    return artist


async def test_remove_provider_mappings_strips_provider_images(mass: MusicAssistant) -> None:
    """Removing a provider's mappings keeps the item but drops only that provider's images."""
    artists = mass.music.artists
    db_artist = await artists.add_item_to_library(_artist_with_two_providers("Two Provider Artist"))
    db_id = int(db_artist.item_id)
    assert db_artist.metadata.images is not None
    assert len(db_artist.metadata.images) == 2

    await artists.remove_provider_mappings(db_id, FS_INSTANCE)

    # the item survives (it still has the streaming provider mapping)
    updated = await artists.get_library_item(db_id)
    assert {pm.provider_instance for pm in updated.provider_mappings} == {STREAM_INSTANCE}
    # only the streaming image remains; the (now unresolvable) filesystem image is gone
    assert updated.metadata.images is not None
    assert [img.provider for img in updated.metadata.images] == [STREAM_INSTANCE]


async def test_cleanup_filesystem_provider_does_not_reset_database(mass: MusicAssistant) -> None:
    """Removing a local (filesystem) provider cleans up its items without wiping the db."""
    artists = mass.music.artists
    # an artist that only exists on the filesystem provider (should be removed entirely)
    fs_only = Artist(
        item_id="fsonly",
        provider=FS_INSTANCE,
        name="Filesystem Only Artist",
        provider_mappings={
            ProviderMapping(
                item_id="fsonly",
                provider_domain="filesystem_local",
                provider_instance=FS_INSTANCE,
            )
        },
    )
    db_fs_only = await artists.add_item_to_library(fs_only)
    # an artist from an unrelated provider that must survive the cleanup
    survivor = Artist(
        item_id="sp2",
        provider=STREAM_INSTANCE,
        name="Surviving Artist",
        provider_mappings={
            ProviderMapping(
                item_id="sp2", provider_domain="spotify", provider_instance=STREAM_INSTANCE
            )
        },
    )
    db_survivor = await artists.add_item_to_library(survivor)

    with patch.object(mass.music, "_reset_database", AsyncMock()) as mock_reset:
        await mass.music.cleanup_provider(FS_INSTANCE)

    # the whole database must NOT be wiped for a local provider anymore
    mock_reset.assert_not_called()
    # the filesystem-only item is gone, the unrelated item survives
    with pytest.raises(MediaNotFoundError):
        await artists.get_library_item(int(db_fs_only.item_id))
    assert await artists.get_library_item(int(db_survivor.item_id))


async def test_remove_single_provider_mapping_strips_provider_images(mass: MusicAssistant) -> None:
    """Removing the last mapping of a provider instance also strips its images."""
    artists = mass.music.artists
    db_artist = await artists.add_item_to_library(
        _artist_with_two_providers("Single Mapping Artist")
    )
    db_id = int(db_artist.item_id)

    await artists.remove_provider_mapping(db_id, FS_INSTANCE, "fs1")

    updated = await artists.get_library_item(db_id)
    assert {pm.provider_instance for pm in updated.provider_mappings} == {STREAM_INSTANCE}
    assert updated.metadata.images is not None
    assert [img.provider for img in updated.metadata.images] == [STREAM_INSTANCE]


async def test_remove_single_provider_mapping_keeps_images_if_instance_remains(
    mass: MusicAssistant,
) -> None:
    """A provider's images are kept while another mapping of that same instance remains."""
    artists = mass.music.artists
    artist = Artist(
        item_id="fs1",
        provider=FS_INSTANCE,
        name="Duplicate Mapping Artist",
        provider_mappings={
            ProviderMapping(
                item_id="fs1", provider_domain="filesystem_local", provider_instance=FS_INSTANCE
            ),
            ProviderMapping(
                item_id="fs2", provider_domain="filesystem_local", provider_instance=FS_INSTANCE
            ),
        },
    )
    artist.metadata.images = UniqueList(
        [
            MediaItemImage(
                type=ImageType.THUMB,
                path="Artist/folder.jpg",
                provider=FS_INSTANCE,
                remotely_accessible=False,
            )
        ]
    )
    db_artist = await artists.add_item_to_library(artist)
    db_id = int(db_artist.item_id)

    # remove only one of the two mappings for the same provider instance
    await artists.remove_provider_mapping(db_id, FS_INSTANCE, "fs1")

    # the instance still maps the item (via fs2), so its image must be kept
    updated = await artists.get_library_item(db_id)
    assert {pm.item_id for pm in updated.provider_mappings} == {"fs2"}
    assert updated.metadata.images is not None
    assert [img.provider for img in updated.metadata.images] == [FS_INSTANCE]


async def test_cleanup_suppresses_media_item_updated_events(mass: MusicAssistant) -> None:
    """A surviving item must not emit MEDIA_ITEM_UPDATED during bulk provider cleanup."""
    artists = mass.music.artists
    db_artist = await artists.add_item_to_library(_artist_with_two_providers("Suppressed Artist"))
    db_id = int(db_artist.item_id)

    updated_object_ids: list[str | None] = []
    real_signal_event = mass.signal_event

    def _spy(event: EventType, object_id: str | None = None, data: object = None) -> None:
        if event == EventType.MEDIA_ITEM_UPDATED:
            updated_object_ids.append(object_id)
        real_signal_event(event, object_id, data)

    with patch.object(mass, "signal_event", side_effect=_spy):
        await mass.music.cleanup_provider(FS_INSTANCE)

    # no per-item update event was emitted during the bulk cleanup
    assert updated_object_ids == []
    # but the cleanup work still happened: the filesystem image was stripped, item kept
    updated = await artists.get_library_item(db_id)
    assert {pm.provider_instance for pm in updated.provider_mappings} == {STREAM_INSTANCE}
    assert [img.provider for img in (updated.metadata.images or [])] == [STREAM_INSTANCE]


async def test_remove_provider_mappings_emits_event_without_image_changes(
    mass: MusicAssistant,
) -> None:
    """Removing mappings emits an update event even when no images were stripped."""
    artists = mass.music.artists
    # artist on two providers but without any images, so nothing gets stripped
    artist = Artist(
        item_id="fs1",
        provider=FS_INSTANCE,
        name="No Image Artist",
        provider_mappings={
            ProviderMapping(
                item_id="fs1", provider_domain="filesystem_local", provider_instance=FS_INSTANCE
            ),
            ProviderMapping(
                item_id="sp1", provider_domain="spotify", provider_instance=STREAM_INSTANCE
            ),
        },
    )
    db_artist = await artists.add_item_to_library(artist)
    db_id = int(db_artist.item_id)

    events: list[Artist] = []
    real_signal_event = mass.signal_event

    def _spy(event: EventType, object_id: str | None = None, data: object = None) -> None:
        if event == EventType.MEDIA_ITEM_UPDATED and isinstance(data, Artist):
            events.append(data)
        real_signal_event(event, object_id, data)

    with patch.object(mass, "signal_event", side_effect=_spy):
        await artists.remove_provider_mappings(db_id, FS_INSTANCE)

    # the update event fires purely because a provider mapping was removed, and its
    # payload reflects the removed mapping
    assert len(events) == 1
    assert {pm.provider_instance for pm in events[0].provider_mappings} == {STREAM_INSTANCE}


async def test_failed_item_removal_keeps_provider_mapping(mass: MusicAssistant) -> None:
    """A failing library removal must not leave the item behind without any providers."""
    artists = mass.music.artists
    fs_only = Artist(
        item_id="fsonly",
        provider=FS_INSTANCE,
        name="Filesystem Only Artist",
        provider_mappings={
            ProviderMapping(
                item_id="fsonly",
                provider_domain="filesystem_local",
                provider_instance=FS_INSTANCE,
            )
        },
    )
    db_artist = await artists.add_item_to_library(fs_only)
    db_id = int(db_artist.item_id)

    with (
        patch.object(
            artists, "remove_item_from_library", AsyncMock(side_effect=MusicAssistantError("boom"))
        ),
        pytest.raises(MusicAssistantError),
    ):
        await artists.remove_provider_mappings(db_id, FS_INSTANCE)

    # the item survives *with* its mapping, so the removal can be retried
    updated = await artists.get_library_item(db_id)
    assert {pm.provider_instance for pm in updated.provider_mappings} == {FS_INSTANCE}


async def test_failed_item_removal_keeps_single_provider_mapping(mass: MusicAssistant) -> None:
    """The same applies when the item's last individual mapping is removed."""
    artists = mass.music.artists
    fs_only = Artist(
        item_id="fsonly",
        provider=FS_INSTANCE,
        name="Filesystem Only Artist",
        provider_mappings={
            ProviderMapping(
                item_id="fsonly",
                provider_domain="filesystem_local",
                provider_instance=FS_INSTANCE,
            )
        },
    )
    db_artist = await artists.add_item_to_library(fs_only)
    db_id = int(db_artist.item_id)

    with (
        patch.object(
            artists, "remove_item_from_library", AsyncMock(side_effect=MusicAssistantError("boom"))
        ),
        pytest.raises(MusicAssistantError),
    ):
        await artists.remove_provider_mapping(db_id, FS_INSTANCE, "fsonly")

    updated = await artists.get_library_item(db_id)
    assert {pm.provider_instance for pm in updated.provider_mappings} == {FS_INSTANCE}


async def _assert_orphan_is_pruned(
    mass: MusicAssistant, controller: MediaControllerBase[Any], db_id: int
) -> None:
    """Strip an item's provider mapping rows and assert the periodic cleanup removes it."""
    await mass.music.database.delete(
        DB_TABLE_PROVIDER_MAPPINGS,
        {"media_type": controller.media_type.value, "item_id": db_id},
    )

    await mass.music._cleanup_database()

    with pytest.raises(MediaNotFoundError):
        await controller.get_library_item(db_id)


async def test_database_cleanup_removes_orphaned_podcasts(mass: MusicAssistant) -> None:
    """A podcast without any provider mapping is cleaned up."""
    podcast = Podcast(
        item_id="show1",
        provider=FS_INSTANCE,
        name="Orphaned Show",
        provider_mappings={
            ProviderMapping(
                item_id="show1", provider_domain="filesystem_local", provider_instance=FS_INSTANCE
            )
        },
    )
    db_item = await mass.music.podcasts.add_item_to_library(podcast)
    await _assert_orphan_is_pruned(mass, mass.music.podcasts, int(db_item.item_id))


async def test_database_cleanup_removes_orphaned_audiobooks(mass: MusicAssistant) -> None:
    """An audiobook without any provider mapping is cleaned up."""
    audiobook = Audiobook(
        item_id="book1",
        provider=FS_INSTANCE,
        name="Orphaned Book",
        provider_mappings={
            ProviderMapping(
                item_id="book1", provider_domain="filesystem_local", provider_instance=FS_INSTANCE
            )
        },
    )
    db_item = await mass.music.audiobooks.add_item_to_library(audiobook)
    await _assert_orphan_is_pruned(mass, mass.music.audiobooks, int(db_item.item_id))


async def test_item_without_provider_mappings_raises_media_not_found(
    mass: MusicAssistant,
) -> None:
    """An item that lost all its providers reports a clean 'not found' error."""
    playlists = mass.music.playlists
    playlist = Playlist(
        item_id="pl1",
        provider=FS_INSTANCE,
        name="Orphaned Playlist",
        owner="tester",
        is_editable=False,
        provider_mappings={
            ProviderMapping(
                item_id="pl1", provider_domain="filesystem_local", provider_instance=FS_INSTANCE
            )
        },
    )
    db_playlist = await playlists.add_item_to_library(playlist)
    db_id = int(db_playlist.item_id)
    await mass.music.database.delete(
        DB_TABLE_PROVIDER_MAPPINGS,
        {"media_type": playlists.media_type.value, "item_id": db_id},
    )

    with pytest.raises(MediaNotFoundError):
        async for _ in playlists.tracks(str(db_id), "library"):
            pass
