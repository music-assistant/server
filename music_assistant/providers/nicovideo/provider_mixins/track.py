"""MixIn for NicovideoMusicProvider: track-related methods."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, override

import shortuuid
from aiohttp import web
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    AudioFormat,
    Track,
)

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.providers.nicovideo.converters.stream import NicovideoStreamData
from music_assistant.providers.nicovideo.helpers.hls_seek_optimizer import (
    HLSSeekOptimizer,
)
from music_assistant.providers.nicovideo.provider_mixins.base import (
    NicovideoMusicProviderMixinBase,
)

if TYPE_CHECKING:
    from music_assistant_models.streamdetails import StreamDetails


class NicovideoMusicProviderTrackMixin(NicovideoMusicProviderMixinBase):
    """Track-related methods for NicovideoMusicProvider."""

    @override
    @use_cache(3600 * 24 * 14)  # Cache for 14 days
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        track = await self.service_manager.video.get_video(prov_track_id)
        if not track:
            raise MediaNotFoundError(f"Track with id {prov_track_id} not found on nicovideo.")
        return track

    @override
    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get stream details (streaming URL and format) for given item."""
        if media_type is not MediaType.TRACK:
            raise MediaNotFoundError(f"Media type {media_type} is not supported for stream details")
        return await self.service_manager.video.get_stream_details(item_id)

    @override
    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Get audio stream with dynamic playlist generation for optimized seeking.

        Args:
            streamdetails: Stream details containing domand_bid and parsed_playlist in data field
            seek_position: Position to seek to in seconds

        Yields:
            Audio data bytes
        """
        if not isinstance(streamdetails.data, NicovideoStreamData):
            msg = f"Invalid stream data type: {type(streamdetails.data)}"
            raise TypeError(msg)

        hls_data = streamdetails.data
        processor = HLSSeekOptimizer(hls_data)
        optimized_context = processor.create_stream_context(seek_position)

        # Register dynamic route to serve HLS playlist
        route_id = shortuuid.random(20)
        route_path = f"/nicovideo_m3u8/{route_id}.m3u8"
        playlist_url = f"{self.mass.streams.base_url}{route_path}"

        async def _serve_hls_playlist(_request: web.Request) -> web.Response:
            """Serve dynamically generated HLS playlist (.m3u8) file for seeking."""
            return web.Response(
                text=optimized_context.dynamic_playlist_text,
                content_type="application/vnd.apple.mpegurl",
            )

        unregister = self.mass.streams.register_dynamic_route(route_path, _serve_hls_playlist)

        try:
            async for chunk in get_ffmpeg_stream(
                audio_input=playlist_url,
                input_format=streamdetails.audio_format,
                output_format=AudioFormat(
                    content_type=ContentType.NUT,
                    sample_rate=streamdetails.audio_format.sample_rate,
                    bit_depth=streamdetails.audio_format.bit_depth,
                    channels=streamdetails.audio_format.channels,
                ),
                extra_input_args=optimized_context.extra_input_args,
            ):
                yield chunk
        finally:
            unregister()
