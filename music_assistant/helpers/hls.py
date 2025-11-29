"""RFC 8216-compliant HLS media playlist parser.

For simple variant stream selection from master playlists, use helpers.playlists.parse_m3u.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from music_assistant_models.errors import InvalidDataError


@dataclass
class HLSSegment:
    """Single HLS segment entry.

    Attributes:
        extinf_line: #EXTINF line with duration (e.g., "#EXTINF:5.967528,")
        segment_url: URL to the segment file
        key_line: Optional #EXT-X-KEY line that applies to this segment
        byterange_line: Optional #EXT-X-BYTERANGE line for byte-range requests
        discontinuity: Whether this segment has a discontinuity marker before it
        map_line: Optional #EXT-X-MAP line for fMP4 initialization segment
        program_date_time: Optional #EXT-X-PROGRAM-DATE-TIME for wall-clock time sync
    """

    extinf_line: str = ""
    segment_url: str = ""
    key_line: str | None = None
    byterange_line: str | None = None
    discontinuity: bool = False
    map_line: str | None = None
    program_date_time: str | None = None

    @property
    def duration(self) -> float:
        """Extract duration in seconds from #EXTINF line.

        Format: #EXTINF:<duration>,[<title>]
        """
        try:
            # Split by "#EXTINF:" and then by "," to extract duration
            duration_part = self.extinf_line.split("#EXTINF:")[1].split(",", 1)[0]
            return float(duration_part.strip())
        except (IndexError, ValueError):
            return 0.0

    @property
    def title(self) -> str | None:
        """Extract optional title from #EXTINF line.

        Format: #EXTINF:<duration>,[<title>]
        """
        try:
            # Split by "#EXTINF:" and then by "," to extract title
            parts = self.extinf_line.split("#EXTINF:")[1].split(",", 1)
            if len(parts) == 2:
                title = parts[1].strip()
                return title if title else None
            return None
        except IndexError:
            return None


@dataclass
class HLSPlaylistStructure:
    """HLS playlist structure with all components preserved.

    This class preserves the complete HLS playlist structure including headers,
    encryption, and segments to enable dynamic manipulation.

    This implementation supports both MPEG-2 TS and fMP4 segment formats, handling
    encryption (#EXT-X-KEY), discontinuities (#EXT-X-DISCONTINUITY), byte-range
    requests (#EXT-X-BYTERANGE), fMP4 initialization segments (#EXT-X-MAP), and
    wall-clock time synchronization (#EXT-X-PROGRAM-DATE-TIME) for live streams.

    Attributes:
        header_lines: All playlist header lines including #EXTM3U, #EXT-X-VERSION,
                     #EXT-X-TARGETDURATION, etc.
        segments: List of HLS segments in order
        footer_lines: All playlist footer lines, typically #EXT-X-ENDLIST

    Notes:
        - #EXT-X-MAP and #EXT-X-KEY is inherited by subsequent segments until changed
        - #EXT-X-PROGRAM-DATE-TIME applies only to the next segment (per RFC 8216)
        - Comment lines (# without EXT) are ignored per RFC 8216
    """

    header_lines: list[str] = field(default_factory=list)
    segments: list[HLSSegment] = field(default_factory=list)
    footer_lines: list[str] = field(default_factory=list)

    @property
    def total_duration(self) -> float:
        """Calculate total duration of the playlist in seconds."""
        return sum(segment.duration for segment in self.segments)


class HLSPlaylistParser:
    """RFC 8216-compliant HLS media playlist parser with full segment detail preservation.

    This parser maintains complete playlist structure (headers, per-segment metadata,
    footers) to enable dynamic manipulation such as segment filtering, playlist
    reconstruction, and precise seeking operations.

    For simple variant stream selection from HLS master playlists, use
    helpers.playlists.parse_m3u instead.
    """

    def __init__(self) -> None:
        """Initialize parser with empty result structure."""
        self.result = HLSPlaylistStructure()
        self.working_segment = HLSSegment()
        self.segments_started = False

    @classmethod
    def parse(cls, hls_playlist_text: str) -> HLSPlaylistStructure:
        """Parse HLS playlist text into structured data.

        Args:
            hls_playlist_text: HLS playlist text in M3U8 format

        Returns:
            HLSPlaylistStructure object with extracted structure

        Raises:
            InvalidDataError: If playlist doesn't start with #EXTM3U or has invalid format
        """
        lines = [line.strip() for line in hls_playlist_text.split("\n") if line.strip()]

        if not lines or not lines[0].startswith("#EXTM3U"):
            msg = "Invalid HLS playlist: must start with #EXTM3U"
            raise InvalidDataError(msg)

        parser = cls()

        for line in lines:
            parser.process_line(line)

        if not parser.result.segments:
            msg = "Invalid HLS playlist: no segments found"
            raise InvalidDataError(msg)

        return parser.result

    def process_line(self, line: str) -> None:
        """Process a single line from the playlist."""
        if line.startswith("#EXTINF:"):
            self._on_extinf(line)
        elif line.startswith("#EXT-X-KEY:"):
            self._on_key_line(line)
        elif line.startswith("#EXT-X-MAP:"):
            self._on_map_line(line)
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            self._on_program_date_time(line)
        elif line.startswith("#EXT-X-BYTERANGE:"):
            self._on_byterange(line)
        elif line.startswith("#EXT-X-DISCONTINUITY"):
            self._on_discontinuity()
        elif line.startswith("#EXT"):
            self._on_ext_tag(line)
        elif line.startswith("#"):
            # Ignore comment lines (lines starting with # but not #EXT)
            pass
        elif self.working_segment.extinf_line:
            self._on_segment_url(line)

    def _on_extinf(self, line: str) -> None:
        """Handle #EXTINF tag - marks start of segment.

        Raises:
            InvalidDataError: If another #EXTINF appears without a segment URL
        """
        if self.working_segment.extinf_line:
            msg = (
                f"Malformed HLS playlist: #EXTINF '{line}' found without "
                f"preceding segment URL for '{self.working_segment.extinf_line}'"
            )
            raise InvalidDataError(msg)
        self.segments_started = True
        self.working_segment.extinf_line = line

    def _on_key_line(self, line: str) -> None:
        """Handle #EXT-X-KEY tag - applies to subsequent segments."""
        self.working_segment.key_line = line
        # KEY should appear in header before first segment if used globally
        if not self.segments_started:
            self.result.header_lines.append(line)

    def _on_map_line(self, line: str) -> None:
        """Handle #EXT-X-MAP tag - applies to subsequent segments (fMP4 init segment)."""
        self.working_segment.map_line = line
        # MAP must appear in header before first segment (RFC 8216 Section 4.3.2.5)
        if not self.segments_started:
            self.result.header_lines.append(line)

    def _on_program_date_time(self, line: str) -> None:
        """Handle #EXT-X-PROGRAM-DATE-TIME tag - applies to next segment only."""
        self.working_segment.program_date_time = line

    def _on_byterange(self, line: str) -> None:
        """Handle #EXT-X-BYTERANGE tag for next segment."""
        self.working_segment.byterange_line = line

    def _on_discontinuity(self) -> None:
        """Handle #EXT-X-DISCONTINUITY tag for next segment."""
        self.working_segment.discontinuity = True

    def _on_ext_tag(self, line: str) -> None:
        """Handle other #EXT tags (header or footer)."""
        if self.segments_started:
            # After segments started, tags go to footer
            self.result.footer_lines.append(line)
        else:
            # Before segments, tags go to header
            self.result.header_lines.append(line)

    def _on_segment_url(self, line: str) -> None:
        """Handle segment URL following #EXTINF."""
        self.working_segment.segment_url = line
        self.result.segments.append(self.working_segment)

        # Prepare next segment with inherited state (KEY and MAP persist)
        self.working_segment = HLSSegment(
            key_line=self.working_segment.key_line,
            map_line=self.working_segment.map_line,
        )
