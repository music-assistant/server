"""Utilities for image manipulation and retrieval."""
from __future__ import annotations

import asyncio
import random
from base64 import b64decode, b64encode
from io import BytesIO
from typing import TYPE_CHECKING

import aiofiles
from PIL import Image

from music_assistant.server.helpers.tags import get_embedded_image

if TYPE_CHECKING:
    from music_assistant.server import MusicAssistant
    from music_assistant.server.providers.filesystem_local.base import FileSystemProviderBase


async def get_image_data(mass: MusicAssistant, path_or_url: str, source: str = "http") -> bytes:
    """Create thumbnail from image url."""
    if source != "http" and (prov := mass.get_provider(source)):
        prov: FileSystemProviderBase
        file_item = await prov.resolve(path_or_url)
        # store images in cache db if file larger than 5mb and we have no direct access to the file
        use_cache = not file_item.local_path and (
            not file_item.file_size or file_item.file_size > 5000000
        )
        cache_key = f"embedded_image.{path_or_url}.{source}"
        if use_cache and (
            cache_data := await mass.cache.get(cache_key, checksum=file_item.checksum)
        ):
            return b64decode(cache_data)
        # read from file
        input_file = file_item.local_path or prov.read_file_content(file_item.absolute_path)
        if img_data := await get_embedded_image(input_file):
            if use_cache:
                await mass.cache.set(
                    cache_key, b64encode(img_data).decode(), checksum=file_item.checksum
                )
            return img_data
    # always use ffmpeg to get the image because it supports
    # both online and offline image files as well as embedded images in media files
    if img_data := await get_embedded_image(path_or_url):
        return img_data
    raise FileNotFoundError(f"Image not found: {path_or_url}")


async def get_image_thumb(
    mass: MusicAssistant, path_or_url: str, size: int | None, source: str = "http"
) -> bytes:
    """Get (optimized) PNG thumbnail from image url."""
    img_data = await get_image_data(mass, path_or_url, source)

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


async def get_icon_string(icon_path: str) -> str:
    """Get icon as (base64 encoded) string."""
    ext = icon_path.rsplit(".")[-1]
    assert ext in ("png", "svg", "ico", "jpg")
    async with aiofiles.open(icon_path, "rb") as _file:
        img_data = await _file.read()
    enc_image = b64encode(img_data).decode()
    if ext == "svg":
        return f"data:image/svg+xml;base64,{enc_image}"
    return f"data:image/{ext};base64,{enc_image}"
