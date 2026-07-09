"""Tests for image-cache invalidation on explicit library item updates."""

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


def _controller_returning(*items: Track) -> tuple[TracksController, AsyncMock, AsyncMock]:
    """Build a TracksController with mocked db plus its invalidation/get mocks."""
    mass = MagicMock()
    invalidate_mock = AsyncMock()
    mass.metadata.invalidate_image_cache = invalidate_mock
    mass.get_provider.return_value = None
    controller = TracksController(mass)
    get_item_mock = AsyncMock(side_effect=list(items))
    controller.get_library_item = get_item_mock  # type: ignore[method-assign]
    controller._update_library_item = AsyncMock()  # type: ignore[method-assign]
    return controller, invalidate_mock, get_item_mock


async def test_replaced_image_is_invalidated() -> None:
    """Replacing an item's image invalidates the cached artwork of the old image."""
    prev_item = _track([_image("Artist/old-cover.jpg")])
    updated_item = _track([_image("Artist/new-cover.jpg")])
    controller, invalidate_mock, _ = _controller_returning(prev_item, updated_item)
    await controller.update_item_in_library("1", updated_item)
    invalidate_mock.assert_awaited_once_with("test--1", "Artist/old-cover.jpg")


async def test_unchanged_images_are_not_invalidated() -> None:
    """An update that keeps the same images busts nothing."""
    prev_item = _track([_image("Artist/cover.jpg")])
    updated_item = _track([_image("Artist/cover.jpg")])
    controller, invalidate_mock, _ = _controller_returning(prev_item, updated_item)
    await controller.update_item_in_library("1", updated_item)
    invalidate_mock.assert_not_awaited()


async def test_added_image_is_not_invalidated() -> None:
    """Merely adding an image (e.g. metadata enrichment) busts nothing."""
    prev_item = _track([])
    updated_item = _track([_image("Artist/fresh-cover.jpg")])
    controller, invalidate_mock, _ = _controller_returning(prev_item, updated_item)
    await controller.update_item_in_library("1", updated_item)
    invalidate_mock.assert_not_awaited()


async def test_suppressed_update_skips_snapshot_and_invalidation() -> None:
    """During a provider sync neither the pre-fetch nor any invalidation runs."""
    updated_item = _track([_image("Artist/new-cover.jpg")])
    controller, invalidate_mock, get_item_mock = _controller_returning(updated_item)
    token = SUPPRESS_MEDIA_ITEM_UPDATES.set(True)
    try:
        await controller.update_item_in_library("1", updated_item)
    finally:
        SUPPRESS_MEDIA_ITEM_UPDATES.reset(token)
    # only the post-update read happened; no snapshot read, no invalidation
    assert get_item_mock.await_count == 1
    invalidate_mock.assert_not_awaited()
