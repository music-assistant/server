"""Utilities for image manipulation and retrieval."""
from __future__ import annotations

from io import BytesIO

from PIL import Image

from music_assistant.helpers.typing import MusicAssistant
from music_assistant.models.media_items import (
    ImageType,
    ItemMapping,
    MediaItemType,
    MediaType,
)


async def create_thumbnail(mass: MusicAssistant, url, size: int = 150) -> bytes:
    """Create thumbnail from image url."""
    async with mass.http_session.get(url, verify_ssl=False) as response:
        assert response.status == 200
        img_data = BytesIO(await response.read())
        img = Image.open(img_data)
        img.thumbnail((size, size), Image.ANTIALIAS)
        img.save(format="png")
        return img_data.getvalue()


async def get_image_url(
    mass: MusicAssistant,
    media_item: MediaItemType,
    img_type: ImageType = ImageType.THUMB,
):
    """Get url to image for given media media_item."""
    if not media_item:
        return None
    if isinstance(media_item, ItemMapping):
        media_item = await mass.music.get_item_by_uri(media_item.uri)
    if media_item and media_item.metadata.images:
        for img in media_item.metadata.images:
            if img.type == img_type:
                return img.url

    # retry with track's album
    if media_item.media_type == MediaType.TRACK and media_item.album:
        return await get_image_url(mass, media_item.album, img_type)

    # try artist instead for albums
    if media_item.media_type == MediaType.ALBUM and media_item.artist:
        return await get_image_url(mass, media_item.artist, img_type)

    # last resort: track artist(s)
    if media_item.media_type == MediaType.TRACK and media_item.artists:
        for artist in media_item.artists:
            return await get_image_url(mass, artist, img_type)

    return None
