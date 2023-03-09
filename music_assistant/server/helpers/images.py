"""Utilities for image manipulation and retrieval."""
from __future__ import annotations

import asyncio
import random
from io import BytesIO
from typing import TYPE_CHECKING

from PIL import Image

from music_assistant.server.helpers.tags import get_embedded_image

if TYPE_CHECKING:
    from music_assistant.server import MusicAssistant


async def get_image_data(mass: MusicAssistant, path: str) -> bytes:
    """Create thumbnail from image url."""
    # always try ffmpeg first to get the image because it supports
    # both online and offline image files as well as embedded images in media files
    img_data = await get_embedded_image(path)
    if img_data:
        return img_data
    # assume file from file provider, we need to fetch it here...
    for prov in mass.music.providers:
        if not prov.domain.startswith("filesystem"):
            continue
        if not await prov.exists(path):
            continue
        img_data = await get_embedded_image(prov.read_file_content(path))
        if img_data:
            return img_data
    raise FileNotFoundError(f"Image not found: {path}")


async def get_image_thumb(mass: MusicAssistant, path: str, size: int | None) -> bytes:
    """Get (optimized) PNG thumbnail from image url."""
    img_data = await get_image_data(mass, path)

    def _create_image():
        data = BytesIO()
        img = Image.open(BytesIO(img_data))
        if size:
            img.thumbnail((size, size), Image.ANTIALIAS)
        img.convert("RGB").save(data, "PNG", optimize=True)
        return data.getvalue()

    return await asyncio.to_thread(_create_image)


async def create_collage(mass: MusicAssistant, images: list[str]) -> bytes:
    """Create a basic collage image from multiple image urls."""

    def _new_collage():
        return Image.new("RGBA", (1500, 1500), color=(255, 255, 255, 255))

    collage = await asyncio.to_thread(_new_collage)

    def _add_to_collage(img_data: bytes, coord_x: int, coord_y: int):
        data = BytesIO(img_data)
        photo = Image.open(data).convert("RGBA")
        photo = photo.resize((500, 500))
        collage.paste(photo, (coord_x, coord_y))

    for x_co in range(0, 1500, 500):
        for y_co in range(0, 1500, 500):
            img_data = await get_image_data(mass, random.choice(images))
            await asyncio.to_thread(_add_to_collage, img_data, x_co, y_co)

    def _save_collage():
        final_data = BytesIO()
        collage.convert("RGB").save(final_data, "PNG", optimize=True)
        return final_data.getvalue()

    return await asyncio.to_thread(_save_collage)
