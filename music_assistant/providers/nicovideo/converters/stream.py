"""Stream converter for nicovideo objects."""

from __future__ import annotations

import re
from dataclasses import dataclass

from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import UnplayableMediaError
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata
from niconico.objects.video.watch import (  # noqa: TC002 - Using by StreamConversionData(BaseModel Serialization)
    WatchData,
    WatchMediaDomandAudio,
)
from pydantic import BaseModel

from music_assistant.providers.nicovideo.converters.base import NicovideoConverterBase
from music_assistant.providers.nicovideo.helpers import create_audio_format


@dataclass
class HLSSegment:
    """Single HLS segment entry.

    Attributes:
        duration_line: #EXTINF line with duration (e.g., "#EXTINF:5.967528,")
        segment_url: URL to the segment file
    """

    duration_line: str
    segment_url: str


@dataclass
class ParsedM3u8:
    """Parsed HLS m3u8 playlist data.

    Attributes:
        init_segment_url: URL to the initialization segment (#EXT-X-MAP)
        encryption_key_line: Encryption key line (#EXT-X-KEY) if present
        segments: List of HLS segments
        header_lines: Playlist header lines (#EXTM3U, #EXT-X-VERSION, etc.)
    """

    init_segment_url: str
    encryption_key_line: str
    segments: list[HLSSegment]
    header_lines: list[str]

    @classmethod
    def from_text(cls, m3u8_text: str) -> ParsedM3u8:
        """Parse m3u8 text into structured data.

        Args:
            m3u8_text: m3u8 playlist text

        Returns:
            ParsedM3u8 object with extracted data
        """
        lines = [line.strip() for line in m3u8_text.split("\n") if line.strip()]

        # Extract header lines (#EXTM3U, #EXT-X-VERSION, etc.)
        header_lines = []
        for line in lines:
            if line.startswith("#EXT-X-TARGETDURATION"):
                break
            if line.startswith("#EXT"):
                header_lines.append(line)

        # Extract init segment URL from #EXT-X-MAP
        init_segment_url = ""
        for line in lines:
            if line.startswith("#EXT-X-MAP:"):
                match = re.search(r'URI="([^"]+)"', line)
                if match:
                    init_segment_url = match.group(1)
                break

        # Extract encryption key line
        encryption_key_line = ""
        for line in lines:
            if line.startswith("#EXT-X-KEY:"):
                encryption_key_line = line
                break

        # Extract segments (duration + URL pairs)
        segments: list[HLSSegment] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("#EXTINF:"):
                duration_line = line
                # Next line should be segment URL
                if i + 1 < len(lines):
                    segment_url = lines[i + 1]
                    if not segment_url.startswith("#"):
                        segments.append(HLSSegment(duration_line, segment_url))
                        i += 2
                        continue
            i += 1

        return cls(
            init_segment_url=init_segment_url,
            encryption_key_line=encryption_key_line,
            segments=segments,
            header_lines=header_lines,
        )


@dataclass
class NicovideoStreamData:
    """Type-safe container for nicovideo HLS streaming data.

    This dataclass is stored in StreamDetails.data to pass
    HLS-specific information to get_audio_stream().

    Attributes:
        domand_bid: Authentication cookie value
        parsed_m3u8: Pre-parsed HLS playlist data (fetched once during conversion)
    """

    domand_bid: str
    parsed_m3u8: ParsedM3u8


class StreamConversionData(BaseModel):
    """Data needed for StreamDetails conversion."""

    watch_data: WatchData
    selected_audio: WatchMediaDomandAudio
    hls_url: str
    domand_bid: str
    m3u8_text: str


class NicovideoStreamConverter(NicovideoConverterBase):
    """Handles StreamDetails conversion for nicovideo.

    This converter transforms nicovideo video data into MusicAssistant StreamDetails
    using StreamType.CUSTOM for optimized HLS streaming with fast seeking support.
    """

    def convert_by_stream_data(self, stream_data: StreamConversionData) -> StreamDetails:
        """Convert StreamConversionData into StreamDetails.

        Args:
            stream_data: Data containing video info, audio selection, and HLS details

        Returns:
            StreamDetails configured for custom HLS streaming with seek optimization

        Raises:
            UnplayableMediaError: If track data cannot be converted
        """
        watch_data = stream_data.watch_data
        selected_audio = stream_data.selected_audio
        video_id = watch_data.video.id_

        # Get track information for stream metadata
        track = self.converter_manager.track.convert_by_watch_data(watch_data)
        if not track:
            raise UnplayableMediaError(f"Cannot convert track data for video {video_id}")

        # Get album and image information
        album = track.album
        # Do not use album image intentionally
        image = track.image if track else None

        parsed_m3u8 = ParsedM3u8.from_text(stream_data.m3u8_text)

        return StreamDetails(
            provider=self.provider.instance_id,
            item_id=video_id,
            audio_format=create_audio_format(
                sample_rate=selected_audio.sampling_rate,
                bit_rate=selected_audio.bit_rate,
            ),
            media_type=MediaType.TRACK,
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
                domand_bid=stream_data.domand_bid,
                parsed_m3u8=parsed_m3u8,
            ),
            allow_seek=True,
            can_seek=True,
        )
