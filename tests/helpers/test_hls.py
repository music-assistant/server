"""Tests for HLS playlist parsing utilities."""

from __future__ import annotations

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers.hls import HLSMediaPlaylistParser, HLSMediaSegment


def test_basic_vod_playlist() -> None:
    """Test parsing basic VOD playlist with encryption (standard fMP4 format)."""
    playlist_text = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-KEY:METHOD=AES-128,URI="https://example.com/key.bin"
#EXT-X-MAP:URI="init.mp4"
#EXTINF:5.967528,
segment0.m4s
#EXTINF:5.967528,
segment1.m4s
#EXTINF:5.967528,
segment2.m4s
#EXTINF:3.123456,
segment3.m4s
#EXT-X-ENDLIST
"""
    result = HLSMediaPlaylistParser(playlist_text).parse()

    assert len(result.header_lines) == 5
    assert len(result.segments) == 4
    assert len(result.footer_lines) == 1

    # Check total duration
    total_duration = sum(segment.duration for segment in result.segments)
    assert total_duration == pytest.approx(21.02604, rel=1e-6)

    # Check first segment inherits MAP from header
    assert result.segments[0].segment_url == "segment0.m4s"
    assert result.segments[0].duration == pytest.approx(5.967528, rel=1e-6)
    assert (
        result.segments[0].key_line == '#EXT-X-KEY:METHOD=AES-128,URI="https://example.com/key.bin"'
    )
    assert result.segments[0].map_line == '#EXT-X-MAP:URI="init.mp4"'
    assert result.segments[0].byterange_line is None
    assert result.segments[0].discontinuity is False
    assert result.segments[0].program_date_time is None

    # Check last segment - also has MAP
    assert result.segments[3].segment_url == "segment3.m4s"
    assert result.segments[3].duration == pytest.approx(3.123456, rel=1e-6)
    assert result.segments[3].map_line == '#EXT-X-MAP:URI="init.mp4"'


def test_live_stream_with_program_date_time() -> None:
    """Test parsing live stream with PROGRAM-DATE-TIME tags."""
    playlist_text = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:2680
#EXT-X-PROGRAM-DATE-TIME:2025-11-27T10:15:00.000Z
#EXTINF:9.009,
https://example.com/segment2680.ts
#EXTINF:9.009,
https://example.com/segment2681.ts
#EXT-X-DISCONTINUITY
#EXT-X-PROGRAM-DATE-TIME:2025-11-27T10:15:20.000Z
#EXTINF:9.009,
https://example.com/segment2682.ts
"""
    result = HLSMediaPlaylistParser(playlist_text).parse()

    assert len(result.segments) == 3

    # First segment has PROGRAM-DATE-TIME
    assert (
        result.segments[0].program_date_time == "#EXT-X-PROGRAM-DATE-TIME:2025-11-27T10:15:00.000Z"
    )
    assert result.segments[0].discontinuity is False

    # Second segment does not have PROGRAM-DATE-TIME (single-use tag)
    assert result.segments[1].program_date_time is None
    assert result.segments[1].discontinuity is False

    # Third segment has both discontinuity and new PROGRAM-DATE-TIME
    assert result.segments[2].discontinuity is True
    assert (
        result.segments[2].program_date_time == "#EXT-X-PROGRAM-DATE-TIME:2025-11-27T10:15:20.000Z"
    )


def test_byterange_segments() -> None:
    """Test parsing playlist with byte-range segments."""
    playlist_text = """#EXTM3U
#EXT-X-VERSION:4
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-MAP:URI="init.mp4"
#EXTINF:10.0,
#EXT-X-BYTERANGE:1000@0
video.mp4
#EXTINF:10.0,
#EXT-X-BYTERANGE:1500
video.mp4
#EXTINF:10.0,
#EXT-X-BYTERANGE:1200
video.mp4
#EXT-X-ENDLIST
"""
    result = HLSMediaPlaylistParser(playlist_text).parse()

    assert len(result.segments) == 3
    assert result.segments[0].byterange_line == "#EXT-X-BYTERANGE:1000@0"
    assert result.segments[1].byterange_line == "#EXT-X-BYTERANGE:1500"
    assert result.segments[2].byterange_line == "#EXT-X-BYTERANGE:1200"

    # All segments use same file
    assert result.segments[0].segment_url == "video.mp4"
    assert result.segments[1].segment_url == "video.mp4"
    assert result.segments[2].segment_url == "video.mp4"


def test_multiple_encryption_keys() -> None:
    """Test parsing playlist with multiple encryption keys (ad insertion scenario)."""
    playlist_text = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:15
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-KEY:METHOD=AES-128,URI="https://example.com/key1.bin"
#EXTINF:10.0,
segment0.ts
#EXTINF:10.0,
segment1.ts
#EXT-X-DISCONTINUITY
#EXT-X-KEY:METHOD=AES-128,URI="https://example.com/key2.bin"
#EXTINF:15.0,
ad_segment.ts
#EXT-X-DISCONTINUITY
#EXT-X-KEY:METHOD=AES-128,URI="https://example.com/key1.bin"
#EXTINF:10.0,
segment2.ts
#EXT-X-ENDLIST
"""
    result = HLSMediaPlaylistParser(playlist_text).parse()

    assert len(result.segments) == 4

    # First two segments use key1
    assert (
        result.segments[0].key_line
        == '#EXT-X-KEY:METHOD=AES-128,URI="https://example.com/key1.bin"'
    )
    assert (
        result.segments[1].key_line
        == '#EXT-X-KEY:METHOD=AES-128,URI="https://example.com/key1.bin"'
    )
    assert result.segments[1].discontinuity is False

    # Ad segment has discontinuity and key2
    assert result.segments[2].discontinuity is True
    assert (
        result.segments[2].key_line
        == '#EXT-X-KEY:METHOD=AES-128,URI="https://example.com/key2.bin"'
    )

    # Back to key1 with discontinuity
    assert result.segments[3].discontinuity is True
    assert (
        result.segments[3].key_line
        == '#EXT-X-KEY:METHOD=AES-128,URI="https://example.com/key1.bin"'
    )


def test_segment_properties() -> None:
    """Test HLSMediaSegment properties (duration, title) and comment handling."""
    # Test duration extraction
    segment = HLSMediaSegment(extinf_line="#EXTINF:5.967528,", segment_url="test.m4s")
    assert segment.duration == pytest.approx(5.967528, rel=1e-6)

    # Test duration with title
    segment = HLSMediaSegment(extinf_line="#EXTINF:10.5,Track Title", segment_url="test.m4s")
    assert segment.duration == pytest.approx(10.5, rel=1e-6)
    assert segment.title == "Track Title"

    # Test malformed EXTINF
    segment = HLSMediaSegment(extinf_line="malformed", segment_url="test.m4s")
    assert segment.duration == 0.0

    # Test comment lines and title extraction in playlist
    playlist_text = """#EXTM3U
#EXT-X-VERSION:3
# This is a comment
#EXTINF:10.0,Test Title
segment1.ts
#EXTINF:10.0,
segment2.ts
#EXT-X-ENDLIST
"""
    result = HLSMediaPlaylistParser(playlist_text).parse()
    assert len(result.segments) == 2
    assert result.segments[0].title == "Test Title"
    assert result.segments[1].title is None
    assert all("# This" not in line for line in result.header_lines)


def test_tag_inheritance() -> None:
    """Test that EXT-X-KEY and EXT-X-MAP persist across segments until changed."""
    playlist_text = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-KEY:METHOD=AES-128,URI="key1.bin"
#EXT-X-MAP:URI="init.mp4"
#EXTINF:10.0,
segment1.ts
#EXTINF:10.0,
segment2.ts
#EXT-X-KEY:METHOD=AES-128,URI="key2.bin"
#EXT-X-MAP:URI="init2.mp4"
#EXTINF:10.0,
segment3.ts
#EXT-X-ENDLIST
"""
    result = HLSMediaPlaylistParser(playlist_text).parse()

    # First two segments inherit key1 and init.mp4
    assert result.segments[0].key_line == '#EXT-X-KEY:METHOD=AES-128,URI="key1.bin"'
    assert result.segments[0].map_line == '#EXT-X-MAP:URI="init.mp4"'
    assert result.segments[1].key_line == '#EXT-X-KEY:METHOD=AES-128,URI="key1.bin"'
    assert result.segments[1].map_line == '#EXT-X-MAP:URI="init.mp4"'

    # Third segment uses key2 and init2.mp4
    assert result.segments[2].key_line == '#EXT-X-KEY:METHOD=AES-128,URI="key2.bin"'
    assert result.segments[2].map_line == '#EXT-X-MAP:URI="init2.mp4"'


def test_invalid_playlists() -> None:
    """Test error handling for invalid playlist formats."""
    # No #EXTM3U header
    with pytest.raises(InvalidDataError, match="must start with #EXTM3U"):
        HLSMediaPlaylistParser("#EXTINF:10.0\nsegment.ts").parse()

    # Empty playlist
    with pytest.raises(InvalidDataError, match="must start with #EXTM3U"):
        HLSMediaPlaylistParser("").parse()

    # No segments
    with pytest.raises(InvalidDataError, match="no segments found"):
        HLSMediaPlaylistParser("#EXTM3U\n#EXT-X-VERSION:3").parse()

    # EXTINF without segment URL
    with pytest.raises(InvalidDataError, match="without preceding segment URL"):
        HLSMediaPlaylistParser("#EXTM3U\n#EXTINF:10.0,\n#EXTINF:10.0,").parse()
