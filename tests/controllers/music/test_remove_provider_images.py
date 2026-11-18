"""Tests for removing (a subset of) a provider's images from a library item."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import ImageType
from music_assistant_models.media_items import MediaItemImage, MediaItemMetadata, UniqueList

from music_assistant.controllers.music.media.albums import AlbumsController
from music_assistant.helpers.json import json_loads, serialize_to_json

PROVIDER_A = "filesystem_local--test"
PROVIDER_B = "spotify--test"


def _image(provider: str, path: str) -> MediaItemImage:
    """Create a MediaItemImage for the given provider/path."""
    return MediaItemImage(type=ImageType.THUMB, path=path, provider=provider)


def _make_controller(images: list[MediaItemImage]) -> AlbumsController:
    """Build an AlbumsController whose db row carries the given images."""
    controller = AlbumsController.__new__(AlbumsController)
    controller.mass = MagicMock()
    raw_metadata = serialize_to_json(MediaItemMetadata(images=UniqueList(images)))
    controller.mass.music.database.get_row = AsyncMock(return_value={"metadata": raw_metadata})
    controller.mass.music.database.update = AsyncMock()
    return controller


async def test_remove_provider_images_only_drops_given_paths() -> None:
    """Passing paths only removes the matching images of that provider."""
    images = [
        _image(PROVIDER_A, "dead.jpg"),
        _image(PROVIDER_A, "alive.jpg"),
        _image(PROVIDER_B, "dead.jpg"),
    ]
    controller = _make_controller(images)
    controller.get_library_item = AsyncMock(return_value=MagicMock(uri="album://1"))  # type: ignore[method-assign]

    await controller.remove_provider_images(1, PROVIDER_A, {"dead.jpg"})

    update_call = cast("MagicMock", controller.mass.music.database.update).call_args
    updated_metadata = MediaItemMetadata.from_dict(json_loads(update_call.args[2]["metadata"]))
    remaining_paths = {(img.provider, img.path) for img in updated_metadata.images or []}
    assert remaining_paths == {(PROVIDER_A, "alive.jpg"), (PROVIDER_B, "dead.jpg")}
    cast("MagicMock", controller.mass.signal_event).assert_called_once()


async def test_remove_provider_images_no_match_is_a_noop() -> None:
    """When none of the given paths match, nothing is updated or signaled."""
    images = [_image(PROVIDER_A, "alive.jpg")]
    controller = _make_controller(images)
    controller.get_library_item = AsyncMock()  # type: ignore[method-assign]

    await controller.remove_provider_images(1, PROVIDER_A, {"missing.jpg"})

    cast("AsyncMock", controller.mass.music.database.update).assert_not_called()
    controller.get_library_item.assert_not_called()
    cast("MagicMock", controller.mass.signal_event).assert_not_called()
