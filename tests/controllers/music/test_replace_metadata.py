"""Tests for replacing metadata values and provider images stored for a library item."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import ArtistEntityType, ImageType
from music_assistant_models.media_items import MediaItemImage, MediaItemMetadata
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.music.media.artists import ArtistsController
from music_assistant.helpers.json import json_loads, serialize_to_json


def _make_controller(metadata: MediaItemMetadata) -> ArtistsController:
    """Build an ArtistsController whose db row carries the given metadata."""
    controller = ArtistsController.__new__(ArtistsController)
    controller.mass = MagicMock()
    raw_metadata = serialize_to_json(metadata)
    controller.mass.music.database.get_row = AsyncMock(return_value={"metadata": raw_metadata})
    controller.mass.music.database.update = AsyncMock()
    controller.get_library_item = AsyncMock(return_value=MagicMock(uri="artist://1"))  # type: ignore[method-assign]
    return controller


def _stored(controller: ArtistsController) -> MediaItemMetadata:
    """Return the metadata the controller wrote to the database."""
    update_call = cast("MagicMock", controller.mass.music.database.update).call_args
    return MediaItemMetadata.from_dict(json_loads(update_call.args[2]["metadata"]))


def _image(path: str, provider: str) -> MediaItemImage:
    """Return a thumb image of a provider."""
    return MediaItemImage(type=ImageType.THUMB, path=path, provider=provider)


async def test_replace_metadata_overwrites_values_and_provider_images() -> None:
    """Given values and one provider's images replace the stored ones; the rest stays."""
    controller = _make_controller(
        MediaItemMetadata(
            description="Wikipedia bio",
            description_language="en",
            artist_entity_type=ArtistEntityType.PERSON,
            genres={"Rock"},
            images=UniqueList([_image("old.jpg", "local_1"), _image("online.jpg", "fanarttv")]),
        )
    )

    await controller.replace_metadata(
        1,
        {
            "description": "Own bio",
            "description_language": "en",
            "artist_entity_type": ArtistEntityType.CHARACTER,
        },
        {"local_1": [_image("new.jpg", "local_1")]},
    )

    stored = _stored(controller)
    assert (stored.description, stored.description_language) == ("Own bio", "en")
    assert stored.artist_entity_type == ArtistEntityType.CHARACTER
    assert stored.genres == {"Rock"}
    assert stored.images == [_image("online.jpg", "fanarttv"), _image("new.jpg", "local_1")]
    cast("MagicMock", controller.mass.signal_event).assert_called_once()


async def test_replace_metadata_with_no_images_removes_the_provider_images() -> None:
    """An empty image list removes the provider's stored images."""
    controller = _make_controller(
        MediaItemMetadata(images=UniqueList([_image("old.jpg", "local_1")]))
    )

    await controller.replace_metadata(1, provider_images={"local_1": []})

    assert _stored(controller).images is None


async def test_replace_metadata_with_the_stored_values_is_a_noop() -> None:
    """Unchanged values and images write and signal nothing."""
    controller = _make_controller(
        MediaItemMetadata(
            description="Own bio",
            description_language="en",
            images=UniqueList([_image("own.jpg", "local_1"), _image("online.jpg", "fanarttv")]),
        )
    )

    await controller.replace_metadata(
        1,
        {"description": "Own bio", "description_language": "en"},
        {"local_1": [_image("own.jpg", "local_1")]},
    )

    cast("AsyncMock", controller.mass.music.database.update).assert_not_called()
    cast("MagicMock", controller.mass.signal_event).assert_not_called()
