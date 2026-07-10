"""Utilities for image manipulation and retrieval."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import os
import random
import re
import urllib.parse
from base64 import b64decode
from collections import OrderedDict
from collections.abc import Iterable
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, cast

import aiofiles
from aiohttp.client_exceptions import ClientError

from music_assistant.constants import APPLICATION_NAME
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
_ALLOWED_THUMB_FORMATS: frozenset[str] = frozenset({"PNG", "JPEG"})

# Bump on encoding-rule changes so stale entries (e.g. old black-bg JPEGs for
# transparent logos) aren't served from a colliding filename after upgrade.
_THUMB_CACHE_VERSION = 2

# By construction the filename is `<sha256>_<int>_v<int>[_flat].(jpg|png)`; the
# regex is an explicit sanitizer that also lets CodeQL prove the value is safe
# to join into a filesystem path. The `_flat` marker separates the flattened
# and transparency-preserving cache variants.
_THUMB_FILENAME_RE = re.compile(r"^[0-9a-f]{64}_\d+_v\d+(?:_flat)?\.(?:jpg|png)$")

_thumb_memory_cache: OrderedDict[str, bytes] = OrderedDict()

_MAX_IMAGEPROXY_RECURSION_DEPTH = 5

# Leading magic bytes used to sniff raster image formats from their content.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


def is_svg_data(data: bytes) -> bool:
    """Return True when the given bytes appear to be an SVG image."""
    if not data:
        return False
    # the root <svg> may be preceded by an xml declaration, doctype or comment
    sample = data[:1024].lstrip()
    if not sample[:64].lower().startswith((b"<?xml", b"<svg", b"<!--", b"<!doctype")):
        return False
    return b"<svg" in sample.lower()


def detect_image_content_format(data: bytes) -> str | None:
    """
    Return the sniffed image format (`png`, `jpg` or `svg`), or None if unknown.

    :param data: Raw image bytes to inspect.
    """
    if not data:
        return None
    if data.startswith(_PNG_MAGIC):
        return "png"
    if data.startswith(_JPEG_MAGIC):
        return "jpg"
    if is_svg_data(data):
        return "svg"
    return None


def create_thumb_hash(provider: str, path_or_url: str) -> str:
    """Create a safe filesystem hash from provider and image path."""
    raw = f"{provider}/{path_or_url}"
    return hashlib.sha256(raw.encode(), usedforsecurity=False).hexdigest()


def _thumb_cache_filename(
    thumb_hash: str, size: int | None, image_format: str, flatten_transparency: bool = False
) -> str:
    """Build the cache filename for a thumbnail."""
    ext = image_format.lower()
    if ext == "jpeg":
        ext = "jpg"
    suffix = "_flat" if flatten_transparency else ""
    return f"{thumb_hash}_{size or 0}_v{_THUMB_CACHE_VERSION}{suffix}.{ext}"


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


def _has_alpha(img: ImageClass) -> bool:
    """Return True if the image actually uses transparency."""
    if img.mode == "P":
        return "transparency" in img.info
    if img.mode in ("RGBA", "LA", "PA"):
        # an alpha channel may still be fully opaque; only treat it as
        # transparent when some pixel is not fully opaque
        # single-band getextrema() returns a (min, max) numeric tuple
        return cast("tuple[float, float]", img.getchannel("A").getextrema())[0] < 255
    return False


_IMAGEPROXY_V2_PREFIX = "/imageproxy/"


def _extract_imageproxy_id(url: str) -> str | None:
    """
    Return the 64-hex image_id from a /imageproxy/<id> URL, or None.

    The path must match the canonical shape `/imageproxy/<id>` (optionally
    with a single trailing slash) — extra segments are rejected so this
    helper agrees with what `MetaDataController.handle_imageproxy` accepts.
    """
    # bail out early on anything that obviously can't be a v2 imageproxy URL
    # (non-strings such as MagicMock from tests; urlparse would TypeError)
    if not isinstance(url, str) or _IMAGEPROXY_V2_PREFIX not in url:
        return None
    parsed = urllib.parse.urlparse(url)
    if not parsed.path.startswith(_IMAGEPROXY_V2_PREFIX):
        return None
    remainder = parsed.path[len(_IMAGEPROXY_V2_PREFIX) :].rstrip("/").lower()
    if len(remainder) != 64 or any(c not in "0123456789abcdef" for c in remainder):
        return None
    return remainder


def player_image_url(mass: MusicAssistant, url: str | None) -> str | None:
    """
    Rewrite an imageproxy URL for consumption by a (physical) player.

    :param mass: The MusicAssistant instance.
    :param url: Image URL as produced for frontend/API consumers.
    """
    if not url:
        return url
    webserver_base = mass.webserver.base_url
    if webserver_base and url.startswith(f"{webserver_base}/imageproxy"):
        # players may not be able to reach the webserver, so serve from the streams
        # server, and force jpeg (= flatten transparency) as players such as legacy
        # AirPlay receivers cannot be assumed to handle PNG alpha
        url = mass.streams.base_url + url[len(webserver_base) :]
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        query["fmt"] = ["jpeg"]
        return parsed._replace(query=urllib.parse.urlencode(query, doseq=True)).geturl()
    return url


async def get_image_data(
    mass: MusicAssistant, path_or_url: str, provider: str, *, _depth: int = 0
) -> bytes:
    """
    Retrieve image data from a path or URL.

    :param mass: The MusicAssistant instance.
    :param path_or_url: The image path, URL, or base64 data URI.
    :param provider: The provider ID that can resolve the image.
    :param _depth: Internal recursion depth counter (do not set manually).
    """
    if _depth >= _MAX_IMAGEPROXY_RECURSION_DEPTH:
        msg = f"Maximum recursion depth exceeded when fetching image: {path_or_url}"
        raise FileNotFoundError(msg)
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
        # Handle imageproxy URLs pointing to our own server
        parsed_url = urllib.parse.urlparse(path_or_url)
        url_origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
        server_origins = {
            f"{p.scheme}://{p.netloc}"
            for b in (mass.webserver.base_url, mass.streams.base_url)
            if (p := urllib.parse.urlparse(b)).netloc
        }
        if url_origin in server_origins:
            # opaque-id form: /imageproxy/<image_id>?size=...&fmt=...
            if image_id := _extract_imageproxy_id(path_or_url):
                resolved = await mass.metadata.resolve_image_id(image_id)
                if resolved is None:
                    msg = f"Unknown image id in URL: {path_or_url}"
                    raise FileNotFoundError(msg)
                extracted_provider, extracted_path = resolved
                return await get_image_data(
                    mass, extracted_path, extracted_provider, _depth=_depth + 1
                )
            msg = f"Invalid imageproxy URL: {path_or_url}"
            raise FileNotFoundError(msg)
        try:
            return await _fetch_remote_image(mass, path_or_url)
        except ClientError as err:
            msg = f"Failed to fetch image from {path_or_url}: {err}"
            raise FileNotFoundError(msg) from err
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


async def _fetch_remote_image(mass: MusicAssistant, url: str) -> bytes:
    """
    Fetch raw image bytes over HTTP.

    :param mass: The MusicAssistant instance.
    :param url: The (http/https) image URL to fetch.
    """
    # Bot-protected CDNs (e.g. Akamai) reject our normal self-identifying User-Agent,
    # and even regular browser User-Agents, while still serving well-known fetch tools.
    # We keep identifying as Music Assistant but carry a Wget compatibility token, which
    # such CDNs allowlist, so artwork is served on the first (and only) request.
    user_agent = f"{APPLICATION_NAME}/{mass.version} (Wget/1.24.5; +https://music-assistant.io)"
    async with mass.http_session_no_ssl.get(
        url, raise_for_status=True, headers={"User-Agent": user_agent}
    ) as resp:
        return await resp.read()


async def get_image_thumb(
    mass: MusicAssistant,
    path_or_url: str,
    size: int | None,
    provider: str,
    image_format: str = "PNG",
    flatten_transparency: bool = False,
) -> bytes:
    """
    Get (optimized) thumbnail from image url.

    Uses a two-tier cache (in-memory FIFO + on-disk) keyed by a hash of
    provider + path so that repeated requests never trigger ffmpeg or
    PIL processing again.  Concurrent requests for the same thumbnail
    are de-duplicated via create_task.

    :param mass: The MusicAssistant instance.
    :param path_or_url: Path or URL to the source image.
    :param size: Target thumbnail size (square), or None for original.
    :param provider: Provider identifier for the image source.
    :param image_format: Output format (PNG or JPEG/JPG).
    :param flatten_transparency: When True, alpha is composited onto white and
        kept as JPEG; when False, transparent sources are emitted as PNG.
    """
    image_format = image_format.upper()
    if image_format == "JPG":
        image_format = "JPEG"
    if image_format not in _ALLOWED_THUMB_FORMATS:
        msg = f"Unsupported thumbnail format: {image_format}"
        raise ValueError(msg)

    thumb_hash = create_thumb_hash(provider, path_or_url)
    cache_filename = _thumb_cache_filename(thumb_hash, size, image_format, flatten_transparency)
    if not _THUMB_FILENAME_RE.fullmatch(cache_filename):
        # cache_filename is built from a sha256 + int + fixed extension, so this
        # is unreachable in practice — it is here so a future change to either
        # builder cannot silently let an unsafe value reach the filesystem path
        msg = f"Refusing to use unexpected cache filename: {cache_filename!r}"
        raise OSError(msg)

    # 1. Check in-memory FIFO cache
    if cached := _get_from_memory_cache(cache_filename):
        return cached

    # 2. Check on-disk cache
    thumb_dir_resolved = os.path.realpath(os.path.join(mass.cache_path, _THUMB_CACHE_DIR))
    cache_filepath = os.path.realpath(os.path.join(thumb_dir_resolved, cache_filename))
    if not cache_filepath.startswith(thumb_dir_resolved + os.sep):
        msg = f"Cache path escapes thumbnail directory: {cache_filepath}"
        raise OSError(msg)
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
        flatten_transparency,
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
    flatten_transparency: bool = False,
) -> bytes:
    """
    Generate a thumbnail, persist it on disk, and return the bytes.

    :param mass: The MusicAssistant instance.
    :param path_or_url: Path or URL to the source image.
    :param size: Target thumbnail size (square), or None for original.
    :param provider: Provider identifier for the image source.
    :param image_format: Normalized output format (PNG or JPEG).
    :param cache_filepath: Absolute path where the thumbnail will be stored.
    :param flatten_transparency: When True, alpha is composited onto white and
        kept as JPEG; when False, transparent sources are emitted as PNG.
    """
    img_data = await get_image_data(mass, path_or_url, provider)
    if not img_data or not isinstance(img_data, bytes):
        raise FileNotFoundError(f"Image not found: {path_or_url}")

    if is_svg_data(img_data):
        # Pillow can't decode SVG; pass it through unchanged.
        thumb_data = img_data
    elif not size and image_format.encode() in img_data:
        thumb_data = img_data
    else:

        def _create_image() -> bytes:
            # PIL is imported at use (here and in create_collage) to keep it off the
            # server startup path
            from PIL import Image, UnidentifiedImageError  # noqa: PLC0415

            data = BytesIO()
            try:
                img: ImageClass = Image.open(BytesIO(img_data))
            except UnidentifiedImageError:
                raise FileNotFoundError(f"Invalid image: {path_or_url}")
            if size:
                img.thumbnail((size, size), Image.Resampling.LANCZOS)
            target_format = image_format
            if target_format == "JPEG" and _has_alpha(img):
                if flatten_transparency:
                    # composite onto white, else the alpha would flatten to black
                    background = Image.new("RGBA", img.size, (255, 255, 255, 255))
                    background.alpha_composite(img.convert("RGBA"))
                    img = background
                else:
                    target_format = "PNG"
            mode = "RGBA" if target_format == "PNG" else "RGB"
            if target_format == "JPEG":
                img.convert(mode).save(data, target_format, quality=95, optimize=False)
            else:
                img.convert(mode).save(data, target_format, optimize=False)
            return data.getvalue()

        thumb_data = await asyncio.to_thread(_create_image)

    # Persist to disk cache (best-effort, don't fail on I/O errors)
    try:
        resolved = os.path.realpath(cache_filepath)
        thumb_dir = os.path.realpath(os.path.join(mass.cache_path, _THUMB_CACHE_DIR))
        if not resolved.startswith(thumb_dir + os.sep):
            raise OSError("Cache path escapes thumbnail directory")
        await asyncio.to_thread(os.makedirs, os.path.dirname(cache_filepath), exist_ok=True)
        async with aiofiles.open(cache_filepath, "wb") as f:
            await f.write(thumb_data)
    except OSError:
        pass

    return thumb_data


async def cleanup_thumb_cache(cache_path: str, max_size_bytes: int) -> int:
    """
    Remove oldest cached thumbnails when total size exceeds the limit.

    :param cache_path: The base cache directory (mass.cache_path).
    :param max_size_bytes: Maximum allowed total size in bytes.
    :returns: Number of files removed.
    """
    thumb_dir = os.path.join(cache_path, _THUMB_CACHE_DIR)

    def _cleanup() -> int:
        if not os.path.isdir(thumb_dir):
            return 0
        entries = []
        for entry in os.scandir(thumb_dir):
            if entry.is_file():
                stat = entry.stat()
                entries.append((entry.path, stat.st_size, stat.st_mtime))
        entries.sort(key=lambda e: e[2])
        total_size = sum(e[1] for e in entries)
        removed = 0
        for filepath, file_size, _ in entries:
            if total_size <= max_size_bytes:
                break
            try:
                Path(filepath).unlink()
                total_size -= file_size
                removed += 1
            except OSError:
                pass
        return removed

    return await asyncio.to_thread(_cleanup)


async def create_collage(
    mass: MusicAssistant,
    images: Iterable[MediaItemImage],
    dimensions: tuple[int, int] = (1500, 1500),
) -> bytes:
    """Create a basic collage image from multiple image urls."""
    from PIL import Image  # noqa: PLC0415

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
    ext = icon_path.rsplit(".", maxsplit=1)[-1]
    assert ext == "svg"
    async with aiofiles.open(icon_path) as _file:
        xml_data = await _file.read()
        assert isinstance(xml_data, str)  # for type checking
        return xml_data.replace("\n", "").strip()
