"""HLS data models and parsing for nicovideo provider."""

from __future__ import annotations

import re
from dataclasses import dataclass


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
class ParsedHLSPlaylist:
    """Parsed HLS playlist data.

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
    def from_text(cls, hls_playlist_text: str) -> ParsedHLSPlaylist:
        """Parse HLS playlist text into structured data.

        Args:
            hls_playlist_text: HLS playlist text

        Returns:
            ParsedHLSPlaylist object with extracted data
        """
        lines = [line.strip() for line in hls_playlist_text.split("\n") if line.strip()]

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
