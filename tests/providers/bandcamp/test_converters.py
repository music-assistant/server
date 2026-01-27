"""Test Bandcamp converters."""

from unittest.mock import Mock

import pytest
from bandcamp_async_api.models import BCAlbum, BCArtist, BCTrack

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
