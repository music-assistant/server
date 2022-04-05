"""Utilities for image manipulation and retrieval."""
from __future__ import annotations

from io import BytesIO

from music_assistant.helpers.typing import MusicAssistant
from music_assistant.models.media_items import ItemMapping, MediaItemType, MediaType
from PIL import Image


async def create_thumbnail(mass: MusicAssistant, url, size: int = 150) -> bytes:
    """Create thumbnail from image url."""
    async with mass.http_session.get(url, verify_ssl=False) as response:
        assert response.status == 200
        img_data = BytesIO(await response.read())
        img = Image.open(img_data)
        img.thumbnail((size, size), Image.ANTIALIAS)
        img.save(format="png")
        return img_data.getvalue()


async def get_image_url(mass: MusicAssistant, media_item: MediaItemType):
    """Get url to image for given media media_item."""
    if not media_item:
        return None
    if isinstance(media_item, ItemMapping):
        media_item = await mass.music.get_item_by_uri(media_item.uri)
    if media_item and media_item.metadata.get("image"):
        return media_item.metadata["image"]
    if (
        hasattr(media_item, "album")
        and hasattr(media_item.album, "metadata")
        and media_item.album.metadata.get("image")
    ):
        return media_item.album.metadata["image"]
    if hasattr(media_item, "albums"):
        for album in media_item.albums:
            if hasattr(album, "metadata") and album.metadata.get("image"):
                return album.metadata["image"]
    if (
        hasattr(media_item, "artist")
        and hasattr(media_item.artist, "metadata")
        and media_item.artist.metadata.get("image")
    ):
        return media_item.artist.metadata["image"]
    if media_item.media_type == MediaType.TRACK and media_item.album:
        # try album instead for tracks
        return await get_image_url(mass, media_item.album)
    if media_item.media_type == MediaType.ALBUM and media_item.artist:
        # try artist instead for albums
        return await get_image_url(mass, media_item.artist)
    return None
