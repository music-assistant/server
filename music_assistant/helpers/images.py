"""Utilities for image manipulation and retrieval."""

from __future__ import annotations

import asyncio
import itertools
import os
import random
from base64 import b64decode
from collections.abc import Iterable
from io import BytesIO
from typing import TYPE_CHECKING, cast

import aiofiles
from aiohttp.client_exceptions import ClientError
from PIL import Image, UnidentifiedImageError

from music_assistant.helpers.tags import get_embedded_image
from music_assistant.models.metadata_provider import MetadataProvider
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemImage
    from PIL.Image import Image as ImageClass

    from music_assistant.mass import MusicAssistant


def _is_safe_local_image_path(path_or_url: str) -> bool:
    """Return True if the given path is considered safe to open as a local image file.

    This performs basic normalization and traversal checks to avoid accessing
    unexpected locations when the path originates from untrusted input.
    """
    # URLs and data URIs are handled elsewhere
    if path_or_url.startswith(("http://", "https://", "data:image")):
        return False

    # Normalize the path and reject any traversal components
    norm_path = os.path.normpath(path_or_url)
    if norm_path.startswith("..") or "/../" in norm_path or "\\..\\" in norm_path:
        return False

    # Resolve to an absolute real path and ensure it is within the current working directory
    real_path = os.path.realpath(norm_path)
    cwd = os.path.realpath(os.getcwd())
    try:
        common = os.path.commonpath([real_path, cwd])
    except ValueError:
        # Different drives on Windows or other path issues
        return False
    if common != cwd:
        return False

    return True


async def get_image_data(mass: MusicAssistant, path_or_url: str, provider: str) -> bytes:
    """Create thumbnail from image url."""
    # TODO: add local cache here !
    if prov := mass.get_provider(provider):
        assert isinstance(prov, MusicProvider | MetadataProvider | PluginProvider)
        if resolved_image := await prov.resolve_image(path_or_url):
            if isinstance(resolved_image, bytes):
                return resolved_image
            if isinstance(resolved_image, str):
                path_or_url = resolved_image
    # handle HTTP location
    if path_or_url.startswith("http"):
        try:
            async with mass.http_session_no_ssl.get(path_or_url, raise_for_status=True) as resp:
                return await resp.read()
        except ClientError as err:
            raise FileNotFoundError from err
    # handle base64 embedded images
    if path_or_url.startswith("data:image"):
        return b64decode(path_or_url.split(",")[-1])
    # handle FILE location (of type image)
    if path_or_url.endswith(("jpg", "JPG", "png", "PNG", "jpeg")) and _is_safe_local_image_path(
        path_or_url
    ):
        if await asyncio.to_thread(os.path.isfile, path_or_url):
            async with aiofiles.open(path_or_url, "rb") as _file:
                return cast("bytes", await _file.read())
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

    image_format = image_format.upper()
    if image_format == "JPG":
        image_format = "JPEG"

    def _create_image() -> bytes:
        data = BytesIO()
        try:
            img = Image.open(BytesIO(img_data))
        except UnidentifiedImageError:
            raise FileNotFoundError(f"Invalid image: {path_or_url}")
        if size:
            # Use LANCZOS for high quality downsampling
            img.thumbnail((size, size), Image.Resampling.LANCZOS)

        mode = "RGBA" if image_format == "PNG" else "RGB"

        # Save with high quality settings
        if image_format == "JPEG":
            # For JPEG, use quality=95 for better quality
            img.convert(mode).save(data, image_format, quality=95, optimize=False)
        else:
            # For PNG, disable optimize to preserve quality
            img.convert(mode).save(data, image_format, optimize=False)
        return data.getvalue()

    image_format = image_format.upper()
    return await asyncio.to_thread(_create_image)


async def create_collage(
    mass: MusicAssistant,
    images: Iterable[MediaItemImage],
    dimensions: tuple[int, int] = (1500, 1500),
) -> bytes:
    """Create a basic collage image from multiple image urls."""
    image_size = 250

    def _new_collage() -> ImageClass:
        return Image.new("RGB", (dimensions[0], dimensions[1]), color=(255, 255, 255, 255))

    collage = await asyncio.to_thread(_new_collage)

    def _add_to_collage(img_data: bytes, coord_x: int, coord_y: int) -> None:
        data = BytesIO(img_data)
        photo = Image.open(data).convert("RGB")
        photo = photo.resize((image_size, image_size))
        collage.paste(photo, (coord_x, coord_y))
        del data

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
                    del img_data
                    break

    def _save_collage() -> bytes:
        final_data = BytesIO()
        collage.convert("RGB").save(final_data, "JPEG", optimize=True)
        return final_data.getvalue()

    return await asyncio.to_thread(_save_collage)


async def get_icon_string(icon_path: str) -> str:
    """Get svg icon as string."""
    ext = icon_path.rsplit(".")[-1]
    assert ext == "svg"
    async with aiofiles.open(icon_path) as _file:
        xml_data = await _file.read()
        assert isinstance(xml_data, str)  # for type checking
        return xml_data.replace("\n", "").strip()
