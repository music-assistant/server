"""Concrete Music Assistant models for MSX Bridge tests."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from music_assistant_models.enums import ImageType
from music_assistant_models.media_items import (
    Album,
    Artist,
    MediaItemImage,
    MediaItemMetadata,
    Playlist,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.player import PlayerMedia
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem


def image(path: str = "image", provider: str = "library") -> MediaItemImage:
    """Create image metadata for a library media item."""
    return MediaItemImage(type=ImageType.THUMB, path=path, provider=provider)


def artist(item_id: str = "artist-1", name: str = "Test Artist") -> Artist:
    """Create a library artist."""
    return Artist(item_id=item_id, provider="library", name=name, provider_mappings=set())


def album(
    item_id: str = "album-1",
    name: str = "Test Album",
    artists: Sequence[Artist] | None = None,
    image_path: str | None = None,
) -> Album:
    """Create a library album with optional artists and image metadata."""
    metadata = (
        MediaItemMetadata(images=UniqueList([image(image_path)]))
        if image_path is not None
        else MediaItemMetadata()
    )
    return Album(
        item_id=item_id,
        provider="library",
        name=name,
        provider_mappings=set(),
        artists=UniqueList(artists if artists is not None else [artist()]),
        metadata=metadata,
    )


def track(
    item_id: str = "track-1",
    name: str = "Test Track",
    *,
    duration: int = 180,
    artists: Sequence[Artist] | None = None,
    album: Album | None = None,
    image_path: str | None = None,
    disc_number: int = 0,
    track_number: int = 0,
    uri: str | None = None,
) -> Track:
    """Create a library track with the fields consumed by the provider."""
    metadata = (
        MediaItemMetadata(images=UniqueList([image(image_path)]))
        if image_path is not None
        else MediaItemMetadata()
    )
    return Track(
        item_id=item_id,
        provider="library",
        name=name,
        uri=uri,
        provider_mappings=set(),
        duration=duration,
        artists=UniqueList(artists if artists is not None else [artist()]),
        album=album,
        disc_number=disc_number,
        track_number=track_number,
        metadata=metadata,
    )


def playlist(
    item_id: str = "playlist-1", name: str = "Test Playlist", owner: str = "test_user"
) -> Playlist:
    """Create a library playlist."""
    return Playlist(
        item_id=item_id,
        provider="library",
        name=name,
        provider_mappings=set(),
        owner=owner,
    )


def queue_item(
    media_item: Track | None = None,
    *,
    queue_id: str = "msx_test",
    queue_item_id: str = "queue-item-1",
    name: str | None = None,
    duration: int | None = None,
) -> QueueItem:
    """Create a queue item, optionally backed by a concrete track."""
    media = media_item or track(item_id=queue_item_id)
    return QueueItem(
        queue_id=queue_id,
        queue_item_id=queue_item_id,
        name=name or media.name,
        duration=media.duration if duration is None else duration,
        media_item=media,
    )


def player_queue(
    queue_id: str = "msx_test", *, items: int = 0, current_index: int | None = 0
) -> PlayerQueue:
    """Create an active, available queue."""
    return PlayerQueue(
        queue_id=queue_id,
        active=True,
        display_name="Test queue",
        available=True,
        items=items,
        current_index=current_index,
    )


def player_media(uri: str = "library://track/track-1", **kwargs: Any) -> PlayerMedia:
    """Create playback media with explicit test-relevant fields."""
    return PlayerMedia(uri=uri, **kwargs)


def search_results(
    *,
    artists: Sequence[Artist] = (),
    albums: Sequence[Album] = (),
    tracks: Sequence[Track] = (),
    playlists: Sequence[Playlist] = (),
) -> SearchResults:
    """Create a concrete MA search response."""
    return SearchResults(artists=artists, albums=albums, tracks=tracks, playlists=playlists)
