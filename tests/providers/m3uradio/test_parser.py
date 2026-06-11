"""Tests for the M3U Radio provider's playlist parser."""

from music_assistant_models.enums import ContentType

from music_assistant.providers.m3uradio import _guess_content_type, parse_m3u

FULL_PLAYLIST = """#EXTM3U
#EXTINF:-1 tvg-id="st1" tvg-logo="http://logo.example.com/1.png" group-title="Chill",Station One
http://stream.example.com/1.mp3
#EXTINF:-1 group-title="News",Station Two
http://stream.example.com/2.aac

# a stray comment line
#EXTINF:-1,Bare Station
http://stream.example.com/3
"""


def test_parse_full_attributes() -> None:
    """Test that all EXTINF attributes are extracted."""
    stations = parse_m3u(FULL_PLAYLIST)
    assert len(stations) == 3
    first = stations[0]
    assert first["id"] == "st1"
    assert first["name"] == "Station One"
    assert first["url"] == "http://stream.example.com/1.mp3"
    assert first["logo"] == "http://logo.example.com/1.png"
    assert first["group"] == "Chill"
    assert first["tvg_id"] == "st1"


def test_hash_id_fallback_is_stable() -> None:
    """Test that entries without tvg-id get a stable 16-char hash id."""
    stations = parse_m3u(FULL_PLAYLIST)
    second = stations[1]
    assert second["tvg_id"] == ""
    assert len(second["id"]) == 16
    # same name+url must hash to the same id across parses
    assert parse_m3u(FULL_PLAYLIST)[1]["id"] == second["id"]
    # a different url must yield a different id
    changed = FULL_PLAYLIST.replace("2.aac", "2-changed.aac")
    assert parse_m3u(changed)[1]["id"] != second["id"]


def test_name_with_comma_inside_quoted_attribute() -> None:
    """Test that commas inside quoted attribute values do not corrupt the name."""
    playlist = (
        '#EXTM3U\n#EXTINF:-1 group-title="News, Talk",Station X\nhttp://stream.example.com/x.mp3\n'
    )
    stations = parse_m3u(playlist)
    assert len(stations) == 1
    assert stations[0]["name"] == "Station X"
    assert stations[0]["group"] == "News, Talk"


def test_header_comments_and_blank_lines_skipped() -> None:
    """Test that the header, comments and blank lines do not produce entries."""
    stations = parse_m3u(FULL_PLAYLIST)
    urls = [st["url"] for st in stations]
    assert "# a stray comment line" not in urls
    assert all(url.startswith("http") for url in urls)


def test_name_falls_back_to_url() -> None:
    """Test that an entry without a display name uses the url as name."""
    playlist = "#EXTM3U\n#EXTINF:-1,\nhttp://stream.example.com/unnamed.mp3\n"
    stations = parse_m3u(playlist)
    assert len(stations) == 1
    assert stations[0]["name"] == "http://stream.example.com/unnamed.mp3"


def test_url_without_extinf_is_ignored() -> None:
    """Test that bare urls without a preceding EXTINF line are ignored."""
    playlist = "#EXTM3U\nhttp://stream.example.com/orphan.mp3\n"
    assert parse_m3u(playlist) == []


def test_crlf_line_endings() -> None:
    """Test that playlists with CRLF line endings parse correctly."""
    playlist = FULL_PLAYLIST.replace("\n", "\r\n")
    stations = parse_m3u(playlist)
    assert len(stations) == 3
    assert stations[0]["name"] == "Station One"


def test_guess_content_type() -> None:
    """Test content type detection from stream urls."""
    assert _guess_content_type("http://x/stream.mp3") == ContentType.MP3
    assert _guess_content_type("http://x/stream.aac") == ContentType.AAC
    assert _guess_content_type("http://x/stream.mp3?token=abc") == ContentType.MP3
    assert _guess_content_type("http://x/stream") == ContentType.UNKNOWN
