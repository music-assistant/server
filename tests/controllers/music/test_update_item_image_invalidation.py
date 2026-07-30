"""Tests for image-cache invalidation on library item updates and removals."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import ImageType
from music_assistant_models.media_items import (
    MediaItemImage,
    MediaItemMetadata,
    ProviderMapping,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.music.media.base import SUPPRESS_MEDIA_ITEM_UPDATES
from music_assistant.controllers.music.media.tracks import TracksController


def _image(path: str) -> MediaItemImage:
    """Build a MediaItemImage for the given path."""
    return MediaItemImage(
        type=ImageType.THUMB, path=path, provider="test--1", remotely_accessible=False
    )


def _track(images: list[MediaItemImage]) -> Track:
    """Build a minimal library Track carrying the given images."""
    return Track(
        item_id="1",
        provider="library",
        name="Test Track",
        provider_mappings={
            ProviderMapping(item_id="1", provider_domain="test", provider_instance="test--1")
        },
        metadata=MediaItemMetadata(images=UniqueList(images) or None),
    )


def _controller_returning(item: Track) -> tuple[TracksController, AsyncMock]:
    """Build a TracksController with mocked db plus its invalidation mock."""
    mass = MagicMock()
    mass.music.database = AsyncMock()
    # deferred_commit is used as (sync-called) async context manager
    mass.music.database.deferred_commit = MagicMock()
    invalidate_mock = AsyncMock()
    mass.metadata.invalidate_image_cache = invalidate_mock
    mass.get_provider.return_value = None
    controller = TracksController(mass)
    controller.get_library_item = AsyncMock(return_value=item)  # type: ignore[method-assign]
    controller._update_library_item = AsyncMock()  # type: ignore[method-assign]
    return controller, invalidate_mock


async def test_updated_item_artwork_is_invalidated() -> None:
    """Updating an item drops its cached artwork so replaced art is served fresh."""
    updated_item = _track([_image("Artist/cover.jpg")])
    controller, invalidate_mock = _controller_returning(updated_item)
    await controller.update_item_in_library("1", updated_item)
    invalidate_mock.assert_awaited_once_with("test--1", "Artist/cover.jpg")


async def test_update_without_images_invalidates_nothing() -> None:
    """An item without any images busts nothing."""
    updated_item = _track([])
    controller, invalidate_mock = _controller_returning(updated_item)
    await controller.update_item_in_library("1", updated_item)
    invalidate_mock.assert_not_awaited()


async def test_suppressed_update_skips_invalidation() -> None:
    """During a provider sync no invalidation runs for updated items."""
    updated_item = _track([_image("Artist/new-cover.jpg")])
    controller, invalidate_mock = _controller_returning(updated_item)
    token = SUPPRESS_MEDIA_ITEM_UPDATES.set(True)
    try:
        await controller.update_item_in_library("1", updated_item)
    finally:
        SUPPRESS_MEDIA_ITEM_UPDATES.reset(token)
    invalidate_mock.assert_not_awaited()


async def test_removed_item_artwork_is_invalidated() -> None:
    """Removing an item drops its cached artwork."""
    library_item = _track([_image("Artist/cover.jpg")])
    controller, invalidate_mock = _controller_returning(library_item)
    await controller.remove_item_from_library("1", recursive=False)
    invalidate_mock.assert_awaited_once_with("test--1", "Artist/cover.jpg")
