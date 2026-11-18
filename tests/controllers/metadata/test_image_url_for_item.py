"""Tests for the image url the metadata controller picks for a media item."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.media_items import (
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    Track,
    UniqueList,
)

if TYPE_CHECKING:
    from music_assistant.controllers.metadata import MetaDataController

ALBUM_THUMB = MediaItemImage(
    type=ImageType.THUMB,
    path="http://images/album.jpg",
    provider="spotify--a",
    remotely_accessible=True,
)
TRACK_THUMB = MediaItemImage(
    type=ImageType.THUMB,
    path="http://images/track.jpg",
    provider="spotify--a",
    remotely_accessible=True,
)


def _track(album_image: MediaItemImage | None) -> Track:
    """Build a library track with its own thumb, on an album with the given image."""
    return Track(
        item_id="1",
        provider="library",
        name="Track",
        provider_mappings=set(),
        album=ItemMapping(
            media_type=MediaType.ALBUM,
            item_id="2",
            provider="library",
            name="Album",
            image=album_image,
        ),
        metadata=MediaItemMetadata(images=UniqueList([TRACK_THUMB])),
    )


async def test_track_prefers_its_album_image(metadata_controller: MetaDataController) -> None:
    """A track shows the image of its album over its own image."""
    assert await metadata_controller.get_image_url_for_item(_track(ALBUM_THUMB)) == (
        ALBUM_THUMB.path
    )


async def test_track_without_album_image_shows_its_own_image(
    metadata_controller: MetaDataController,
) -> None:
    """A track on an album without an image shows its own image."""
    assert await metadata_controller.get_image_url_for_item(_track(None)) == TRACK_THUMB.path
