"""
RFC 8216-based HLS utilities.

For simple variant stream selection from master playlists, use helpers.playlists.parse_m3u.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from music_assistant_models.errors import InvalidDataError


@dataclass
class HLSMediaSegment:
    """Single HLS media segment entry with associated metadata."""

    extinf_line: str = ""
    segment_url: str = ""
    key_line: str | None = None
    byterange_line: str | None = None
    discontinuity: bool = False
    map_line: str | None = None
    program_date_time: str | None = None

    @property
    def duration(self) -> float:
        """Extract duration in seconds from #EXTINF line."""
        try:
            duration_part = self.extinf_line.split("#EXTINF:")[1].split(",", 1)[0]
            return float(duration_part.strip())
        except (IndexError, ValueError):
            return 0.0

    @property
    def title(self) -> str | None:
        """Extract optional title from #EXTINF line."""
        try:
            parts = self.extinf_line.split("#EXTINF:")[1].split(",", 1)
            if len(parts) == 2:
                title = parts[1].strip()
                return title if title else None
            return None
        except IndexError:
            return None


@dataclass
class HLSMediaPlaylist:
    """
    HLS media playlist structure with headers, segments, and footers preserved.

    Note: header_lines excludes EXT-X-KEY and EXT-X-MAP tags. Per RFC 8216, these
    tags apply to subsequent segments until overridden, so they're stored per-segment
    for easier manipulation.
    """

    header_lines: list[str] = field(default_factory=list)
    segments: list[HLSMediaSegment] = field(default_factory=list)
    footer_lines: list[str] = field(default_factory=list)


class HLSMediaPlaylistParser:
    """RFC 8216-based HLS media playlist parser."""

    def __init__(self, hls_playlist_text: str) -> None:
        """Initialize parser with playlist text."""
        self.hls_playlist_text = hls_playlist_text
        self.result = HLSMediaPlaylist()
        self.working_segment = HLSMediaSegment()
        self.segments_started = False

    def parse(self) -> HLSMediaPlaylist:
        """Parse HLS media playlist text into structured data.

        Returns:
            HLSMediaPlaylist object with extracted structure

        Raises:
            InvalidDataError: If playlist doesn't start with #EXTM3U or has invalid format
        """
        lines = [line.strip() for line in self.hls_playlist_text.split("\n") if line.strip()]

        if not lines or not lines[0].startswith("#EXTM3U"):
            msg = "Invalid HLS playlist: must start with #EXTM3U"
            raise InvalidDataError(msg)

        for line in lines:
            self.process_line(line)

        if not self.result.segments:
            msg = "Invalid HLS playlist: no segments found"
            raise InvalidDataError(msg)

        return self.result

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
            pass
        elif self.working_segment.extinf_line:
            self._on_segment_url(line)

    def _on_extinf(self, line: str) -> None:
        """Handle #EXTINF tag."""
        if self.working_segment.extinf_line:
            msg = (
                f"Malformed HLS playlist: #EXTINF '{line}' found without "
                f"preceding segment URL for '{self.working_segment.extinf_line}'"
            )
            raise InvalidDataError(msg)
        self.segments_started = True
        self.working_segment.extinf_line = line

    def _on_key_line(self, line: str) -> None:
        """Handle #EXT-X-KEY tag."""
        self.working_segment.key_line = line

    def _on_map_line(self, line: str) -> None:
        """Handle #EXT-X-MAP tag."""
        self.working_segment.map_line = line

    def _on_program_date_time(self, line: str) -> None:
        """Handle #EXT-X-PROGRAM-DATE-TIME tag."""
        self.working_segment.program_date_time = line

    def _on_byterange(self, line: str) -> None:
        """Handle #EXT-X-BYTERANGE tag."""
        self.working_segment.byterange_line = line

    def _on_discontinuity(self) -> None:
        """Handle #EXT-X-DISCONTINUITY tag."""
        self.working_segment.discontinuity = True

    def _on_ext_tag(self, line: str) -> None:
        """Handle other #EXT tags."""
        if self.segments_started:
            self.result.footer_lines.append(line)
        else:
            self.result.header_lines.append(line)

    def _on_segment_url(self, line: str) -> None:
        """Handle segment URL following #EXTINF."""
        self.working_segment.segment_url = line
        self.result.segments.append(self.working_segment)

        self.working_segment = HLSMediaSegment(
            key_line=self.working_segment.key_line,
            map_line=self.working_segment.map_line,
        )
