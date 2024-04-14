"""Utilities for image manipulation and retrieval."""

from __future__ import annotations

import asyncio
import itertools
import random
from collections.abc import Iterable
from io import BytesIO
from typing import TYPE_CHECKING

import aiofiles
from PIL import Image

from music_assistant.server.helpers.tags import get_embedded_image
from music_assistant.server.models.metadata_provider import MetadataProvider

if TYPE_CHECKING:
    from music_assistant.common.models.media_items import MediaItemImage
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models.music_provider import MusicProvider


async def get_image_data(mass: MusicAssistant, path_or_url: str, provider: str = "url") -> bytes:
    """Create thumbnail from image url."""
    if prov := mass.get_provider(provider):
        prov: MusicProvider | MetadataProvider
        if resolved_data := await prov.resolve_image(path_or_url):
            if isinstance(resolved_data, bytes):
                return resolved_data
            return await get_embedded_image(resolved_data)
    # always use ffmpeg to get the image because it supports
    # both online and offline image files as well as embedded images in media files
    if img_data := await get_embedded_image(path_or_url):
        return img_data
    msg = f"Image not found: {path_or_url}"
    raise FileNotFoundError(msg)


async def get_image_thumb(
    mass: MusicAssistant,
    path_or_url: str,
    size: int | None,
    provider: str = "url",
    image_format: str = "PNG",
) -> bytes:
    """Get (optimized) PNG thumbnail from image url."""
    img_data = await get_image_data(mass, path_or_url, provider)

    def _create_image():
        data = BytesIO()
        img = Image.open(BytesIO(img_data))
        if size:
            img.thumbnail((size, size), Image.LANCZOS)  # pylint: disable=no-member
        img.convert("RGB").save(data, image_format, optimize=True)
        return data.getvalue()

    return await asyncio.to_thread(_create_image)


async def create_collage(
    mass: MusicAssistant, images: Iterable[MediaItemImage], dimensions: tuple[int] = (1500, 1500)
) -> bytes:
    """Create a basic collage image from multiple image urls."""
    image_size = 250

    def _new_collage():
        return Image.new("RGBA", (dimensions[0], dimensions[1]), color=(255, 255, 255, 255))

    collage = await asyncio.to_thread(_new_collage)

    def _add_to_collage(img_data: bytes, coord_x: int, coord_y: int) -> None:
        data = BytesIO(img_data)
        photo = Image.open(data).convert("RGBA")
        photo = photo.resize((image_size, image_size))
        collage.paste(photo, (coord_x, coord_y))

    # prevent duplicates with a set
    images = list(set(images))
    random.shuffle(images)
    iter_images = itertools.cycle(images)

    for x_co in range(0, dimensions[0], image_size):
        for y_co in range(0, dimensions[1], image_size):
            img = next(iter_images)
            img_data = await get_image_data(mass, img.path, img.provider)
            await asyncio.to_thread(_add_to_collage, img_data, x_co, y_co)

    def _save_collage():
        final_data = BytesIO()
        collage.convert("RGB").save(final_data, "JPEG", optimize=True)
        return final_data.getvalue()

    return await asyncio.to_thread(_save_collage)


async def get_icon_string(icon_path: str) -> str:
    """Get svg icon as string."""
    ext = icon_path.rsplit(".")[-1]
    assert ext == "svg"
    async with aiofiles.open(icon_path, "r") as _file:
        xml_data = await _file.read()
        return xml_data.replace("\n", "").strip()
