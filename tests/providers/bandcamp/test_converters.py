"""Test Bandcamp converters."""

from unittest.mock import Mock

import pytest
from bandcamp_async_api.models import BCAlbum, BCArtist, BCTrack
from music_assistant_models.enums import ContentType

from music_assistant.providers.bandcamp.converters import BandcampConverters


@pytest.fixture
def converters() -> BandcampConverters:
    """Return a BandcampConverters instance."""
    return BandcampConverters("bandcamp", "bandcamp_test")


def test_track_from_search(converters: BandcampConverters) -> None:
    """Test converting SearchResultTrack to MA Track."""
    # Create a mock SearchResultTrack
    search_result = Mock()
    search_result.artist_id = 123
    search_result.album_id = 456
    search_result.id = 789
    search_result.name = "Test Track"
    search_result.artist_name = "Test Artist"
    search_result.album_name = "Test Album"
    search_result.url = "https://test.bandcamp.com/track/test-track"

    result = converters.track_from_search(search_result)

    assert result.item_id == "123-456-789"
    assert result.name == "Test Track"
    assert result.provider == "bandcamp_test"


def test_album_from_search(converters: BandcampConverters) -> None:
    """Test converting SearchResultAlbum to MA Album."""
    # Create a mock SearchResultAlbum
    search_result = Mock()
    search_result.artist_id = 123
    search_result.id = 456
    search_result.name = "Test Album"
    search_result.artist_name = "Test Artist"
    search_result.image_url = "https://f4.bcbits.com/img/a1234567890_16.jpg"
    search_result.url = "https://test.bandcamp.com/album/test-album"
    search_result.artist_url = "https://test.bandcamp.com"

    result = converters.album_from_search(search_result)

    assert result.item_id == "123-456"
    assert result.name == "Test Album"
    assert result.provider == "bandcamp_test"


def test_artist_from_search(converters: BandcampConverters) -> None:
    """Test converting SearchResultArtist to MA Artist."""
    # Create a mock SearchResultArtist
    search_result = Mock()
    search_result.id = 123
    search_result.name = "Test Artist"
    search_result.url = "https://test.bandcamp.com"
    search_result.image_url = "https://f4.bcbits.com/img/a1234567890_16.jpg"
    search_result.tags = ["rock", "indie"]

    result = converters.artist_from_search(search_result)

    assert result.item_id == "123"
    assert result.name == "Test Artist"
    assert result.provider == "bandcamp_test"


def test_track_from_api(converters: BandcampConverters) -> None:
    """Test converting API Track to MA Track."""
    # Create mock API models
    mock_artist = Mock()
    mock_artist.id = 123
    mock_artist.name = "Test Artist"
    mock_artist.url = "https://test.bandcamp.com"

    mock_track = Mock()
    mock_track.id = 789
    mock_track.title = "Test Track"
    mock_track.artist = mock_artist
    mock_track.url = "https://test.bandcamp.com/track/test-track"
    mock_track.duration = 300
    mock_track.lyrics = "Test lyrics"
    mock_track.track_number = 1
    mock_track.streaming_url = {"mp3-320": "https://example.com/track.mp3"}

    result = converters.track_from_api(
        track=mock_track,
        album_id=456,
        album_name="Test Album",
        album_image_url="https://f4.bcbits.com/img/a1234567890_16.jpg",
    )

    assert result.item_id == "123-456-789"
    assert result.name == "Test Track"
    assert result.provider == "bandcamp_test"


def test_artist_from_api(converters: BandcampConverters) -> None:
    """Test converting API Artist to MA Artist."""
    # Create mock API artist
    mock_artist = Mock()
    mock_artist.id = 123
    mock_artist.name = "Test Artist"
    mock_artist.url = "https://test.bandcamp.com"
    mock_artist.image_url = "https://f4.bcbits.com/img/a1234567890_16.jpg"
    mock_artist.bio = "Test bio"

    result = converters.artist_from_api(mock_artist)

    assert result.item_id == "123"
    assert result.name == "Test Artist"
    assert result.provider == "bandcamp_test"


def test_album_from_api(converters: BandcampConverters) -> None:
    """Test converting API Album to MA Album."""
    # Create mock API models
    mock_artist = Mock()
    mock_artist.id = 123
    mock_artist.name = "Test Artist"
    mock_artist.url = "https://test.bandcamp.com"

    mock_album = Mock()
    mock_album.id = 456
    mock_album.title = "Test Album"
    mock_album.artist = mock_artist
    mock_album.url = "https://test.bandcamp.com/album/test-album"
    mock_album.art_url = "https://f4.bcbits.com/img/a1234567890_16.jpg"
    mock_album.release_date = 1609459200
    mock_album.about = "Test album description"

    result = converters.album_from_api(mock_album)

    assert result.item_id == "123-456"
    assert result.name == "Test Album"
    assert result.provider == "bandcamp_test"


def test_track_from_api_without_album_info(converters: BandcampConverters) -> None:
    """Test converting API Track without album info."""
    # Create mock API models
    mock_artist = BCArtist(id=123, name="Test Artist", url="https://test.bandcamp.com")
    mock_track = BCTrack(
        id=789,
        title="Test Track",
        artist=mock_artist,
        url="https://test.bandcamp.com/track/test-track",
        duration=300,
        lyrics="Test lyrics",
        track_number=1,
        streaming_url={"mp3-320": "https://example.com/track.mp3"},
    )

    result = converters.track_from_api(track=mock_track)

    assert result.item_id == "123-0-789"
    assert result.album is None
    assert result.metadata.lyrics == "Test lyrics"


def test_track_from_api_with_album(converters: BandcampConverters) -> None:
    """Test converting API Track with album information."""
    # Create mock API models
    mock_artist = BCArtist(id=123, name="Test Artist", url="https://test.bandcamp.com")
    mock_album = BCAlbum(
        id=456,
        title="Test Album",
        artist=mock_artist,
        url="https://test.bandcamp.com/album/test-album",
        art_url="https://f4.bcbits.com/img/a1234567890_16.jpg",
        release_date=1609459200,
        about="Test album description",
    )
    mock_track = BCTrack(
        id=789,
        title="Test Track",
        artist=mock_artist,
        album=mock_album,
        url="https://test.bandcamp.com/track/test-track",
        duration=300,
        lyrics="Test lyrics",
        track_number=1,
        streaming_url={"mp3-320": "https://example.com/track.mp3"},
    )

    result = converters.track_from_api(track=mock_track)

    assert result.item_id == "123-0-789"
    assert result.album is not None
    assert result.album.item_id == "123-456"
    assert result.album.name == "Test Album"


def _make_mock_track(streaming_url: dict[str, str]) -> Mock:
    """Create a mock API track with the given streaming URL."""
    mock_artist = Mock()
    mock_artist.id = 123
    mock_artist.name = "Test Artist"
    mock_artist.url = "https://test.bandcamp.com"

    mock_track = Mock()
    mock_track.id = 789
    mock_track.title = "Test Track"
    mock_track.artist = mock_artist
    mock_track.url = "https://test.bandcamp.com/track/test-track"
    mock_track.duration = 300
    mock_track.lyrics = None
    mock_track.track_number = 1
    mock_track.streaming_url = streaming_url
    return mock_track


def test_track_from_api_audio_format_mp3_320(converters: BandcampConverters) -> None:
    """Test that mp3-320 streaming URL sets audio format correctly."""
    mock_track = _make_mock_track({"mp3-320": "https://example.com/track.mp3"})
    result = converters.track_from_api(track=mock_track, album_id=456)
    mapping = next(iter(result.provider_mappings))
    assert mapping.audio_format.content_type == ContentType.MP3
    assert mapping.audio_format.bit_rate == 320


def test_track_from_api_audio_format_mp3_v0(converters: BandcampConverters) -> None:
    """Test that mp3-v0 streaming URL sets content type with no bitrate (VBR)."""
    mock_track = _make_mock_track({"mp3-v0": "https://example.com/track.mp3"})
    result = converters.track_from_api(track=mock_track, album_id=456)
    mapping = next(iter(result.provider_mappings))
    assert mapping.audio_format.content_type == ContentType.MP3
    assert mapping.audio_format.bit_rate is None


def test_track_from_api_audio_format_mp3_128(converters: BandcampConverters) -> None:
    """Test that mp3-128 streaming URL sets audio format correctly."""
    mock_track = _make_mock_track({"mp3-128": "https://example.com/track.mp3"})
    result = converters.track_from_api(track=mock_track, album_id=456)
    mapping = next(iter(result.provider_mappings))
    assert mapping.audio_format.content_type == ContentType.MP3
    assert mapping.audio_format.bit_rate == 128


def test_track_from_api_audio_format_none_streaming_url(converters: BandcampConverters) -> None:
    """Test that None streaming_url does not crash."""
    mock_track = _make_mock_track({"mp3-128": "https://example.com/track.mp3"})
    mock_track.streaming_url = None
    result = converters.track_from_api(track=mock_track, album_id=456)
    mapping = next(iter(result.provider_mappings))
    assert mapping.audio_format.content_type == ContentType.MP3
    assert mapping.audio_format.bit_rate is None


def test_streaming_url_priority_v0_over_320(converters: BandcampConverters) -> None:
    """Test that mp3-v0 is preferred over mp3-320."""
    url, bitrate, content_type = converters.streaming_url_from_api(
        {
            "mp3-320": "https://example.com/320.mp3",
            "mp3-v0": "https://example.com/v0.mp3",
        }
    )
    assert url == "https://example.com/v0.mp3"
    assert bitrate is None
    assert content_type == ContentType.MP3


def test_streaming_url_priority_320_over_128(converters: BandcampConverters) -> None:
    """Test that mp3-320 is preferred over mp3-128."""
    url, bitrate, content_type = converters.streaming_url_from_api(
        {
            "mp3-128": "https://example.com/128.mp3",
            "mp3-320": "https://example.com/320.mp3",
        }
    )
    assert url == "https://example.com/320.mp3"
    assert bitrate == 320
    assert content_type == ContentType.MP3


def test_streaming_url_priority_v0_over_320_over_128(converters: BandcampConverters) -> None:
    """Test full priority chain when all three formats are present."""
    url, bitrate, content_type = converters.streaming_url_from_api(
        {
            "mp3-128": "https://example.com/128.mp3",
            "mp3-320": "https://example.com/320.mp3",
            "mp3-v0": "https://example.com/v0.mp3",
        }
    )
    assert url == "https://example.com/v0.mp3"
    assert bitrate is None
    assert content_type == ContentType.MP3


def test_streaming_url_fallback_unknown_key(converters: BandcampConverters) -> None:
    """Test that an unknown streaming key falls back with UNKNOWN content type."""
    url, bitrate, content_type = converters.streaming_url_from_api(
        {"ogg-vorbis": "https://example.com/track.ogg"}
    )
    assert url == "https://example.com/track.ogg"
    assert bitrate is None
    assert content_type == ContentType.UNKNOWN


def test_streaming_url_empty_dict(converters: BandcampConverters) -> None:
    """Test that empty dict returns None for URL and bitrate."""
    url, bitrate, content_type = converters.streaming_url_from_api({})
    assert url is None
    assert bitrate is None
    assert content_type == ContentType.MP3
