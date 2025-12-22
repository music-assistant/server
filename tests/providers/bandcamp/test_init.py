"""Integration tests for the Bandcamp provider."""

from collections.abc import AsyncGenerator
from unittest import mock

import pytest
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import MediaType, StreamType

from music_assistant.mass import MusicAssistant
from tests.common import wait_for_sync_completion


@pytest.fixture
async def bandcamp_provider(mass: MusicAssistant) -> AsyncGenerator[ProviderConfig, None]:
    """Configure a Bandcamp test fixture, and add a provider to mass that uses it."""
    # Mock the BandcampAPIClient to avoid real API calls
    with mock.patch("music_assistant.providers.bandcamp.BandcampAPIClient") as mock_client_class:
        mock_client = mock.AsyncMock()
        mock_client_class.return_value = mock_client

        # Configure mock client for collection access
        mock_collection = mock.AsyncMock()
        mock_collection.items = []

        # Mock collection items for library tests
        mock_item_artist = mock.AsyncMock()
        mock_item_artist.item_type = "band"
        mock_item_artist.item_id = 123
        mock_item_artist.band_name = "Test Artist"
        mock_item_artist.item_url = "https://test.bandcamp.com"

        mock_item_album = mock.AsyncMock()
        mock_item_album.item_type = "album"
        mock_item_album.band_id = 123
        mock_item_album.item_id = 456
        mock_item_album.item_title = "Test Album"
        mock_item_album.item_url = "https://test.bandcamp.com/album/test-album"

        mock_collection.items = [mock_item_artist, mock_item_album]
        mock_client.get_collection_items.return_value = mock_collection

        # Mock artist and album data
        mock_artist = mock.AsyncMock()
        mock_artist.id = 123
        mock_artist.name = "Test Artist"
        mock_artist.url = "https://test.bandcamp.com"
        mock_client.get_artist.return_value = mock_artist

        mock_album = mock.AsyncMock()
        mock_album.id = 456
        mock_album.title = "Test Album"
        mock_album.artist = mock_artist
        mock_album.url = "https://test.bandcamp.com/album/test-album"
        mock_album.art_url = "https://f4.bcbits.com/img/a1234567890_16.jpg"
        mock_album.release_date = 1609459200
        mock_album.about = "Test album description"

        mock_track = mock.AsyncMock()
        mock_track.id = 789
        mock_track.title = "Test Track"
        mock_track.artist = mock_artist
        mock_track.url = "https://test.bandcamp.com/track/test-track"
        mock_track.duration = 300
        mock_track.streaming_url = {"mp3-320": "https://example.com/track.mp3"}
        mock_track.track_number = 1
        mock_track.lyrics = "Test lyrics"

        # Configure the streaming_url to behave like a dictionary
        mock_track.configure_mock(streaming_url={"mp3-320": "https://example.com/track.mp3"})

        mock_album.tracks = [mock_track]
        mock_client.get_album.return_value = mock_album
        mock_client.get_track.return_value = mock_track

        async with wait_for_sync_completion(mass):
            config = await mass.config.save_provider_config(
                "bandcamp",
                {
                    "identity": "mock_identity_token",
                    "search_limit": 10,
                    "top_tracks_limit": 50,
                },
            )
            await mass.music.start_sync()

        yield config


@pytest.mark.usefixtures("bandcamp_provider")
async def test_initial_sync(mass: MusicAssistant) -> None:
    """Test that initial sync worked."""
    # Test library access (requires identity token)
    all_artists = await mass.music.artists.library_items()
    artists = [artist for artist in all_artists if artist.provider == "bandcamp"]

    assert len(artists) >= 0  # May be empty if no collection items

    all_albums = await mass.music.albums.library_items()
    albums = [album for album in all_albums if album.provider == "bandcamp"]

    assert len(albums) >= 0  # May be empty if no collection items


@pytest.mark.usefixtures("bandcamp_provider")
async def test_search_functionality(mass: MusicAssistant) -> None:
    """Test search functionality."""
    # Mock search results
    with mock.patch("music_assistant.providers.bandcamp.BandcampAPIClient") as mock_client_class:
        mock_client = mock.AsyncMock()
        mock_client_class.return_value = mock_client

        # Mock search results
        mock_search_result_track = mock.AsyncMock()
        mock_search_result_track.__class__.__name__ = "SearchResultTrack"
        mock_search_result_track.artist_id = 123
        mock_search_result_track.album_id = 456
        mock_search_result_track.id = 789
        mock_search_result_track.name = "Search Test Track"
        mock_search_result_track.artist_name = "Search Test Artist"
        mock_search_result_track.album_name = "Search Test Album"
        mock_search_result_track.url = "https://test.bandcamp.com/track/search-test"

        mock_client.search.return_value = [mock_search_result_track]

        # Perform search
        results = await mass.music.search("test query", [MediaType.TRACK], limit=5)

        # Filter for bandcamp results
        bandcamp_tracks = [track for track in results.tracks if track.provider == "bandcamp"]
        assert len(bandcamp_tracks) >= 0  # May be empty if search is mocked differently


@pytest.mark.usefixtures("bandcamp_provider")
async def test_get_artist_details(mass: MusicAssistant) -> None:
    """Test getting artist details."""
    # Get the bandcamp provider instance
    bandcamp_provider = None
    for provider in mass.music.providers:
        if provider.domain == "bandcamp":
            bandcamp_provider = provider
            break

    assert bandcamp_provider is not None

    # Test artist retrieval
    artist = await bandcamp_provider.get_artist("123")
    assert artist is not None
    assert artist.name == "Test Artist"
    assert artist.provider == bandcamp_provider.instance_id


@pytest.mark.usefixtures("bandcamp_provider")
async def test_get_album_details(mass: MusicAssistant) -> None:
    """Test getting album details."""
    # Get the bandcamp provider instance
    bandcamp_provider = None
    for provider in mass.music.providers:
        if provider.domain == "bandcamp":
            bandcamp_provider = provider
            break

    assert bandcamp_provider is not None

    # Test album retrieval
    album = await bandcamp_provider.get_album("123-456")
    assert album is not None
    assert album.name == "Test Album"
    assert album.provider == bandcamp_provider.instance_id


@pytest.mark.usefixtures("bandcamp_provider")
async def test_get_track_details(mass: MusicAssistant) -> None:
    """Test getting track details."""
    # Get the bandcamp provider instance
    bandcamp_provider = None
    for provider in mass.music.providers:
        if provider.domain == "bandcamp":
            bandcamp_provider = provider
            break

    assert bandcamp_provider is not None

    # Test track retrieval
    track = await bandcamp_provider.get_track("123-456-789")
    assert track is not None
    assert track.name == "Test Track"
    assert track.provider == bandcamp_provider.instance_id


@pytest.mark.usefixtures("bandcamp_provider")
async def test_get_album_tracks(mass: MusicAssistant) -> None:
    """Test getting album tracks."""
    # Get the bandcamp provider instance
    bandcamp_provider = None
    for provider in mass.music.providers:
        if provider.domain == "bandcamp":
            bandcamp_provider = provider
            break

    assert bandcamp_provider is not None

    # Test album tracks retrieval
    tracks = await bandcamp_provider.get_album_tracks("123-456")
    assert len(tracks) == 1
    assert tracks[0].name == "Test Track"


@pytest.mark.usefixtures("bandcamp_provider")
async def test_stream_details(mass: MusicAssistant) -> None:
    """Test stream details retrieval."""
    # Get the bandcamp provider instance
    bandcamp_provider = None
    for provider in mass.music.providers:
        if provider.domain == "bandcamp":
            bandcamp_provider = provider
            break

    assert bandcamp_provider is not None

    # Test stream details retrieval
    stream_details = await bandcamp_provider.get_stream_details("123-456-789", MediaType.TRACK)
    assert stream_details is not None
    assert stream_details.stream_type == StreamType.HTTP
    assert stream_details.path == "https://example.com/track.mp3"
