"""Streaming operations for Tidal."""

from __future__ import annotations

import asyncio
import base64
import binascii
import uuid
from collections.abc import Callable, Coroutine
from sqlite3 import OperationalError
from typing import TYPE_CHECKING, Any

from aiohttp import web
from music_assistant_models.enums import ContentType, ExternalID, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from .constants import (
    CACHE_CATEGORY_ISRC_MAP,
    CACHE_CATEGORY_PLAYBACK_INFO,
    CACHE_TTL_PLAYBACK_INFO,
    CONF_QUALITY,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track

    from .provider import TidalProvider


class TidalStreamingManager:
    """Manages Tidal streaming operations."""

    def __init__(self, provider: TidalProvider):
        """Initialize streaming manager."""
        self.provider = provider
        self.api = provider.api
        self.mass = provider.mass

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get stream details for a track."""
        # 1. Try direct lookup
        try:
            track = await self.provider.get_track(item_id)
        except MediaNotFoundError:
            # 2. Fallback to ISRC lookup
            if isrc_track := await self._get_track_by_isrc(item_id):
                track = isrc_track
            else:
                raise MediaNotFoundError(f"Track {item_id} not found")

        quality = self.provider.config.get_value(CONF_QUALITY)
        cache_key = f"{track.item_id}:{quality}"

        # 3. Get playback info (cached to avoid repeated API calls and reduce CDN token churn)
        stream_data: dict[str, Any] | None = await self.mass.cache.get(
            cache_key,
            provider=self.provider.instance_id,
            category=CACHE_CATEGORY_PLAYBACK_INFO,
        )
        if stream_data is None:
            self.provider.logger.debug(
                "Playback info cache miss for track %s (quality=%s) - fetching from API",
                track.item_id,
                quality,
            )
            async with self.api.throttler.bypass():
                api_result = await self.api.get(
                    f"tracks/{track.item_id}/playbackinfopostpaywall",
                    params={
                        "playbackmode": "STREAM",
                        "assetpresentation": "FULL",
                        "audioquality": quality,
                    },
                )
            stream_data = api_result[0] if isinstance(api_result, tuple) else api_result
            await self.mass.cache.set(
                cache_key,
                stream_data,
                provider=self.provider.instance_id,
                category=CACHE_CATEGORY_PLAYBACK_INFO,
                expiration=CACHE_TTL_PLAYBACK_INFO,
            )
        else:
            self.provider.logger.debug(
                "Playback info cache hit for track %s (quality=%s)",
                track.item_id,
                quality,
            )

        # 4. Parse stream URL
        manifest_type = stream_data.get("manifestMimeType", "")
        self.provider.logger.debug(
            "Tidal playback info for track %s: manifestMimeType=%s audioQuality=%s codec=%s",
            track.item_id,
            manifest_type,
            stream_data.get("audioQuality"),
            stream_data.get("codec"),
        )
        if "dash+xml" in manifest_type and "manifest" in stream_data:
            # Tidal returns the DASH manifest as inline base64 content. Passing a data: URI
            # directly to ffmpeg is unreliable — its DASH demuxer stops processing after
            # buffering an initial batch of segments and never fetches the rest, resulting in
            # only a fraction of the track being played. We therefore serve the decoded
            # manifest XML from MA's in-memory stream server so that ffmpeg receives a proper
            # HTTP URL. ffmpeg then connects directly to Tidal's CDN for all audio segments
            # without MA acting as a proxy for the audio data.
            try:
                manifest_bytes = base64.b64decode(stream_data["manifest"])
            except (binascii.Error, TypeError, ValueError) as err:
                self.provider.logger.warning(
                    "Invalid DASH manifest for track %s, evicting cache entry: %s",
                    track.item_id,
                    err,
                )
                await self.mass.cache.delete(
                    cache_key,
                    provider=self.provider.instance_id,
                    category=CACHE_CATEGORY_PLAYBACK_INFO,
                )
                raise MediaNotFoundError(
                    f"Invalid DASH manifest for track {track.item_id}"
                ) from err
            manifest_id = uuid.uuid4().hex
            route_path = f"/tidal-dash/{manifest_id}.mpd"
            unregister = self.mass.streams.register_dynamic_route(
                route_path,
                self._make_manifest_handler(manifest_bytes, self.provider.logger, track.item_id),
                method="GET",
            )
            url = f"{self.mass.streams.base_url}{route_path}"
            self.provider.logger.debug(
                "Using DASH manifest (stream server route %s) for track %s",
                route_path,
                track.item_id,
            )
            # Unregister the route once the track duration has elapsed.
            self.mass.create_task(
                self._async_unregister_manifest_route(
                    unregister, route_path, (track.duration or 600) + 60
                )
            )
        else:
            urls = stream_data.get("urls", [])
            if not urls:
                raise MediaNotFoundError("No stream URL found")
            url = urls[0]
            self.provider.logger.debug("Using direct URL for track %s", track.item_id)

        # 5. Determine format
        audio_quality = stream_data.get("audioQuality")
        if audio_quality in ("HIRES_LOSSLESS", "HI_RES_LOSSLESS", "LOSSLESS"):
            content_type = ContentType.FLAC
        elif codec := stream_data.get("codec"):
            content_type = ContentType.try_parse(codec)
        else:
            content_type = ContentType.MP4

        resolved_audio_format = AudioFormat(
            content_type=content_type,
            sample_rate=stream_data.get("sampleRate", 44100),
            bit_depth=stream_data.get("bitDepth", 16),
            channels=2,
        )

        # Never block or fail playback on DB issues.
        self.mass.create_task(
            self._async_update_provider_mapping_audio_format(
                provider_track_id=track.item_id,
                resolved_audio_format=resolved_audio_format,
            )
        )

        return StreamDetails(
            item_id=track.item_id,
            provider=self.provider.instance_id,
            audio_format=resolved_audio_format,
            stream_type=StreamType.HTTP,
            duration=track.duration,
            path=url,
            can_seek=True,
            allow_seek=True,
        )

    @staticmethod
    def _make_manifest_handler(
        manifest_bytes: bytes,
        logger: Any,
        track_id: str,
    ) -> Callable[[web.Request], Coroutine[Any, Any, web.Response]]:
        """Return an aiohttp request handler that serves the given manifest bytes."""

        async def _handler(_request: web.Request) -> web.Response:
            logger.debug("Serving DASH manifest to ffmpeg for track %s", track_id)
            return web.Response(
                body=manifest_bytes,
                content_type="application/dash+xml",
            )

        return _handler

    async def _async_unregister_manifest_route(
        self, unregister: Callable[[], None], route_path: str, delay: float
    ) -> None:
        """Call unregister after delay seconds to clean up the temporary manifest route."""
        await asyncio.sleep(delay)
        unregister()
        self.provider.logger.debug("Unregistered DASH manifest route %s", route_path)

    async def _async_update_provider_mapping_audio_format(
        self,
        provider_track_id: str,
        resolved_audio_format: AudioFormat,
    ) -> None:
        """Persist resolved audio format on the provider mapping (best-effort)."""
        try:
            lib_track = await self.mass.music.tracks.get_library_item_by_prov_id(
                provider_track_id, self.provider.instance_id
            )
            if not lib_track:
                return

            cur_mapping = next(
                (
                    m
                    for m in lib_track.provider_mappings
                    if m.provider_instance == self.provider.instance_id
                    and m.item_id == provider_track_id
                ),
                None,
            )
            if not cur_mapping or cur_mapping.audio_format == resolved_audio_format:
                return

            await self.mass.music.tracks.update_provider_mapping(
                item_id=lib_track.item_id,
                provider_instance_id=self.provider.instance_id,
                provider_item_id=provider_track_id,
                audio_format=resolved_audio_format,
            )
        except (MediaNotFoundError, OperationalError, AssertionError) as err:
            self.provider.logger.debug(
                "Failed to persist audio_format on provider mapping for Tidal track %s "
                "(provider_instance=%s): %s",
                provider_track_id,
                self.provider.instance_id,
                err,
            )
        except Exception:
            self.provider.logger.exception(
                "Unexpected error while persisting audio_format on provider mapping for "
                "Tidal track %s (provider_instance=%s)",
                provider_track_id,
                self.provider.instance_id,
            )

    async def _get_track_by_isrc(self, item_id: str) -> Track | None:
        """Lookup track by ISRC with caching."""
        # Check cache
        if cached_id := await self.mass.cache.get(
            item_id, provider=self.provider.instance_id, category=CACHE_CATEGORY_ISRC_MAP
        ):
            try:
                return await self.provider.get_track(cached_id)
            except MediaNotFoundError:
                await self.mass.cache.delete(
                    item_id, provider=self.provider.instance_id, category=CACHE_CATEGORY_ISRC_MAP
                )

        # Get library item to find ISRC
        lib_track = await self.mass.music.tracks.get_library_item_by_prov_id(
            item_id, self.provider.instance_id
        )
        if not lib_track:
            return None

        isrc = next((x[1] for x in lib_track.external_ids if x[0] == ExternalID.ISRC), None)
        if not isrc:
            return None

        # Lookup by ISRC
        api_result = await self.api.get(
            "/tracks", params={"filter[isrc]": isrc}, base_url=self.api.OPEN_API_URL
        )
        data = api_result[0] if isinstance(api_result, tuple) else api_result

        data_items = data.get("data", [])
        if not data_items:
            return None

        track_id = str(data_items[0]["id"])

        # Cache result
        await self.mass.cache.set(
            key=item_id,
            data=track_id,
            provider=self.provider.instance_id,
            category=CACHE_CATEGORY_ISRC_MAP,
            persistent=True,
            expiration=86400 * 90,
        )

        return await self.provider.get_track(track_id)
