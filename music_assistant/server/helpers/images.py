"""Utilities for image manipulation and retrieval."""

from __future__ import annotations

import asyncio
import itertools
import os
import random
from collections.abc import Iterable
from io import BytesIO
from typing import TYPE_CHECKING

import aiofiles
from aiohttp.client_exceptions import ClientError
from PIL import Image, UnidentifiedImageError

from music_assistant.server.helpers.tags import get_embedded_image
from music_assistant.server.models.metadata_provider import MetadataProvider

if TYPE_CHECKING:
    from music_assistant.common.models.media_items import MediaItemImage
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models.music_provider import MusicProvider


async def get_image_data(mass: MusicAssistant, path_or_url: str, provider: str) -> bytes:
    """Create thumbnail from image url."""
    # TODO: add local cache here !
    if prov := mass.get_provider(provider):
        prov: MusicProvider | MetadataProvider
        if resolved_image := await prov.resolve_image(path_or_url):
            if isinstance(resolved_image, bytes):
                return resolved_image
            if isinstance(resolved_image, str):
                path_or_url = resolved_image
    # handle HTTP location
    if path_or_url.startswith("http"):
        try:
            async with mass.http_session.get(path_or_url, raise_for_status=True) as resp:
                return await resp.read()
        except ClientError as err:
            raise FileNotFoundError from err
    # handle FILE location (of type image)
    if path_or_url.endswith(("jpg", "JPG", "png", "PNG", "jpeg")):
        if await asyncio.to_thread(os.path.isfile, path_or_url):
            async with aiofiles.open(path_or_url, "rb") as _file:
                return await _file.read()
    # use ffmpeg for embedded images
    if img_data := await get_embedded_image(path_or_url):
        return img_data
    msg = f"Image not found: {path_or_url}"
    raise FileNotFoundError(msg)


async def get_image_thumb(
    mass: MusicAssistant,
    path_or_url: str,
    size: int | None,
    provider: str,
    image_format: str = "PNG",
) -> bytes:
    """Get (optimized) PNG thumbnail from image url."""
    img_data = await get_image_data(mass, path_or_url, provider)
    if not img_data or not isinstance(img_data, bytes):
        raise FileNotFoundError(f"Image not found: {path_or_url}")

    if not size and image_format.encode() in img_data:
        return img_data

    def _create_image():
        data = BytesIO()
        try:
            img = Image.open(BytesIO(img_data))
        except UnidentifiedImageError:
            raise FileNotFoundError(f"Invalid image: {path_or_url}")
        if size:
            img.thumbnail((size, size), Image.LANCZOS)  # pylint: disable=no-member

        mode = "RGBA" if image_format == "PNG" else "RGB"
        img.convert(mode).save(data, image_format, optimize=True)
        return data.getvalue()

    image_format = image_format.upper()
    return await asyncio.to_thread(_create_image)


async def create_collage(
    mass: MusicAssistant, images: Iterable[MediaItemImage], dimensions: tuple[int] = (1500, 1500)
) -> bytes:
    """Create a basic collage image from multiple image urls."""
    image_size = 250

    def _new_collage():
        return Image.new("RGB", (dimensions[0], dimensions[1]), color=(255, 255, 255, 255))

    collage = await asyncio.to_thread(_new_collage)

    def _add_to_collage(img_data: bytes, coord_x: int, coord_y: int) -> None:
        data = BytesIO(img_data)
        photo = Image.open(data).convert("RGB")
        photo = photo.resize((image_size, image_size))
        collage.paste(photo, (coord_x, coord_y))

    # prevent duplicates with a set
    images = list(set(images))
    random.shuffle(images)
    iter_images = itertools.cycle(images)

    for x_co in range(0, dimensions[0], image_size):
        for y_co in range(0, dimensions[1], image_size):
            for _ in range(5):
                img = next(iter_images)
                img_data = await get_image_data(mass, img.path, img.provider)
                if img_data:
                    await asyncio.to_thread(_add_to_collage, img_data, x_co, y_co)
                    break

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
