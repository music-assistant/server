"""Utilities for image manipulation and retrieval."""
from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING, Optional

from PIL import Image
from tinytag import TinyTag

from music_assistant.models.enums import ImageType, MediaType
from music_assistant.models.media_items import ItemMapping, MediaItemType

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


async def create_thumbnail(
    mass: MusicAssistant, path: str, size: Optional[int]
) -> bytes:
    """Create thumbnail from image url."""
    if not size:
        size = 200
    img_data = None
    if path.startswith("http"):
        async with mass.http_session.get(path, verify_ssl=False) as response:
            assert response.status == 200
            img_data = await response.read()
    else:
        # assume file from file provider, we need to fetch it here...
        for prov in mass.music.providers:
            if not prov.type.is_file():
                continue
            if not prov.exists(path):
                continue
            if TinyTag.is_supported(path):
                # embedded image in music file
                def get_embedded_image():
                    tags = TinyTag.get(path, image=True)
                    return tags.get_image()

                img_data = await mass.loop.run_in_executor(None, get_embedded_image)
            else:
                # regular image file on disk
                async with prov.open_file(path) as _file:
                    img_data = BytesIO(await _file.read())
            break
    if not img_data:
        raise FileNotFoundError(f"Image not found: {path}")

    def _create_image():
        data = BytesIO(img_data)
        img = Image.open(data)
        img.thumbnail((size, size), Image.ANTIALIAS)
        img.save(data, format="png")
        return data.getvalue()

    return await mass.loop.run_in_executor(None, _create_image)


async def get_image_url(
    mass: MusicAssistant,
    media_item: MediaItemType,
    img_type: ImageType = ImageType.THUMB,
) -> str:
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
