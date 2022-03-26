"""Utilities for image manipulation and retrieval."""

import os
from io import BytesIO

from music_assistant.helpers.typing import MusicAssistant
from music_assistant.music.models import MediaType
from PIL import Image


async def create_thumbnail(mass: MusicAssistant, url, size: int = 150) -> bytes:
    async with mass.http_session.get(url, verify_ssl=False) as response:
        assert response.status == 200
        img_data = BytesIO(await response.read())
        img = Image.open(img_data)
        img.thumbnail((size, size), Image.ANTIALIAS)
        img.save(format="png")
        return img_data.getvalue()


async def get_image_url(
    mass: MusicAssistant, item_id: str, provider_id: str, media_type: MediaType
):
    """Get url to image for given media item."""
    item = await mass.music.get_item(item_id, provider_id, media_type)
    if not item:
        return None
    if item and item.metadata.get("image"):
        return item.metadata["image"]
    if (
        hasattr(item, "album")
        and hasattr(item.album, "metadata")
        and item.album.metadata.get("image")
    ):
        return item.album.metadata["image"]
    if hasattr(item, "albums"):
        for album in item.albums:
            if hasattr(album, "metadata") and album.metadata.get("image"):
                return album.metadata["image"]
    if (
        hasattr(item, "artist")
        and hasattr(item.artist, "metadata")
        and item.artist.metadata.get("image")
    ):
        return item.artist.metadata["image"]
    if media_type == MediaType.TRACK and item.album:
        # try album instead for tracks
        return await get_image_url(
            mass, item.album.item_id, item.album.provider, MediaType.ALBUM
        )
    if media_type == MediaType.ALBUM and item.artist:
        # try artist instead for albums
        return await get_image_url(
            mass, item.artist.item_id, item.artist.provider, MediaType.ARTIST
        )
    return None
