"""HLS seek optimizer for nicovideo provider."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from music_assistant.providers.nicovideo.constants import (
    DOMAND_BID_COOKIE_NAME,
    NICOVIDEO_USER_AGENT,
)
from music_assistant.providers.nicovideo.helpers.utils import log_verbose

if TYPE_CHECKING:
    from music_assistant.providers.nicovideo.converters.stream import NicovideoStreamData
    from music_assistant.providers.nicovideo.helpers.hls_models import ParsedHLSPlaylist

LOGGER = logging.getLogger(__name__)


@dataclass
class SeekOptimizedStreamContext:
    """Context for seek-optimized HLS streaming.

    Contains all information needed to set up streaming with fast seeking:
    - Dynamic playlist content to serve
    - FFmpeg extra input arguments (headers, seeking)
    """

    dynamic_playlist_text: str
    extra_input_args: list[str]


class HLSSeekOptimizer:
    """Optimizes HLS streaming with fast seeking support.

    Generates dynamic HLS playlists and FFmpeg arguments for efficient
    seeking by calculating optimal segment start positions.

    This eliminates the need to decode all segments before the target position,
    enabling instant seeking in long nicovideo streams.
    """

    def __init__(
        self,
        hls_data: NicovideoStreamData,
    ) -> None:
        """Initialize seek optimizer with HLS data.

        Args:
            hls_data: HLS streaming data containing parsed playlist and authentication info
        """
        self.parsed_playlist: ParsedHLSPlaylist = hls_data.parsed_hls_playlist
        self.domand_bid = hls_data.domand_bid

    def _calculate_start_segment(self, seek_position: int) -> tuple[int, float]:
        """Calculate which segment to start from based on seek position.

        Args:
            seek_position: Desired seek position in seconds

        Returns:
            Tuple of (segment_index, offset_within_segment)
            - segment_index: Index of the segment to start from
            - offset_within_segment: Seconds to skip within that segment
        """
        if seek_position <= 0:
            return (0, 0.0)

        accumulated_time = 0.0
        for idx, segment in enumerate(self.parsed_playlist.segments):
            # Extract duration from #EXTINF:5.967528,
            match = re.search(r"#EXTINF:([0-9.]+)", segment.duration_line)
            if match:
                segment_duration = float(match.group(1))
                if accumulated_time + segment_duration > seek_position:
                    # Found the segment containing seek_position
                    offset = seek_position - accumulated_time
                    return (idx, offset)
                accumulated_time += segment_duration

        # If seek position is beyond total duration, start from last segment
        return (max(0, len(self.parsed_playlist.segments) - 1), 0.0)

    def _generate_dynamic_playlist(self, start_segment_idx: int) -> str:
        """Generate dynamic HLS playlist with segments from start_segment_idx onward.

        Args:
            start_segment_idx: Index to start from

        Returns:
            Dynamic HLS playlist text
        """
        lines = []

        # Add header lines
        lines.extend(self.parsed_playlist.header_lines)

        # Calculate target duration from segments (rounded up)
        max_duration = 6  # Default fallback
        for segment in self.parsed_playlist.segments:
            match = re.search(r"#EXTINF:([0-9.]+)", segment.duration_line)
            if match:
                duration = float(match.group(1))
                max_duration = max(max_duration, int(duration) + 1)

        # Add required HLS tags
        lines.extend(
            [
                f"#EXT-X-TARGETDURATION:{max_duration}",
                "#EXT-X-MEDIA-SEQUENCE:1",
                "#EXT-X-PLAYLIST-TYPE:VOD",
            ]
        )

        # Add init segment
        if self.parsed_playlist.init_segment_url:
            lines.append(f'#EXT-X-MAP:URI="{self.parsed_playlist.init_segment_url}"')

        # Add encryption key if present
        if self.parsed_playlist.encryption_key_line:
            lines.append(self.parsed_playlist.encryption_key_line)

        # Add segments from start_segment_idx onward
        for segment in self.parsed_playlist.segments[start_segment_idx:]:
            lines.append(segment.duration_line)
            lines.append(segment.segment_url)

        # Add end tag
        lines.append("#EXT-X-ENDLIST")

        return "\n".join(lines)

    def create_stream_context(self, seek_position: int) -> SeekOptimizedStreamContext:
        """Create seek-optimized streaming context.

        This method combines segment calculation, playlist generation,
        and FFmpeg arguments preparation for fast seeking.

        Args:
            seek_position: Position to seek to in seconds

        Returns:
            SeekOptimizedStreamContext with all streaming setup information
        """
        # Stage 1: Calculate which segment contains the seek position (coarse seek)
        # This avoids processing unnecessary segments before the target position
        start_segment_idx, offset_within_segment = self._calculate_start_segment(seek_position)
        if seek_position > 0:
            log_verbose(
                LOGGER,
                "HLS seek: position=%ds → segment %d/%d (offset %.2fs)",
                seek_position,
                start_segment_idx,
                len(self.parsed_playlist.segments),
                offset_within_segment,
            )

        # Generate HLS playlist starting from the calculated segment
        dynamic_playlist_text = self._generate_dynamic_playlist(start_segment_idx)

        # Build FFmpeg extra input arguments
        headers = (
            f"User-Agent: {NICOVIDEO_USER_AGENT}\r\n"
            f"Cookie: {DOMAND_BID_COOKIE_NAME}={self.domand_bid}\r\n"
        )
        extra_input_args = ["-headers", headers]

        # Stage 2: Apply input-side -ss for fine-tuning within the segment
        if offset_within_segment > 0:
            extra_input_args.extend(["-ss", str(offset_within_segment)])

        return SeekOptimizedStreamContext(
            dynamic_playlist_text=dynamic_playlist_text,
            extra_input_args=extra_input_args,
        )
