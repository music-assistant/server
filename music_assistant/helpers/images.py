"""Utilities for image manipulation and retrieval."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import os
import random
import time
import weakref
from base64 import b64decode
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
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


_IMAGE_CACHE_TTL_SECONDS = 900
_IMAGE_CACHE_MAX_ENTRIES = 256
_MAX_CONCURRENT_EMBEDDED_IMAGE_EXTRACTIONS = 2


@dataclass(slots=True)
class _ImageHelperState:
    """Runtime state for image helper cache/throttling."""

    cache: OrderedDict[str, tuple[float, bytes]] = field(default_factory=OrderedDict)
    embedded_image_semaphore: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(_MAX_CONCURRENT_EMBEDDED_IMAGE_EXTRACTIONS)
    )


_IMAGE_HELPER_STATES: weakref.WeakKeyDictionary[MusicAssistant, _ImageHelperState] = (
    weakref.WeakKeyDictionary()
)


def _get_helper_state(mass: MusicAssistant) -> _ImageHelperState:
    """Return/create helper state for this Music Assistant instance."""
    if state := _IMAGE_HELPER_STATES.get(mass):
        return state
    state = _ImageHelperState()
    _IMAGE_HELPER_STATES[mass] = state
    return state


def _create_cache_key(provider: str, path_or_url: str) -> str:
    """Create deterministic cache key for image requests."""
    return f"{provider}.{path_or_url}"


def _create_task_id(provider: str, path_or_url: str) -> str:
    """Create compact task_id for in-flight request de-duplication."""
    cache_key = _create_cache_key(provider, path_or_url)
    cache_key_hash = hashlib.md5(cache_key.encode(), usedforsecurity=False).hexdigest()
    return f"image_data.{cache_key_hash}"


def _get_cached_image(state: _ImageHelperState, cache_key: str) -> bytes | None:
    """Get image bytes from local memory cache."""
    cache_entry = state.cache.get(cache_key)
    if cache_entry is None:
        return None
    expires_at, image_data = cache_entry
    if expires_at <= time.monotonic():
        state.cache.pop(cache_key, None)
        return None
    state.cache.move_to_end(cache_key)
    return image_data


def _set_cached_image(state: _ImageHelperState, cache_key: str, image_data: bytes) -> None:
    """Store image bytes in local memory cache."""
    state.cache[cache_key] = (time.monotonic() + _IMAGE_CACHE_TTL_SECONDS, image_data)
    state.cache.move_to_end(cache_key)
    while len(state.cache) > _IMAGE_CACHE_MAX_ENTRIES:
        state.cache.popitem(last=False)


async def get_image_data(mass: MusicAssistant, path_or_url: str, provider: str) -> bytes:
    """Create thumbnail from image url."""
    state = _get_helper_state(mass)
    cache_key = _create_cache_key(provider, path_or_url)
    if (cached_image := _get_cached_image(state, cache_key)) is not None:
        return cached_image
    task: asyncio.Task[bytes] = mass.create_task(
        _get_image_data_uncached,
        mass,
        path_or_url,
        provider,
        task_id=_create_task_id(provider, path_or_url),
        abort_existing=False,
    )
    image_data = await asyncio.shield(task)
    _set_cached_image(state, cache_key, image_data)
    return image_data


async def _get_image_data_uncached(mass: MusicAssistant, path_or_url: str, provider: str) -> bytes:
    """Fetch image bytes without using local cache."""
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
    if path_or_url.endswith(("jpg", "JPG", "png", "PNG", "jpeg")) and is_safe_path(path_or_url):
        if await asyncio.to_thread(os.path.isfile, path_or_url):
            async with aiofiles.open(path_or_url, "rb") as _file:
                return cast("bytes", await _file.read())
    # use ffmpeg for embedded images
    if is_safe_path(path_or_url):
        state = _get_helper_state(mass)
        async with state.embedded_image_semaphore:
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
