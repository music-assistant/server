"""Stream converter for nicovideo objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata
from niconico.objects.video.watch import (  # noqa: TC002 - pydantic resolves the StreamConversionData field types at runtime
    WatchData,
    WatchMediaAudio,
)
from pydantic import BaseModel

from music_assistant.helpers.hls import HLSMediaPlaylist, HLSMediaPlaylistParser
from music_assistant.providers.nicovideo.converters.base import NicovideoConverterBase
from music_assistant.providers.nicovideo.helpers import create_audio_format

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track


@dataclass
class NicovideoStreamData:
    """
    Type-safe container for nicovideo HLS streaming data.

    This dataclass is stored in StreamDetails.data to pass
    HLS-specific information to get_audio_stream().

    Attributes:
        domand_bid: Authentication cookie value
        parsed_hls_playlist: Pre-parsed HLS playlist data (fetched once during conversion)
    """

    domand_bid: str
    parsed_hls_playlist: HLSMediaPlaylist


class StreamConversionData(BaseModel):
    """Data needed for StreamDetails conversion."""

    watch_data: WatchData
    selected_audio: WatchMediaAudio
    hls_url: str
    domand_bid: str
    hls_playlist_text: str


class NicovideoStreamConverter(NicovideoConverterBase):
    """
    Handles StreamDetails conversion for nicovideo.

    This converter transforms nicovideo video data into MusicAssistant StreamDetails
    using StreamType.CUSTOM for optimized HLS streaming with fast seeking support.
    """

    def convert_from_conversion_data(
        self, conversion_data: StreamConversionData, track: Track
    ) -> StreamDetails:
        """
        Convert StreamConversionData into StreamDetails for custom HLS streaming with seeking.

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

        parsed_playlist = HLSMediaPlaylistParser(conversion_data.hls_playlist_text).parse()

        return StreamDetails(
            provider=self.provider.instance_id,
            item_id=video_id,
            audio_format=create_audio_format(
                sample_rate=selected_audio.sampling_rate,
                bit_rate=selected_audio.bit_rate,
            ),
            media_type=MediaType.TRACK,
            # CUSTOM stream type enables optimized seeking for nicovideo's fMP4-based HLS:
            # 1. Generate dynamic playlist starting near target position (coarse seek)
            # 2. Use input-side -ss for precise positioning (fine-tune)
            # Without playlist reconstruction, input-side -ss on HLS results in empty output
            # because FFmpeg cannot identify target segments before parsing the playlist.
            stream_type=StreamType.CUSTOM,
            duration=watch_data.video.duration,
            stream_metadata=StreamMetadata(
                title=track.name,
                artist=track.artist_str,
                album=album.name if album else None,
                image_url=image.path if image else None,
            ),
            loudness=selected_audio.integrated_loudness,
            data=NicovideoStreamData(
                domand_bid=conversion_data.domand_bid,
                parsed_hls_playlist=parsed_playlist,
            ),
            allow_seek=True,
            can_seek=True,
        )
