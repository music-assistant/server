"""Utilities for image manipulation and retrieval."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import itertools
import logging
import os
import random
import re
import tempfile
import time
import urllib.parse
from base64 import b64decode
from collections import OrderedDict
from collections.abc import Iterable
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, cast

import aiofiles
import aiofiles.os
from aiohttp.client_exceptions import ClientError
from music_assistant_models.enums import ProviderIconVariant
from music_assistant_models.errors import (
    MediaNotFoundError,
    MusicAssistantError,
    ProviderUnavailableError,
)
from PIL import Image, UnidentifiedImageError

from music_assistant.constants import APPLICATION_NAME
from music_assistant.helpers.security import is_safe_path
from music_assistant.helpers.tags import get_embedded_image
from music_assistant.helpers.util import join_task
from music_assistant.models.metadata_provider import MetadataProvider
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemImage
    from PIL.Image import Image as ImageClass

    from music_assistant.mass import MusicAssistant


LOGGER = logging.getLogger(__name__)

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

# Source-image cache: the raw origin bytes (remote download, local file read or
# ffmpeg-extracted embedded art) that every derived artifact (thumb sizes/formats,
# color palette, collage tiles) is generated from. Without it, first display of a
# single item fetches the same source several times within seconds. The memory
# tier is byte-budgeted (originals can be multi-MB); sources also get an on-disk
# `<hash>_src` entry in the thumbnail cache dir so multi-variant generation
# after a restart doesn't re-fetch either.
_SOURCE_CACHE_SUFFIX = "_src"
# remote urls can serve new content behind a stable url, so keep the TTL modest;
# local files don't rely on it as invalidate_cached_image busts them on change
_SOURCE_CACHE_TTL = 3600
_SOURCE_MEMORY_MAX_BYTES = 32 * 1024 * 1024
# a single huge original would evict everything else, so cap the entry size
_SOURCE_MEMORY_ENTRY_MAX_BYTES = 8 * 1024 * 1024

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
    thumb_hash: str,
    size: int | None,
    image_format: str,
    flatten_transparency: bool = False,
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


class _SourceMemoryCache:
    """In-memory byte-budgeted LRU tier of the source-image cache."""

    def __init__(self) -> None:
        self.entries: OrderedDict[str, tuple[bytes, float]] = OrderedDict()
        self.total_bytes = 0

    def get(self, key: str) -> bytes | None:
        """Return cached source bytes for key, or None on miss/expiry."""
        entry = self.entries.get(key)
        if entry is None:
            return None
        data, stored_at = entry
        if time.monotonic() - stored_at > _SOURCE_CACHE_TTL:
            self.pop(key)
            return None
        self.entries.move_to_end(key)
        return data

    def put(self, key: str, data: bytes) -> None:
        """Store source bytes for key, evicting oldest entries over the byte budget."""
        if len(data) > _SOURCE_MEMORY_ENTRY_MAX_BYTES:
            return
        self.pop(key)
        self.entries[key] = (data, time.monotonic())
        self.total_bytes += len(data)
        while self.total_bytes > _SOURCE_MEMORY_MAX_BYTES and self.entries:
            _, (evicted, _stored_at) = self.entries.popitem(last=False)
            self.total_bytes -= len(evicted)

    def pop(self, key: str) -> None:
        """Remove the entry for key (if present)."""
        if entry := self.entries.pop(key, None):
            self.total_bytes -= len(entry[0])

    def clear(self) -> None:
        """Remove all entries."""
        self.entries.clear()
        self.total_bytes = 0


_source_memory_cache = _SourceMemoryCache()

# Negative cache for sources that recently failed to fetch. Without it, a
# persistently failing origin (e.g. an artwork URL that keeps returning 404)
# is re-fetched — with a fresh error logged each time — for every thumbnail,
# palette or metadata request that references it, and each consumer waits for
# the full network round-trip just to fail again.
_FAILED_SOURCE_TTL = 300
_FAILED_SOURCE_MAX_ENTRIES = 256
_failed_sources: OrderedDict[str, tuple[float, str]] = OrderedDict()


def _get_failed_source(cache_key: str) -> str | None:
    """Return the failure message for a recently failed source, or None."""
    entry = _failed_sources.get(cache_key)
    if entry is None:
        return None
    expires_at, message = entry
    if time.monotonic() >= expires_at:
        _failed_sources.pop(cache_key, None)
        return None
    return message


def _store_failed_source(cache_key: str, message: str) -> None:
    """Remember a failed source fetch so it is not retried for a short while."""
    _failed_sources[cache_key] = (time.monotonic() + _FAILED_SOURCE_TTL, message)
    _failed_sources.move_to_end(cache_key)
    while len(_failed_sources) > _FAILED_SOURCE_MAX_ENTRIES:
        _failed_sources.popitem(last=False)


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

    Source bytes are cached (in memory and on disk) so that deriving multiple
    artifacts from one image (thumb sizes/formats, palette, collage tiles)
    only fetches the origin once. Concurrent requests for the same source
    share a single fetch.

    :param mass: The MusicAssistant instance.
    :param path_or_url: The image path, URL, or base64 data URI.
    :param provider: The provider ID that can resolve the image.
    :param _depth: Internal recursion depth counter (do not set manually).
    """
    if _depth >= _MAX_IMAGEPROXY_RECURSION_DEPTH:
        msg = f"Maximum recursion depth exceeded when fetching image: {path_or_url}"
        raise FileNotFoundError(msg)
    # base64 data URIs carry their content inline; just decode them
    if path_or_url.startswith("data:image"):
        return b64decode(path_or_url.rsplit(",", maxsplit=1)[-1])
    # imageproxy URLs pointing at our own server are resolved to their underlying
    # (provider, path) before anything is cached: an alias-keyed cache entry
    # would keep serving after the underlying image was invalidated
    if path_or_url.startswith("http") and (
        resolved := await _resolve_own_imageproxy_url(mass, path_or_url)
    ):
        extracted_provider, extracted_path = resolved
        return await get_image_data(mass, extracted_path, extracted_provider, _depth=_depth + 1)
    cache_key = create_thumb_hash(provider, path_or_url)
    if (cached := _source_memory_cache.get(cache_key)) is not None:
        return cached
    # fail fast on sources that just failed instead of hammering the origin
    if (failure := _get_failed_source(cache_key)) is not None:
        raise FileNotFoundError(failure)
    # fetch de-duplicated across concurrent requests for the same source
    task: asyncio.Task[bytes] = mass.create_task(
        _fetch_and_cache_source_image,
        mass,
        path_or_url,
        provider,
        cache_key,
        _depth,
        task_id=f"imgsrc.{cache_key}",
        abort_existing=False,
        # the failure reaches every waiter below, and whoever serves the image reports it
        # once with the path it was asked for
        log_exceptions=False,
    )
    return await join_task(task)


async def _resolve_own_imageproxy_url(mass: MusicAssistant, url: str) -> tuple[str, str] | None:
    """
    Resolve an imageproxy URL pointing at our own server to its (provider, path).

    Returns None when the URL does not point at this server at all; raises
    FileNotFoundError for own-server URLs that carry an invalid or unknown id.

    :param mass: The MusicAssistant instance.
    :param url: The (http/https) URL to inspect.
    """
    parsed_url = urllib.parse.urlparse(url)
    url_origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
    server_origins = {
        f"{p.scheme}://{p.netloc}"
        for b in (mass.webserver.base_url, mass.streams.base_url)
        if (p := urllib.parse.urlparse(b)).netloc
    }
    if url_origin not in server_origins:
        return None
    # opaque-id form: /imageproxy/<image_id>?size=...&fmt=...
    if image_id := _extract_imageproxy_id(url):
        resolved = await mass.metadata.resolve_image_id(image_id)
        if resolved is None:
            msg = f"Unknown image id in URL: {url}"
            raise FileNotFoundError(msg)
        return resolved
    msg = f"Invalid imageproxy URL: {url}"
    raise FileNotFoundError(msg)


def _source_cache_filepath(mass: MusicAssistant, cache_key: str) -> str:
    """Return the on-disk path for a cached source image, validating containment."""
    thumb_dir = os.path.realpath(os.path.join(mass.cache_path, _THUMB_CACHE_DIR))
    filepath = os.path.realpath(os.path.join(thumb_dir, f"{cache_key}{_SOURCE_CACHE_SUFFIX}"))
    if not filepath.startswith(thumb_dir + os.sep):
        msg = f"Cache path escapes thumbnail directory: {filepath}"
        raise OSError(msg)
    return filepath


async def _fetch_and_cache_source_image(
    mass: MusicAssistant, path_or_url: str, provider: str, cache_key: str, depth: int
) -> bytes:
    """
    Fetch source image bytes, store them in the source cache tiers and return them.

    :param mass: The MusicAssistant instance.
    :param path_or_url: The image path or URL.
    :param provider: The provider ID that can resolve the image.
    :param cache_key: Source cache key (create_thumb_hash of provider + path).
    :param depth: Recursion depth of the originating get_image_data call.
    """
    filepath = _source_cache_filepath(mass, cache_key)

    def _read_disk_entry() -> bytes | None:
        # remote urls only count as fresh within the TTL (a CDN can serve new
        # content behind a stable url); local files rely on invalidation instead
        try:
            if not os.path.isfile(filepath):
                return None
            if (
                path_or_url.startswith("http")
                and time.time() - Path(filepath).stat().st_mtime > _SOURCE_CACHE_TTL
            ):
                return None
            with open(filepath, "rb") as _file:
                return _file.read()
        except OSError:
            return None

    if disk_data := await asyncio.to_thread(_read_disk_entry):
        _source_memory_cache.put(cache_key, disk_data)
        return disk_data

    try:
        img_data, disk_cacheable = await _fetch_source_image(mass, path_or_url, provider, depth)
    except (FileNotFoundError, MediaNotFoundError) as err:
        # remember the failure briefly and log it once, concisely: every
        # thumbnail/palette/metadata request for this source would otherwise
        # retry the origin and log the same error over and over
        # a provider signals a missing source with MediaNotFoundError, which is not an
        # OSError and would otherwise bypass this negative cache entirely
        _store_failed_source(cache_key, str(err))
        LOGGER.warning("%s (not retrying for %s seconds)", err, _FAILED_SOURCE_TTL)
        raise
    _failed_sources.pop(cache_key, None)
    _source_memory_cache.put(cache_key, img_data)
    if disk_cacheable:
        # persist to disk cache (best-effort, don't fail on I/O errors)
        try:
            await asyncio.to_thread(os.makedirs, os.path.dirname(filepath), exist_ok=True)
            async with aiofiles.open(filepath, "wb") as _file:
                await _file.write(img_data)
        except OSError:
            pass
    return img_data


async def _fetch_source_image(
    mass: MusicAssistant, path_or_url: str, provider: str, depth: int
) -> tuple[bytes, bool]:
    """
    Fetch image bytes from their origin.

    Returns the raw bytes plus whether they may be persisted on disk under this
    (provider, path) cache key. That is True for every origin except results
    resolved under a different key (imageproxy URLs pointing at our own server):
    an alias-keyed copy would keep being served after the underlying image is
    invalidated.

    :param mass: The MusicAssistant instance.
    :param path_or_url: The image path or URL.
    :param provider: The provider ID that can resolve the image.
    :param depth: Recursion depth of the originating get_image_data call.
    """
    if prov := mass.get_provider(provider):
        assert isinstance(prov, MusicProvider | MetadataProvider | PlayerProvider | PluginProvider)
        if resolved_image := await prov.resolve_image(path_or_url):
            if isinstance(resolved_image, bytes):
                return resolved_image, True
            if isinstance(resolved_image, str):
                path_or_url = resolved_image
    # handle HTTP location
    if path_or_url.startswith("http"):
        # handle imageproxy URLs pointing to our own server
        if resolved := await _resolve_own_imageproxy_url(mass, path_or_url):
            extracted_provider, extracted_path = resolved
            # route through the public entrypoint so the result is cached under
            # the resolved key; don't persist it under this alias key as well
            return (
                await get_image_data(mass, extracted_path, extracted_provider, _depth=depth + 1),
                False,
            )
        try:
            return await _fetch_remote_image(mass, path_or_url), True
        except ClientError as err:
            msg = f"Failed to fetch image from {path_or_url}: {err}"
            raise FileNotFoundError(msg) from err
    # handle base64 embedded images
    if path_or_url.startswith("data:image"):
        return b64decode(path_or_url.split(",")[-1]), True
    # handle FILE location (of type image)
    if path_or_url.endswith(("jpg", "JPG", "png", "PNG", "jpeg", "svg", "SVG")) and is_safe_path(
        path_or_url
    ):
        if await asyncio.to_thread(os.path.isfile, path_or_url):
            async with aiofiles.open(path_or_url, "rb") as _file:
                return cast("bytes", await _file.read()), True
    # use ffmpeg for embedded images
    if is_safe_path(path_or_url) and (img_data := await get_embedded_image(path_or_url)):
        return img_data, True
    if prov is None:
        # every provider-independent route is exhausted, so an absent provider means the
        # path was never resolved rather than that the image is gone. Reporting it as
        # missing would cache that verdict and keep serving it after the provider returns.
        msg = f"{provider} is not available to resolve image {path_or_url}"
        raise ProviderUnavailableError(msg)
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
    thumb_data, _cache_filepath = await _get_image_thumb(
        mass,
        path_or_url,
        size,
        provider,
        image_format,
        flatten_transparency,
    )
    return thumb_data


async def get_image_thumb_path(
    mass: MusicAssistant,
    path_or_url: str,
    size: int | None,
    provider: str,
    image_format: str = "PNG",
    flatten_transparency: bool = False,
) -> str:
    """
    Get the absolute on-disk cache path for a thumbnail.

    Unlike :func:`get_image_thumb`, cache persistence is required and any
    filesystem error is raised to the caller.

    :param mass: The MusicAssistant instance.
    :param path_or_url: Path or URL to the source image.
    :param size: Target thumbnail size (square), or None for original.
    :param provider: Provider identifier for the image source.
    :param image_format: Output format (PNG or JPEG/JPG).
    :param flatten_transparency: When True, alpha is composited onto white and
        kept as JPEG; when False, transparent sources are emitted as PNG.
    """
    thumb_data, cache_filepath = await _get_image_thumb(
        mass,
        path_or_url,
        size,
        provider,
        image_format,
        flatten_transparency,
    )
    await _ensure_thumb_on_disk(mass, cache_filepath, thumb_data)
    return cache_filepath


async def _get_image_thumb(
    mass: MusicAssistant,
    path_or_url: str,
    size: int | None,
    provider: str,
    image_format: str,
    flatten_transparency: bool,
) -> tuple[bytes, str]:
    """
    Resolve thumbnail bytes and their validated cache path.

    :param mass: The MusicAssistant instance.
    :param path_or_url: Path or URL to the source image.
    :param size: Target thumbnail size (square), or None for original.
    :param provider: Provider identifier for the image source.
    :param image_format: Output format (PNG or JPEG/JPG).
    :param flatten_transparency: Whether to flatten alpha onto white for JPEG output.
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

    cache_filepath = _thumb_cache_filepath(mass, cache_filename)

    # 1. Check in-memory FIFO cache
    if (cached := _get_from_memory_cache(cache_filename)) is not None:
        return cached, cache_filepath

    # 2. Check on-disk cache
    if await asyncio.to_thread(os.path.isfile, cache_filepath):
        try:
            async with aiofiles.open(cache_filepath, "rb") as f:
                thumb_data = cast("bytes", await f.read())
        except FileNotFoundError:
            pass
        else:
            _put_in_memory_cache(cache_filename, thumb_data)
            return thumb_data, cache_filepath

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
        # as above: reported once by the caller that serves the image
        log_exceptions=False,
    )
    thumb_data = await join_task(task)
    _put_in_memory_cache(cache_filename, thumb_data)
    return thumb_data, cache_filepath


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
            converted = img.convert(mode)
            if target_format == "JPEG":
                converted.save(data, target_format, quality=95, optimize=False)
            else:
                converted.save(data, target_format, optimize=False)
            return data.getvalue()

        thumb_data = await asyncio.to_thread(_create_image)

    # Persist to disk cache (best-effort, don't fail on I/O errors).
    with contextlib.suppress(OSError):
        await _write_thumb_to_disk(mass, cache_filepath, thumb_data)

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


async def invalidate_cached_image(mass: MusicAssistant, provider: str, path_or_url: str) -> None:
    """
    Remove all cached artifacts (thumbnails + source bytes) for an image.

    Used when the image content behind an unchanged (provider, path) identity
    has changed, e.g. a local file whose (embedded) artwork was replaced.

    :param mass: The MusicAssistant instance.
    :param provider: Provider id exactly as used when the image was requested.
    :param path_or_url: Image path or URL exactly as used when the image was requested.
    """
    thumb_hash = create_thumb_hash(provider, path_or_url)
    prefix = f"{thumb_hash}_"
    for key in [key for key in _thumb_memory_cache if key.startswith(prefix)]:
        _thumb_memory_cache.pop(key, None)
    _source_memory_cache.pop(thumb_hash)
    _failed_sources.pop(thumb_hash, None)

    thumb_dir = os.path.realpath(os.path.join(mass.cache_path, _THUMB_CACHE_DIR))

    def _remove_disk_entries() -> None:
        # covers every size/format/flatten thumb variant plus the `_src` entry
        if not os.path.isdir(thumb_dir):
            return
        for entry in os.scandir(thumb_dir):
            if entry.name.startswith(prefix) and entry.is_file():
                with contextlib.suppress(OSError):
                    Path(entry.path).unlink()

    await asyncio.to_thread(_remove_disk_entries)


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
    # warm the source cache with bounded concurrency and drop images that can't
    # be fetched, so the (serial) tile loop below is served from cache
    fetch_limiter = asyncio.Semaphore(8)

    async def _warm_source_cache(img: MediaItemImage) -> MediaItemImage | None:
        async with fetch_limiter:
            try:
                await get_image_data(mass, img.path, img.provider)
            except FileNotFoundError, MusicAssistantError:
                return None
            return img

    usable_images = [
        img for img in await asyncio.gather(*map(_warm_source_cache, images)) if img is not None
    ]
    if not usable_images:
        msg = "None of the collage images could be fetched"
        raise FileNotFoundError(msg)
    random.shuffle(usable_images)
    iter_images = itertools.cycle(usable_images)

    for x_co in range(0, dimensions[0], image_size):
        for y_co in range(0, dimensions[1], image_size):
            # try a few candidates per tile: a fetched image can still fail to decode
            for _ in range(5):
                img = next(iter_images)
                try:
                    img_data = await get_image_data(mass, img.path, img.provider)
                    await asyncio.to_thread(_add_to_collage, img_data, x_co, y_co)
                except FileNotFoundError, MusicAssistantError, UnidentifiedImageError:
                    continue
                del img_data
                break

    def _save_collage() -> bytes:
        final_data = BytesIO()
        collage.convert("RGB").save(final_data, "JPEG", optimize=True)
        return final_data.getvalue()

    return await asyncio.to_thread(_save_collage)


async def load_provider_icon(icon_path: str) -> tuple[str, bytes]:
    """
    Load a provider icon file and return its mime type and bytes.

    :param icon_path: Path to an svg or (transparent) png icon file.
    """
    ext = icon_path.rsplit(".", maxsplit=1)[-1].lower()
    if ext == "svg":
        async with aiofiles.open(icon_path, encoding="utf-8") as svg_file:
            xml_data = (await svg_file.read()).replace("\n", "").strip()
        return "image/svg+xml", xml_data.encode("utf-8")
    if ext == "png":
        async with aiofiles.open(icon_path, "rb") as png_file:
            return "image/png", await png_file.read()
    msg = f"Unsupported provider icon format: {ext}"
    raise ValueError(msg)


async def detect_provider_icons(
    provider_path: str,
) -> dict[ProviderIconVariant, tuple[str, bytes]]:
    """
    Detect the provider icon variants present in a provider directory.

    Svg is preferred over png when both exist for the same variant.

    :param provider_path: Path to the provider directory to scan.
    """
    variant_files = {
        ProviderIconVariant.DEFAULT: ("icon.svg", "icon.png"),
        ProviderIconVariant.DARK: ("icon_dark.svg", "icon_dark.png"),
        ProviderIconVariant.MONOCHROME: ("icon_monochrome.svg", "icon_monochrome.png"),
    }
    icons: dict[ProviderIconVariant, tuple[str, bytes]] = {}
    for variant, filenames in variant_files.items():
        for filename in filenames:  # svg first -> preferred
            icon_path = os.path.join(provider_path, filename)
            if await aiofiles.os.path.isfile(icon_path):
                icons[variant] = await load_provider_icon(icon_path)
                break
    return icons


def _thumb_cache_filepath(mass: MusicAssistant, cache_filename: str) -> str:
    """Return a validated absolute path for a thumbnail cache filename."""
    thumb_dir = os.path.realpath(os.path.join(mass.cache_path, _THUMB_CACHE_DIR))
    cache_filepath = os.path.realpath(os.path.join(thumb_dir, cache_filename))
    if not cache_filepath.startswith(thumb_dir + os.sep):
        msg = f"Cache path escapes thumbnail directory: {cache_filepath}"
        raise OSError(msg)
    return cache_filepath


async def _ensure_thumb_on_disk(
    mass: MusicAssistant, cache_filepath: str, thumb_data: bytes
) -> None:
    """
    Ensure thumbnail bytes are fully persisted at their cache path.

    :param mass: The MusicAssistant instance.
    :param cache_filepath: Validated absolute thumbnail cache path.
    :param thumb_data: Complete encoded thumbnail bytes.
    """

    def _is_complete() -> bool:
        path = Path(cache_filepath)
        try:
            return path.is_file() and path.stat().st_size == len(thumb_data)
        except OSError:
            return False

    if await asyncio.to_thread(_is_complete):
        return
    await _write_thumb_to_disk(mass, cache_filepath, thumb_data)
    if not await asyncio.to_thread(_is_complete):
        msg = f"Thumbnail cache file was not persisted: {cache_filepath}"
        raise OSError(msg)


async def _write_thumb_to_disk(
    mass: MusicAssistant, cache_filepath: str, thumb_data: bytes
) -> None:
    """
    Atomically persist thumbnail bytes to their validated cache path.

    :param mass: The MusicAssistant instance.
    :param cache_filepath: Validated absolute thumbnail cache path.
    :param thumb_data: Complete encoded thumbnail bytes.
    """
    resolved = os.path.realpath(cache_filepath)
    thumb_dir = os.path.realpath(os.path.join(mass.cache_path, _THUMB_CACHE_DIR))
    if not resolved.startswith(thumb_dir + os.sep):
        msg = f"Cache path escapes thumbnail directory: {resolved}"
        raise OSError(msg)
    await asyncio.to_thread(os.makedirs, thumb_dir, exist_ok=True)
    file_descriptor, temp_filepath = await asyncio.to_thread(
        tempfile.mkstemp, dir=thumb_dir, prefix=".thumb-"
    )
    await asyncio.to_thread(os.close, file_descriptor)
    try:
        async with aiofiles.open(temp_filepath, "wb") as temp_file:
            await temp_file.write(thumb_data)
        await asyncio.to_thread(os.replace, temp_filepath, resolved)
    finally:
        with contextlib.suppress(OSError):
            await asyncio.to_thread(Path(temp_filepath).unlink)
