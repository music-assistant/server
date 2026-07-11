"""
Image handling for the Metadata Controller.

Provides the ImageProxyMixin, mixed into the MetaDataController, which resolves
media images to (proxied) URLs, renders and caches thumbnails, serves the
``/imageproxy`` HTTP endpoint, extracts colour palettes and builds playlist
collage images.
"""

from __future__ import annotations

import os
import random
import threading
import time
from base64 import b64encode
from typing import TYPE_CHECKING, cast

import aiofiles
from aiohttp import web
from music_assistant_models.auth import Scope
from music_assistant_models.enums import ImageType
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import (
    Album,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemPalette,
    MediaItemType,
    Track,
)

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.api import api_command
from music_assistant.helpers.colors import get_palette, invalidate_cached_palette
from music_assistant.helpers.images import (
    create_collage,
    create_thumb_hash,
    detect_image_content_format,
    get_image_data,
    get_image_thumb,
    invalidate_cached_image,
)
from music_assistant.helpers.security import is_safe_path

from .constants import (
    _ALLOWED_IMAGEPROXY_SIZES,
    _IMAGE_ID_CACHE_TTL,
    _IMAGE_ID_LRU_MAX,
    _IMAGEPROXY_CONTENT_TYPES,
    _IMAGEPROXY_PATH_PREFIX,
    CACHE_CATEGORY_IMAGE_IDS,
)
from .helpers import (
    _detect_image_format,
    _normalize_imageproxy_format,
)

if TYPE_CHECKING:
    import logging
    from collections import OrderedDict

    from music_assistant import MusicAssistant
    from music_assistant.controllers.cache import CacheController


class ImageProxyMixin:
    """
    Image/imageproxy functionality for the MetaDataController.

    Expects to be mixed with a class providing ``mass``, ``cache``, ``logger``,
    ``domain``, the ``_collage_images_dir`` set during setup and the image-id
    LRU bookkeeping attributes initialised in ``__init__``.
    """

    if TYPE_CHECKING:
        mass: MusicAssistant
        cache: CacheController
        logger: logging.Logger
        domain: str
        _collage_images_dir: str
        _image_id_forward: dict[tuple[str, str], str]
        _image_id_lru: OrderedDict[str, tuple[str, str]]
        _image_id_persisted: dict[str, float]
        _image_id_lock: threading.Lock

    def compute_image_id(self, provider: str, path: str) -> str:
        """
        Return the opaque imageproxy image id for the given image.

        The id is deterministic: the same (provider, path) pair always
        yields the same id, across processes and restarts. Calling this
        also ensures the id is resolvable back to (provider, path) by
        a subsequent imageproxy request.

        Safe to call from any thread.

        :param provider: Provider id that owns / can resolve the image.
        :param path: Image path or URL as the provider knows it.
        """
        # fast path: a bare dict read is atomic, so no hashing or locking is
        # needed for an image that was serialized before. This runs for every
        # image occurrence in every outbound message, so it must stay cheap.
        image_key = (provider, path)
        if (image_id := self._image_id_forward.get(image_key)) is not None:
            return image_id
        image_id = create_thumb_hash(provider, path)
        now = time.time()
        with self._image_id_lock:
            self._image_id_forward[image_key] = image_id
            while len(self._image_id_forward) > _IMAGE_ID_LRU_MAX:
                del self._image_id_forward[next(iter(self._image_id_forward))]
            self._image_id_lru[image_id] = image_key
            self._image_id_lru.move_to_end(image_id)
            while len(self._image_id_lru) > _IMAGE_ID_LRU_MAX:
                self._image_id_lru.popitem(last=False)
            # skip the persist when this process already stored the mapping
            # recently; re-persist once the stored row has burned through half
            # its TTL so long-lived ids remain resolvable across restarts
            persisted_at = self._image_id_persisted.get(image_id)
            if persisted_at is not None and now - persisted_at < _IMAGE_ID_CACHE_TTL / 2:
                return image_id
            # mark optimistically at schedule time to dedupe concurrent bursts;
            # _persist_image_id drops the marker again if storing fails
            self._image_id_persisted[image_id] = now
            while len(self._image_id_persisted) > _IMAGE_ID_LRU_MAX:
                del self._image_id_persisted[next(iter(self._image_id_persisted))]
        # the to_dict hook calls us from the executor when running under
        # _send_message; only call create_task directly when we know we are
        # on the loop thread, otherwise hop across via call_soon_threadsafe
        coro = self._persist_image_id(image_id, provider, path)
        if threading.get_ident() == self.mass.loop_thread_id:
            self.mass.create_task(coro)
        else:
            self.mass.loop.call_soon_threadsafe(self.mass.create_task, coro)
        return image_id

    async def resolve_image_id(self, image_id: str) -> tuple[str, str] | None:
        """
        Return the (provider, path) tuple for a previously registered image id.

        :param image_id: The opaque id as produced by `compute_image_id`.
        """
        with self._image_id_lock:
            if cached := self._image_id_lru.get(image_id):
                self._image_id_lru.move_to_end(image_id)
                return cached
        cached_db = await self.cache.get(
            key=image_id,
            category=CACHE_CATEGORY_IMAGE_IDS,
            provider=self.domain,
        )
        if isinstance(cached_db, dict):
            provider = cached_db.get("provider")
            path = cached_db.get("path")
            if isinstance(provider, str) and isinstance(path, str):
                result = (provider, path)
                with self._image_id_lock:
                    self._image_id_lru[image_id] = result
                    while len(self._image_id_lru) > _IMAGE_ID_LRU_MAX:
                        self._image_id_lru.popitem(last=False)
                    self._image_id_forward[result] = image_id
                    while len(self._image_id_forward) > _IMAGE_ID_LRU_MAX:
                        del self._image_id_forward[next(iter(self._image_id_forward))]
                return result
        return None

    async def get_image_data_for_item(
        self,
        media_item: MediaItemType,
        img_type: ImageType = ImageType.THUMB,
        size: int = 0,
    ) -> bytes | None:
        """Get image data for given MedaItem."""
        img_path = await self.get_image_url_for_item(
            media_item=media_item,
            img_type=img_type,
        )
        if not img_path:
            return None
        try:
            thumbnail = await self.get_thumbnail(img_path, provider="builtin", size=size)
        except MediaNotFoundError:
            return None

        return cast("bytes", thumbnail)

    async def get_image_url_for_item(
        self,
        media_item: MediaItemType | ItemMapping,
        img_type: ImageType = ImageType.THUMB,
        resolve: bool = True,
    ) -> str | None:
        """Get url to image for given media media_item."""
        if not media_item:
            return None

        if isinstance(media_item, ItemMapping):
            # Check if the ItemMapping already has an image - avoid expensive API call
            if media_item.image and media_item.image.type == img_type:
                if media_item.image.remotely_accessible and resolve:
                    return self.get_image_url(media_item.image)
                if not media_item.image.remotely_accessible:
                    return media_item.image.path

            # Only retrieve full item if we don't have the image we need
            if not media_item.uri:
                return None
            retrieved_item = await self.mass.music.get_item_by_uri(media_item.uri)
            if isinstance(retrieved_item, BrowseFolder):
                return None  # can not happen, but guard for type checker
            media_item = retrieved_item

        if media_item and media_item.metadata.images:
            for img in media_item.metadata.images:
                if img.type != img_type:
                    continue
                if not img.remotely_accessible and not resolve:
                    # ignore image if its not remotely accessible and we don't allow resolving
                    continue
                return self.get_image_url(img, prefer_proxy=not img.remotely_accessible)

        # retry with track's album
        if isinstance(media_item, Track) and media_item.album:
            return await self.get_image_url_for_item(media_item.album, img_type, resolve)

        # try artist instead for albums
        if isinstance(media_item, Album) and media_item.artists:
            return await self.get_image_url_for_item(media_item.artists[0], img_type, resolve)

        # last resort: track artist(s)
        if isinstance(media_item, Track) and media_item.artists:
            for artist in media_item.artists:
                return await self.get_image_url_for_item(artist, img_type, resolve)

        return None

    def get_image_url(
        self,
        image: MediaItemImage,
        size: int = 0,
        prefer_proxy: bool = False,
        image_format: str | None = None,
        prefer_stream_server: bool = False,
    ) -> str:
        """Get (proxied) URL for MediaItemImage."""
        if image_format is None:
            image_format = _detect_image_format(image.path)
        if image_format == "svg":
            # SVGs don't need resizing
            size = 0
        if not image.remotely_accessible or prefer_proxy or size:
            # short opaque id form; same id as the thumbnail cache key
            image_id = self.compute_image_id(image.provider, image.path)
            base_url = (
                self.mass.streams.base_url if prefer_stream_server else self.mass.webserver.base_url
            )
            return f"{base_url}/imageproxy/{image_id}?size={size}&fmt={image_format}"
        return image.path

    @api_command("metadata/get_image_palette", required_scope=Scope.LIBRARY_READ)
    async def get_image_palette(self, image_id: str) -> MediaItemPalette | None:
        """
        Get the color palette extracted from a (proxied) image.

        The palette follows the Sendspin color@v1 spec (primary, accent, on_dark,
        on_light, background_dark and background_light). Results are cached, so
        repeated requests for the same image are cheap.

        :param image_id: The opaque imageproxy image id (the ``proxy_id`` field on a
            ``MediaItemImage``). Resolved to the image registered for that id; an
            unknown id yields None.
        """
        resolved = await self.resolve_image_id(image_id)
        if resolved is None:
            return None
        provider, path = resolved
        try:
            return await get_palette(self.mass, path, provider)
        except MediaNotFoundError, OSError:
            return None

    async def invalidate_image_cache(self, provider: str, path: str) -> None:
        """
        Drop every cached artifact for an image so the next request re-fetches it.

        Removes the cached source bytes, all thumbnail size/format variants
        (memory + disk) and the extracted color palette. Call this when the
        image content behind an unchanged (provider, path) identity has
        changed, e.g. a local file whose (embedded) artwork was replaced.

        :param provider: Provider (instance) id that owns / can resolve the image.
        :param path: Image path or URL exactly as referenced by media items.
        """
        await invalidate_cached_image(self.mass, provider, path)
        await invalidate_cached_palette(self.mass, provider, path)

    async def get_thumbnail(
        self,
        path: str,
        provider: str,
        size: int | None = None,
        base64: bool = False,
        image_format: str | None = None,
        flatten_transparency: bool = False,
    ) -> bytes | str:
        """Get/create thumbnail image for path (image url or local path)."""
        if image_format is None:
            image_format = _detect_image_format(path)
        try:
            thumbnail_bytes, content_format = await self._resolve_thumbnail(
                path, provider, size, image_format, flatten_transparency
            )
        except (MediaNotFoundError, OSError) as err:
            # normalize a missing/unreadable image into one typed error so callers
            # (and not just the HTTP imageproxy handler) can handle it uniformly
            raise MediaNotFoundError(f"Image not found or unreadable: {path}") from err
        if base64:
            enc_image = b64encode(thumbnail_bytes).decode()
            return f"data:{_IMAGEPROXY_CONTENT_TYPES[content_format]};base64,{enc_image}"
        return thumbnail_bytes

    async def handle_imageproxy(self, request: web.Request) -> web.Response:
        """
        Serve an image for a `/imageproxy/<image_id>?size=&fmt=` request.

        This is the canonical imageproxy endpoint: clients build the URL by
        taking the `proxy_id` from a `MediaItemImage` and appending it as a
        single path segment, optionally with `size` and `fmt` query parameters.
        """
        # require exactly /imageproxy/<id> (optionally with a trailing slash);
        # extra path segments such as /imageproxy/foo/<id> must not validate
        if not request.path.startswith(_IMAGEPROXY_PATH_PREFIX):
            return web.Response(status=400)
        image_id = request.path[len(_IMAGEPROXY_PATH_PREFIX) :].rstrip("/").lower()
        if len(image_id) != 64 or any(c not in "0123456789abcdef" for c in image_id):
            return web.Response(status=400)
        try:
            size = int(request.query.get("size", "0"))
        except ValueError:
            return web.Response(status=400)
        if size not in _ALLOWED_IMAGEPROXY_SIZES:
            return web.Response(status=400)
        resolved = await self.resolve_image_id(image_id)
        if resolved is None:
            return web.Response(status=404)
        provider, path = resolved
        image_format = _normalize_imageproxy_format(
            request.query.get("fmt")
        ) or _detect_image_format(path)
        return await self._serve_thumbnail(path, provider, size, image_format)

    async def create_collage_image(
        self,
        images: list[MediaItemImage],
        filename: str,
        fanart: bool = False,
    ) -> MediaItemImage | None:
        """Create collage thumb/fanart image for (in-library) playlist."""
        if (len(images) < 8 and fanart) or len(images) < 3:
            # require at least some images otherwise this does not make a lot of sense
            return None
        # limit to 50 images to prevent we're going OOM
        if len(images) > 50:
            images = random.sample(images, 50)
        else:
            random.shuffle(images)
        try:
            # create collage thumb from playlist tracks
            # if playlist has no default image (e.g. a local playlist)
            dimensions = (2500, 1750) if fanart else (1500, 1500)
            img_data = await create_collage(self.mass, images, dimensions)
            # always overwrite existing path
            file_path = os.path.join(self._collage_images_dir, filename)
            async with aiofiles.open(file_path, "wb") as _file:
                await _file.write(img_data)
            del img_data
            return MediaItemImage(
                type=ImageType.FANART if fanart else ImageType.THUMB,
                path=f"/collage/{filename}",
                provider="builtin",
                remotely_accessible=False,
            )
        except Exception as err:
            self.logger.warning(
                "Error while creating playlist image: %s",
                str(err),
                exc_info=err if self.logger.isEnabledFor(10) else None,
            )
        return None

    async def _resolve_thumbnail(
        self,
        path: str,
        provider: str,
        size: int | None,
        image_format: str,
        flatten_transparency: bool,
    ) -> tuple[bytes, str]:
        """
        Fetch image bytes and return them with their resolved content format.

        The served format can differ from the requested one (SVG is passed
        through unchanged and transparent sources may be kept as PNG), so the
        actual format is determined once here and reused by every caller.

        :param path: Image url or local path.
        :param provider: Provider identifier for the image source.
        :param size: Target thumbnail size (square), or None for original.
        :param image_format: Requested output format (jpg/jpeg/png/svg).
        :param flatten_transparency: Composite alpha onto white and keep JPEG when True.
        """
        if not self.mass.get_provider(provider) and not path.startswith("http"):
            raise ProviderUnavailableError
        if provider == "builtin" and path.startswith("/collage/"):
            # special case for collage images
            collage_rel = path.rsplit("/collage/", maxsplit=1)[-1]
            if not is_safe_path(collage_rel):
                raise FileNotFoundError("Invalid collage path")
            path = os.path.join(self._collage_images_dir, collage_rel)
        if image_format == "svg":
            return await get_image_data(self.mass, path, provider), "svg"
        thumbnail_bytes = await get_image_thumb(
            self.mass,
            path,
            size=size,
            provider=provider,
            image_format=image_format,
            flatten_transparency=flatten_transparency,
        )
        return thumbnail_bytes, detect_image_content_format(thumbnail_bytes) or image_format

    async def _serve_thumbnail(
        self, path: str, provider: str, size: int, image_format: str
    ) -> web.Response:
        """Fetch (or render+cache) the thumbnail and produce an HTTP response."""
        # `fmt=jpeg` is the explicit player-media request: players are sent a
        # JPEG for maximum compatibility, and since JPEG has no alpha channel we
        # composite transparency onto white. The auto-detected `fmt=jpg`/`png`
        # default (app/UI) instead keeps transparency as PNG.
        flatten_transparency = image_format == "jpeg"
        try:
            image_data, content_format = await self._resolve_thumbnail(
                path, provider, size, image_format, flatten_transparency
            )
        except Exception as err:
            # broadly catch all exceptions here to ensure we dont crash the request handler
            if isinstance(err, (MediaNotFoundError, FileNotFoundError)):
                self.logger.log(VERBOSE_LOG_LEVEL, "Image not found: %s", path)
            else:
                self.logger.warning(
                    "Error while fetching image %s: %s",
                    path,
                    str(err),
                    exc_info=err if self.logger.isEnabledFor(10) else None,
                )
            return web.Response(status=404)
        response_headers = {
            "Cache-Control": "max-age=31536000",
            "Access-Control-Allow-Origin": "*",
        }
        if content_format == "svg":
            # Sniffed SVGs from attacker-influenceable sources (radio favicons) are
            # served same-origin; without a CSP an embedded <script> would run.
            response_headers["Content-Security-Policy"] = (
                "default-src 'none'; style-src 'unsafe-inline'; sandbox"
            )
            response_headers["X-Content-Type-Options"] = "nosniff"
        return web.Response(
            body=image_data,
            headers=response_headers,
            content_type=_IMAGEPROXY_CONTENT_TYPES[content_format],
        )

    async def _persist_image_id(self, image_id: str, provider: str, path: str) -> None:
        """Store an image-id mapping so a later imageproxy request can resolve it."""
        try:
            # the mapping is usually already stored by a previous process run;
            # probing the expiration first turns the write storm while browsing
            # after a restart into (much cheaper) reads. Only rewrite when the
            # stored row is absent or has burned through half its TTL.
            expires = await self.cache.get_expiration(
                key=image_id,
                category=CACHE_CATEGORY_IMAGE_IDS,
                provider=self.domain,
            )
            if expires is not None and expires - time.time() > _IMAGE_ID_CACHE_TTL / 2:
                return
            await self.cache.set(
                key=image_id,
                data={"provider": provider, "path": path},
                category=CACHE_CATEGORY_IMAGE_IDS,
                provider=self.domain,
                expiration=_IMAGE_ID_CACHE_TTL,
                persistent=True,
            )
        except Exception:
            # drop the optimistic marker so a later encounter retries the persist
            with self._image_id_lock:
                self._image_id_persisted.pop(image_id, None)
            raise
