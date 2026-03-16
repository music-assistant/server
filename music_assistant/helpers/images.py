"""Utilities for image manipulation and retrieval."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import os
import random
from base64 import b64decode
from collections import OrderedDict
from collections.abc import Iterable
from io import BytesIO
from typing import TYPE_CHECKING, cast

import aiofiles
from aiohttp.client_exceptions import ClientError
from PIL import Image, UnidentifiedImageError

from music_assistant.helpers.security import is_safe_path
from music_assistant.helpers.tags import get_embedded_image
from music_assistant.models.metadata_provider import MetadataProvider
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemImage
    from PIL.Image import Image as ImageClass

    from music_assistant.mass import MusicAssistant


# Thumbnail cache: on-disk (persistent) + small in-memory FIFO (hot path)
_THUMB_CACHE_DIR = "thumbnails"
_THUMB_MEMORY_CACHE_MAX = 50

_thumb_memory_cache: OrderedDict[str, bytes] = OrderedDict()


def _create_thumb_hash(provider: str, path_or_url: str) -> str:
    """Create a safe filesystem hash from provider and image path."""
    raw = f"{provider}/{path_or_url}"
    return hashlib.sha256(raw.encode(), usedforsecurity=False).hexdigest()


def _thumb_cache_filename(thumb_hash: str, size: int | None, image_format: str) -> str:
    """Build the cache filename for a thumbnail."""
    ext = image_format.lower()
    if ext == "jpeg":
        ext = "jpg"
    return f"{thumb_hash}_{size or 0}.{ext}"


def _get_from_memory_cache(key: str) -> bytes | None:
    """Retrieve thumbnail from in-memory FIFO cache."""
    if key in _thumb_memory_cache:
        _thumb_memory_cache.move_to_end(key)
        return _thumb_memory_cache[key]
    return None


def _put_in_memory_cache(key: str, data: bytes) -> None:
    """Store thumbnail in in-memory FIFO cache."""
    _thumb_memory_cache[key] = data
    _thumb_memory_cache.move_to_end(key)
    while len(_thumb_memory_cache) > _THUMB_MEMORY_CACHE_MAX:
        _thumb_memory_cache.popitem(last=False)


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
    if path_or_url.endswith(("jpg", "JPG", "png", "PNG", "jpeg", "svg", "SVG")) and is_safe_path(
        path_or_url
    ):
        if await asyncio.to_thread(os.path.isfile, path_or_url):
            async with aiofiles.open(path_or_url, "rb") as _file:
                return cast("bytes", await _file.read())
    # use ffmpeg for embedded images
    if is_safe_path(path_or_url) and (img_data := await get_embedded_image(path_or_url)):
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
    """Get (optimized) thumbnail from image url.

    Uses a two-tier cache (in-memory FIFO + on-disk) keyed by a hash of
    provider + path so that repeated requests never trigger ffmpeg or
    PIL processing again.  Concurrent requests for the same thumbnail
    are de-duplicated via create_task.

    :param mass: The MusicAssistant instance.
    :param path_or_url: Path or URL to the source image.
    :param size: Target thumbnail size (square), or None for original.
    :param provider: Provider identifier for the image source.
    :param image_format: Output format (PNG or JPEG/JPG).
    """
    image_format = image_format.upper()
    if image_format == "JPG":
        image_format = "JPEG"

    thumb_hash = _create_thumb_hash(provider, path_or_url)
    cache_filename = _thumb_cache_filename(thumb_hash, size, image_format)

    # 1. Check in-memory FIFO cache
    if cached := _get_from_memory_cache(cache_filename):
        return cached

    # 2. Check on-disk cache
    thumb_dir = os.path.join(mass.cache_path, _THUMB_CACHE_DIR)
    cache_filepath = os.path.join(thumb_dir, cache_filename)
    if await asyncio.to_thread(os.path.isfile, cache_filepath):
        async with aiofiles.open(cache_filepath, "rb") as f:
            thumb_data = cast("bytes", await f.read())
        _put_in_memory_cache(cache_filename, thumb_data)
        return thumb_data

    # 3. Generate thumbnail (de-duplicated across concurrent requests)
    task: asyncio.Task[bytes] = mass.create_task(
        _generate_and_cache_thumb,
        mass,
        path_or_url,
        size,
        provider,
        image_format,
        cache_filepath,
        task_id=f"thumb.{cache_filename}",
        abort_existing=False,
    )
    thumb_data = await asyncio.shield(task)
    _put_in_memory_cache(cache_filename, thumb_data)
    return thumb_data


async def _generate_and_cache_thumb(
    mass: MusicAssistant,
    path_or_url: str,
    size: int | None,
    provider: str,
    image_format: str,
    cache_filepath: str,
) -> bytes:
    """Generate a thumbnail, persist it on disk, and return the bytes.

    :param mass: The MusicAssistant instance.
    :param path_or_url: Path or URL to the source image.
    :param size: Target thumbnail size (square), or None for original.
    :param provider: Provider identifier for the image source.
    :param image_format: Normalized output format (PNG or JPEG).
    :param cache_filepath: Absolute path where the thumbnail will be stored.
    """
    img_data = await get_image_data(mass, path_or_url, provider)
    if not img_data or not isinstance(img_data, bytes):
        raise FileNotFoundError(f"Image not found: {path_or_url}")

    if not size and image_format.encode() in img_data:
        thumb_data = img_data
    else:

        def _create_image() -> bytes:
            data = BytesIO()
            try:
                img = Image.open(BytesIO(img_data))
            except UnidentifiedImageError:
                raise FileNotFoundError(f"Invalid image: {path_or_url}")
            if size:
                img.thumbnail((size, size), Image.Resampling.LANCZOS)
            mode = "RGBA" if image_format == "PNG" else "RGB"
            if image_format == "JPEG":
                img.convert(mode).save(data, image_format, quality=95, optimize=False)
            else:
                img.convert(mode).save(data, image_format, optimize=False)
            return data.getvalue()

        thumb_data = await asyncio.to_thread(_create_image)

    # Persist to disk cache (best-effort, don't fail on I/O errors)
    try:
        await asyncio.to_thread(os.makedirs, os.path.dirname(cache_filepath), exist_ok=True)
        async with aiofiles.open(cache_filepath, "wb") as f:
            await f.write(thumb_data)
    except OSError:
        pass

    return thumb_data


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
