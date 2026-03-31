"""Streaming operations for Tidal."""

from __future__ import annotations

import base64
from collections.abc import AsyncGenerator
from sqlite3 import OperationalError
from typing import TYPE_CHECKING, Any

import aiohttp
from defusedxml import ElementTree
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

# DASH MPD namespace
_MPD_NS = "urn:mpeg:dash:schema:mpd:2011"


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

        # 4. Parse stream URL and build StreamDetails
        return await self._build_stream_details(stream_data, track, cache_key)

    async def _build_stream_details(
        self, stream_data: dict[str, Any], track: Track, cache_key: str
    ) -> StreamDetails:
        """Build StreamDetails from Tidal playback info.

        :param stream_data: Parsed Tidal playback info response.
        :param track: The track being streamed.
        :param cache_key: Cache key for playback info (used to evict on decode error).
        """
        manifest_type = stream_data.get("manifestMimeType", "")
        self.provider.logger.debug(
            "Tidal playback info for track %s: manifestMimeType=%s audioQuality=%s codec=%s",
            track.item_id,
            manifest_type,
            stream_data.get("audioQuality"),
            stream_data.get("codec"),
        )
        dash_data: dict[str, Any] | None = None
        if "dash+xml" in manifest_type and "manifest" in stream_data:
            try:
                manifest_bytes = base64.b64decode(stream_data["manifest"])
                dash_data = self._parse_dash_segments(manifest_bytes, track.item_id)
            except MediaNotFoundError:
                raise
            except Exception as err:
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
            stream_type = StreamType.CUSTOM
            content_type = ContentType.MP4
            url = None
        else:
            urls = stream_data.get("urls", [])
            if not urls:
                raise MediaNotFoundError("No stream URL found")
            url = urls[0]
            stream_type = StreamType.HTTP
            content_type = None
            self.provider.logger.debug("Using direct URL for track %s", track.item_id)

        if content_type is None:
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

        self.mass.create_task(
            self._async_update_provider_mapping_audio_format(
                provider_track_id=track.item_id,
                resolved_audio_format=resolved_audio_format,
            )
        )

        details = StreamDetails(
            item_id=track.item_id,
            provider=self.provider.instance_id,
            audio_format=resolved_audio_format,
            stream_type=stream_type,
            duration=track.duration,
            path=url,
            can_seek=True,
            allow_seek=True,
        )
        if stream_type == StreamType.CUSTOM:
            details.data = dash_data
        return details

    def _parse_dash_segments(self, manifest_bytes: bytes, track_id: str) -> dict[str, Any]:
        """Parse a DASH manifest and return segment info with timing.

        :param manifest_bytes: Decoded DASH manifest XML bytes.
        :param track_id: Track ID for logging.
        """
        root = ElementTree.fromstring(manifest_bytes)

        rep = root.find(f".//{{{_MPD_NS}}}Representation")
        if rep is None:
            raise MediaNotFoundError(f"No Representation in DASH manifest for track {track_id}")
        seg_tpl = rep.find(f"{{{_MPD_NS}}}SegmentTemplate")
        if seg_tpl is None:
            raise MediaNotFoundError(f"No SegmentTemplate in DASH manifest for track {track_id}")

        init_url = seg_tpl.get("initialization", "")
        media_template = seg_tpl.get("media", "")
        start_number = int(seg_tpl.get("startNumber", "1"))
        timescale = int(seg_tpl.get("timescale", "1"))

        # Build segment list with start times from the SegmentTimeline.
        segments: list[dict[str, Any]] = []
        timeline = seg_tpl.find(f"{{{_MPD_NS}}}SegmentTimeline")
        time_pos = 0  # running position in timescale units
        seg_idx = 0
        if timeline is not None:
            for s_elem in timeline.findall(f"{{{_MPD_NS}}}S"):
                duration = int(s_elem.get("d", "0"))
                repeat = int(s_elem.get("r", "0"))
                for _ in range(1 + repeat):
                    url = media_template.replace("$Number$", str(start_number + seg_idx))
                    segments.append({"url": url, "start": time_pos / timescale})
                    time_pos += duration
                    seg_idx += 1

        self.provider.logger.debug(
            "Parsed DASH manifest for track %s: %d segments", track_id, len(segments)
        )
        return {"init_url": init_url, "segments": segments}

    async def get_audio_stream(
        self,
        streamdetails: StreamDetails,
        seek_position: int = 0,
    ) -> AsyncGenerator[bytes, None]:
        """Stream DASH segments as a single contiguous fMP4 byte stream.

        Downloads the init segment (always needed for codec config) followed by
        media segments starting from the one closest to ``seek_position``.
        ffmpeg receives plain fMP4 on stdin, completely bypassing its DASH demuxer.
        """
        dash_data: dict[str, Any] = streamdetails.data
        init_url: str = dash_data["init_url"]
        segments: list[dict[str, Any]] = dash_data["segments"]

        # Determine which segment to start from for seeking.
        start_seg = 0
        if seek_position > 0 and segments:
            for i, seg in enumerate(segments):
                if seg["start"] > seek_position:
                    break
                start_seg = i

        if start_seg > 0:
            self.provider.logger.debug(
                "Seeking to segment %d (%.1fs) for track %s (requested %ds)",
                start_seg,
                segments[start_seg]["start"],
                streamdetails.item_id,
                seek_position,
            )

        session = self.mass.http_session
        timeout = aiohttp.ClientTimeout(total=30, sock_read=15)

        # Always send the init segment first (contains codec config).
        async with session.get(init_url, timeout=timeout) as resp:
            if resp.status != 200:
                raise MediaNotFoundError(
                    f"DASH init segment returned HTTP {resp.status} for track "
                    f"{streamdetails.item_id}"
                )
            async for chunk in resp.content.iter_any():
                yield chunk

        # Stream media segments from start_seg onwards.
        for seg in segments[start_seg:]:
            async with session.get(seg["url"], timeout=timeout) as resp:
                if resp.status != 200:
                    raise MediaNotFoundError(
                        f"DASH segment at {seg['start']:.1f}s returned HTTP {resp.status} "
                        f"for track {streamdetails.item_id}"
                    )
                async for chunk in resp.content.iter_any():
                    yield chunk

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
