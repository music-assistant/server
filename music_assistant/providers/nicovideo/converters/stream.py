"""Stream converter for nicovideo objects."""

from __future__ import annotations

from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import UnplayableMediaError
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata
from niconico.objects.video.watch import (  # noqa: TC002 - Using by StreamConversionData(BaseModel Serialization)
    WatchData,
    WatchMediaDomandAudio,
)
from pydantic import BaseModel

from music_assistant.providers.nicovideo.constants import NICOVIDEO_USER_AGENT
from music_assistant.providers.nicovideo.converters.base import NicovideoConverterBase
from music_assistant.providers.nicovideo.helpers import (
    create_audio_format,
)


class StreamConversionData(BaseModel):
    """Data needed for StreamDetails conversion."""

    watch_data: WatchData
    selected_audio: WatchMediaDomandAudio
    hls_url: str
    domand_bid: str


class NicovideoStreamConverter(NicovideoConverterBase):
    """Handles StreamDetails conversion for nicovideo."""

    def convert_by_stream_data(self, stream_data: StreamConversionData) -> StreamDetails:
        """Convert StreamConversionData into StreamDetails."""
        watch_data = stream_data.watch_data
        selected_audio = stream_data.selected_audio
        video_id = watch_data.video.id_

        # Get track information for stream metadata
        track = self.converter_manager.track.convert_by_watch_data(watch_data)
        if not track:
            raise UnplayableMediaError(f"Cannot convert track data for video {video_id}")

        # Build extra input args for ffmpeg
        extra_args = self._build_extra_input_args(stream_data.domand_bid)

        # Get album and image information
        album = track.album
        # Do not use album image intentionally
        image = track.image if track else None

        return StreamDetails(
            provider=self.provider.instance_id,
            item_id=video_id,
            audio_format=create_audio_format(
                sample_rate=selected_audio.sampling_rate,
                bit_rate=selected_audio.bit_rate,
            ),
            media_type=MediaType.TRACK,
            stream_type=StreamType.HTTP,
            duration=watch_data.video.duration,
            stream_metadata=StreamMetadata(
                title=track.name,
                artist=track.artist_str,
                album=album.name if album else None,
                image_url=image.path if image else None,
            ),
            path=stream_data.hls_url,
            extra_input_args=extra_args,
            allow_seek=True,
            can_seek=True,
            # If an expiring URL is used, it may not play when pausing and resuming.
            enable_cache=True,
        )

    def _build_extra_input_args(self, domand_bid: str) -> list[str]:
        """Build extra input args for ffmpeg from domand_bid."""
        # Build headers/cookies expected by downstream consumer
        cookies = f"domand_bid={domand_bid}"

        extra_args = [
            "-user_agent",
            NICOVIDEO_USER_AGENT,
            "-referer",
            "https://www.nicovideo.jp/",
        ]

        if cookies:
            extra_args += [
                "-headers",
                f"Cookie: {cookies}\r\n",
            ]

        return extra_args
