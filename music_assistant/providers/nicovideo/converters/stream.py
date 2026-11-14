"""Stream converter for nicovideo objects."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata
from niconico.objects.video.watch import (  # noqa: TC002 - pydantic resolves the StreamConversionData field types at runtime
    WatchData,
    WatchMediaAudio,
)
from pydantic import BaseModel

from music_assistant.providers.nicovideo.constants import (
    DOMAND_BID_COOKIE_NAME,
    NICOVIDEO_USER_AGENT,
)
from music_assistant.providers.nicovideo.converters.base import NicovideoConverterBase
from music_assistant.providers.nicovideo.helpers import create_audio_format

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track


class StreamConversionData(BaseModel):
    """Data needed for StreamDetails conversion."""

    watch_data: WatchData
    selected_audio: WatchMediaAudio
    media_playlist_url: str
    domand_bid: str


class NicovideoStreamConverter(NicovideoConverterBase):
    """
    Handles StreamDetails conversion for nicovideo.

    This converter transforms nicovideo video data into MusicAssistant StreamDetails
    that point at the HLS media playlist of the selected audio.
    """

    def convert_from_conversion_data(
        self, conversion_data: StreamConversionData, track: Track
    ) -> StreamDetails:
        """
        Convert StreamConversionData into StreamDetails for HLS streaming with seeking.

        :param conversion_data: Data containing video info, audio selection, and HLS details.
        :param track: The track of the video, used for the stream metadata.
        """
        watch_data = conversion_data.watch_data
        selected_audio = conversion_data.selected_audio
        video_id = watch_data.video.id_

        # Get album and image information
        album = track.album
        # Do not use album image intentionally
        image = track.image

        return StreamDetails(
            provider=self.provider.instance_id,
            item_id=video_id,
            audio_format=create_audio_format(
                sample_rate=selected_audio.sampling_rate,
                bit_rate=selected_audio.bit_rate,
            ),
            media_type=MediaType.TRACK,
            # HTTP rather than HLS: StreamType.HLS fetches the playlist without the domand_bid
            # cookie, while ffmpeg sends the headers below on every playlist, key and segment
            # request
            stream_type=StreamType.HTTP,
            path=conversion_data.media_playlist_url,
            extra_input_args=[
                "-headers",
                f"User-Agent: {NICOVIDEO_USER_AGENT}\r\n"
                f"Cookie: {DOMAND_BID_COOKIE_NAME}={conversion_data.domand_bid}\r\n",
            ],
            duration=watch_data.video.duration,
            stream_metadata=StreamMetadata(
                title=track.name,
                artist=track.artist_str,
                album=album.name if album else None,
                image_url=image.path if image else None,
            ),
            loudness=selected_audio.integrated_loudness,
            allow_seek=True,
            can_seek=True,
        )
