"""Streaming operations for Tidal."""

from __future__ import annotations

import base64
import hashlib
from contextlib import suppress
from sqlite3 import OperationalError
from typing import TYPE_CHECKING

from aiohttp import web
from music_assistant_models.enums import ContentType, ExternalID, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from .constants import CACHE_CATEGORY_ISRC_MAP, CONF_QUALITY, OPEN_API_URL

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track

    from .provider import TidalProvider

# Seconds of idle buffer after which a DASH manifest route is cleaned up.
# Each time ffmpeg fetches the manifest, the cleanup timer resets — so
# this is only an idle timeout applied AFTER active playback (or seeks)
# stops. Set to 300s so that seeks and queue transitions always land on
# a live route, even when the old ffmpeg process has been dead for
# minutes before the new one starts fetching.
_DASH_ROUTE_IDLE_BUFFER: int = 300


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

        # 3. Get playback info
        async with self.api.throttler.bypass():
            stream_data = await self.api.get(
                f"tracks/{track.item_id}/playbackinfopostpaywall",
                params={
                    "playbackmode": "STREAM",
                    "assetpresentation": "FULL",
                    "audioquality": quality,
                },
            )

        # 4. Parse stream URL
        manifest_type = stream_data.get("manifestMimeType", "")
        if "dash+xml" in manifest_type and "manifest" in stream_data:
            # Tidal returns a DASH manifest (MPD) as a base64 data: URI.
            # ffmpeg re-fetches the MPD during playback to read the
            # segment timeline, but a data: URI can only be read
            # once. Decode the manifest and serve it from a real HTTP
            # endpoint on the stream server so re-fetches succeed.
            manifest_bytes = base64.b64decode(stream_data["manifest"])
            manifest_hash = hashlib.md5(manifest_bytes, usedforsecurity=False).hexdigest()
            route_path = f"/tidal-dash/{manifest_hash}"
            cleanup_id = f"tidal-dash-cleanup-{manifest_hash}"

            # Use track duration + buffer so the route lives through seeks.
            # ffmpeg's manifest re-fetches extend the deadline further via
            # _serve_manifest, but seek kills the old ffmpeg — the new one
            # may not fetch for many seconds so the idle buffer covers that gap.
            cleanup_ttl: float = _DASH_ROUTE_IDLE_BUFFER + (
                track.duration or _DASH_ROUTE_IDLE_BUFFER
            )

            def _schedule_cleanup() -> None:
                """Schedule (or reschedule) idle cleanup for this manifest route."""
                self.mass.call_later(
                    cleanup_ttl,
                    self._remove_dash_route,
                    route_path,
                    task_id=cleanup_id,
                )

            async def _serve_manifest(_request: web.Request) -> web.Response:
                # Extend the idle timeout — ffmpeg is still consuming this route.
                _schedule_cleanup()
                return web.Response(
                    body=manifest_bytes,
                    content_type="application/dash+xml",
                    headers={"Cache-Control": "no-cache"},
                )

            # Register the ephemeral route. If the same manifest was already
            # registered (another track with identical content), this raises
            # RuntimeError — we reuse the existing route.
            with suppress(RuntimeError):
                self.mass.streams.register_dynamic_route(route_path, _serve_manifest, method="GET")

            # Schedule initial cleanup (or extend the existing deadline).
            # This MUST be outside the except block so the timer is always
            # set, even when reusing a pre-registered route.
            _schedule_cleanup()

            url = f"{self.mass.streams.base_url}{route_path}"
        else:
            urls = stream_data.get("urls", [])
            if not urls:
                raise MediaNotFoundError("No stream URL found")
            url = urls[0]

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
        data = await self.api.get("tracks", params={"filter[isrc]": isrc}, base_url=OPEN_API_URL)

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

    def _remove_dash_route(self, route_path: str) -> None:
        """Remove a DASH manifest route from the stream server."""
        with suppress(RuntimeError):
            self.mass.streams.unregister_dynamic_route(route_path, method="GET")
