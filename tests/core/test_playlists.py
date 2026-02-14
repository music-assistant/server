"""Tests for playlist parsing helpers."""

from music_assistant.helpers.playlists import parse_m3u


def test_m3u_extinf_duration_not_truncated() -> None:
    """Test that EXTINF duration is parsed as full string, not truncated to first char."""
    m3u_data = "#EXTM3U\n#EXTINF:120,Test Song\nhttp://example.com/song.mp3\n"
    result = parse_m3u(m3u_data)
    assert len(result) == 1
    assert result[0].length == "120"
    assert result[0].title == "Test Song"


def test_m3u_extinf_negative_duration() -> None:
    """Test that EXTINF with -1 duration is treated as None (unknown length)."""
    m3u_data = "#EXTM3U\n#EXTINF:-1,Live Stream\nhttp://example.com/stream\n"
    result = parse_m3u(m3u_data)
    assert len(result) == 1
    assert result[0].length is None
    assert result[0].title == "Live Stream"


def test_m3u_extinf_single_digit_duration() -> None:
    """Test that single-digit durations still work correctly."""
    m3u_data = "#EXTM3U\n#EXTINF:5,Short Clip\nhttp://example.com/clip.mp3\n"
    result = parse_m3u(m3u_data)
    assert len(result) == 1
    assert result[0].length == "5"
