"""Streaming operations for Tidal."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType, ExternalID, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from .constants import CACHE_CATEGORY_ISRC_MAP, CONF_QUALITY

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

        # 3. Get playback info
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

        # 4. Parse stream URL
        manifest_type = stream_data.get("manifestMimeType", "")
        if "dash+xml" in manifest_type and "manifest" in stream_data:
            url = f"data:application/dash+xml;base64,{stream_data['manifest']}"
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

        return StreamDetails(
            item_id=track.item_id,
            provider=self.provider.instance_id,
            audio_format=AudioFormat(
                content_type=content_type,
                sample_rate=stream_data.get("sampleRate", 44100),
                bit_depth=stream_data.get("bitDepth", 16),
                channels=2,
            ),
            stream_type=StreamType.HTTP,
            duration=track.duration,
            path=url,
            can_seek=True,
            allow_seek=True,
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
