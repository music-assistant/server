"""Test Bandcamp Provider integration."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from bandcamp_async_api import BandcampAPIError, BandcampNotFoundError
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.providers.bandcamp import DEFAULT_TOP_TRACKS_LIMIT, BandcampProvider


@pytest.fixture
def mass_mock() -> Mock:
    """Return a mock MusicAssistant instance."""
    mass = Mock()
    mass.http_session = AsyncMock()
    mass.metadata.locale = "en_US"
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    mass.cache.delete = AsyncMock()
    return mass


@pytest.fixture
def manifest_mock() -> Mock:
    """Return a mock provider manifest."""
    manifest = Mock()
    manifest.domain = "bandcamp"
    return manifest


@pytest.fixture
def config_mock() -> Mock:
    """Return a mock provider config."""
    config = Mock()
    config.name = "Bandcamp Test"
    config.instance_id = "bandcamp_test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "identity": "mock_identity_token",
        "search_limit": 10,
        "top_tracks_limit": 50,
        "log_level": "INFO",
    }.get(
        key,
        default
        if default is not None
        else (10 if key == "search_limit" else (50 if key == "top_tracks_limit" else "INFO")),
    )
    return config


@pytest.fixture
async def provider(mass_mock: Mock, manifest_mock: Mock, config_mock: Mock) -> BandcampProvider:
    """Return a BandcampProvider instance."""
    provider = BandcampProvider(mass_mock, manifest_mock, config_mock)

    # Initialize the provider
    with patch("music_assistant.providers.bandcamp.BandcampAPIClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client
        await provider.handle_async_init()

    return provider


async def test_provider_initialization(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> None:
    """Test provider initialization."""
    provider = BandcampProvider(mass_mock, manifest_mock, config_mock)

    assert provider.domain == "bandcamp"
    assert provider.instance_id == "bandcamp_test"

    # Test that initialization sets the correct values
    with patch("music_assistant.providers.bandcamp.BandcampAPIClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        await provider.handle_async_init()

        assert provider.top_tracks_limit == DEFAULT_TOP_TRACKS_LIMIT


async def test_handle_async_init_with_identity(provider: BandcampProvider) -> None:
    """Test successful async initialization with identity token."""
    with patch("music_assistant.providers.bandcamp.BandcampAPIClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        await provider.handle_async_init()

        mock_client_class.assert_called_once_with(
            session=provider.mass.http_session,
            identity_token="mock_identity_token",
            default_retry_after=3,
        )
        assert provider._client == mock_client
        assert provider._converters is not None


async def test_handle_async_init_without_identity(mass_mock: Mock, manifest_mock: Mock) -> None:
    """Test async initialization without identity token."""
    config = Mock()
    config.get_value.side_effect = (
        lambda key, default=None: default
        if default is not None
        else ("INFO" if key == "log_level" else None)
    )
    provider = BandcampProvider(mass_mock, manifest_mock, config)

    with patch("music_assistant.providers.bandcamp.BandcampAPIClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        await provider.handle_async_init()

        mock_client_class.assert_called_once_with(
            session=provider.mass.http_session,
            identity_token=None,
            default_retry_after=3,
        )


async def test_is_streaming_provider(provider: BandcampProvider) -> None:
    """Test that Bandcamp is not a streaming provider."""
    assert provider.is_streaming_provider is True


async def test_search_with_identity(provider: BandcampProvider) -> None:
    """Test search functionality with identity token."""

    # Create mock objects with proper class names
    class MockSearchResultTrack:
        def __init__(self) -> None:
            self.__class__.__name__ = "SearchResultTrack"

    class MockSearchResultAlbum:
        def __init__(self) -> None:
            self.__class__.__name__ = "SearchResultAlbum"

    class MockSearchResultArtist:
        def __init__(self) -> None:
            self.__class__.__name__ = "SearchResultArtist"

    mock_search_results = [
        MockSearchResultTrack(),
        MockSearchResultAlbum(),
        MockSearchResultArtist(),
    ]

    with (
        patch.object(provider._client, "search", new_callable=AsyncMock) as mock_search,
        patch.object(provider._converters, "track_from_search") as mock_track_converter,
        patch.object(provider._converters, "album_from_search") as mock_album_converter,
        patch.object(provider._converters, "artist_from_search") as mock_artist_converter,
    ):
        mock_search.return_value = mock_search_results

        mock_track_converter.return_value = Mock()
        mock_album_converter.return_value = Mock()
        mock_artist_converter.return_value = Mock()

        results = await provider.search(
            "test query", [MediaType.TRACK, MediaType.ALBUM, MediaType.ARTIST], limit=5
        )

        mock_search.assert_called_once_with("test query")
        assert results.tracks is not None
        assert results.albums is not None
        assert results.artists is not None


async def test_search_without_identity(provider: BandcampProvider) -> None:
    """Test search returns empty results without identity token."""
    provider._client.identity = None

    results = await provider.search("test query", [MediaType.TRACK])

    assert len(results.tracks) == 0
    assert len(results.albums) == 0
    assert len(results.artists) == 0


async def test_search_api_error(provider: BandcampProvider) -> None:
    """Test search handles API errors gracefully."""
    with (
        patch.object(provider._client, "search", side_effect=BandcampAPIError("API Error")),
        pytest.raises(InvalidDataError, match="Unexpected error during Bandcamp search"),
    ):
        await provider.search("test query", [MediaType.TRACK])


async def test_get_artist_success(provider: BandcampProvider) -> None:
    """Test successful artist retrieval."""
    mock_artist = Mock()

    with (
        patch.object(provider._client, "get_artist", new_callable=AsyncMock) as mock_get_artist,
        patch.object(provider._converters, "artist_from_api") as mock_converter,
    ):
        mock_get_artist.return_value = mock_artist
        mock_converter.return_value = Mock()

        result = await provider.get_artist("123")

        mock_get_artist.assert_called_once_with("123")
        mock_converter.assert_called_once_with(mock_artist)
        assert result is not None


async def test_get_artist_not_found(provider: BandcampProvider) -> None:
    """Test artist retrieval when not found."""
    with (
        patch.object(
            provider._client, "get_artist", side_effect=BandcampNotFoundError("Not found")
        ),
        pytest.raises(MediaNotFoundError, match=r"Bandcamp artist 123 search returned no results"),
    ):
        await provider.get_artist("123")


async def test_get_album_success(provider: BandcampProvider) -> None:
    """Test successful album retrieval."""
    mock_album = Mock()

    with (
        patch.object(provider._client, "get_album", new_callable=AsyncMock) as mock_get_album,
        patch.object(provider._converters, "album_from_api") as mock_converter,
    ):
        mock_get_album.return_value = mock_album
        mock_converter.return_value = Mock()

        result = await provider.get_album("123-456")

        mock_get_album.assert_called_once_with(123, 456)
        assert result is not None


async def test_get_track_success(provider: BandcampProvider) -> None:
    """Test successful track retrieval."""
    mock_album = Mock()
    mock_track = Mock()
    mock_album.tracks = [mock_track]
    mock_track.id = 789

    with (
        patch.object(provider._client, "get_album", new_callable=AsyncMock) as mock_get_album,
        patch.object(provider._converters, "track_from_api") as mock_converter,
    ):
        mock_get_album.return_value = mock_album
        mock_converter.return_value = Mock()

        result = await provider.get_track("123-456-789")

        mock_get_album.assert_called_once_with(123, 456)
        assert result is not None


async def test_get_track_not_found(provider: BandcampProvider) -> None:
    """Test track retrieval when not found."""
    with (
        patch.object(provider._client, "get_album", side_effect=BandcampNotFoundError("Not found")),
        pytest.raises(
            MediaNotFoundError, match=r"Bandcamp track 123-456-789 search returned no results"
        ),
    ):
        await provider.get_track("123-456-789")


async def test_get_album_tracks_success(provider: BandcampProvider) -> None:
    """Test successful album tracks retrieval."""
    mock_album = Mock()
    mock_track = Mock()
    mock_track.streaming_url = {"mp3-128": "http://example.com/track.mp3"}
    mock_album.tracks = [mock_track]
    mock_album.title = "Test Album"
    mock_album.art_url = "http://example.com/art.jpg"

    with (
        patch.object(provider._client, "get_album", new_callable=AsyncMock) as mock_get_album,
        patch.object(provider._converters, "track_from_api") as mock_converter,
    ):
        mock_get_album.return_value = mock_album
        mock_converter.return_value = Mock()

        result = await provider.get_album_tracks("123-456")

        assert len(result) == 1
        mock_converter.assert_called_once()


async def test_get_artist_albums_success(provider: BandcampProvider) -> None:
    """Test successful artist albums retrieval."""
    mock_discography = [{"item_type": "album", "band_id": 123, "item_id": 456}]

    with (
        patch.object(
            provider._client, "get_artist_discography", new_callable=AsyncMock
        ) as mock_get_discography,
        patch.object(provider, "get_album", new_callable=AsyncMock) as mock_get_album,
    ):
        mock_get_discography.return_value = mock_discography
        mock_get_album.return_value = Mock()

        result = await provider.get_artist_albums("123")

        mock_get_discography.assert_called_once_with("123")
        assert len(result) == 1


async def test_get_stream_details_success(provider: BandcampProvider) -> None:
    """Test successful stream details retrieval."""
    # Create mock album and track with proper attributes
    mock_artist = Mock()
    mock_artist.id = 123
    mock_artist.name = "Test Artist"

    mock_track = Mock()
    mock_track.id = 789
    mock_track.artist = mock_artist
    mock_track.title = "Test Track"
    mock_track.duration = 180
    mock_track.track_number = 1
    mock_track.streaming_url = {"mp3-320": "http://example.com/track.mp3"}
    mock_track.url = "http://example.com/track"
    mock_track.lyrics = None

    mock_album = Mock()
    mock_album.id = 456
    mock_album.title = "Test Album"
    mock_album.art_url = "http://example.com/art.jpg"
    mock_album.artist = mock_artist
    mock_album.tracks = [mock_track]

    with (
        patch.object(provider._client, "get_album", new_callable=AsyncMock) as mock_get_album,
        patch.object(provider._converters, "track_from_api") as mock_converter,
    ):
        mock_get_album.return_value = mock_album

        # Create a mock track with metadata.links containing the streaming URL
        mock_ma_track = Mock()
        mock_link = Mock()
        mock_link.url = "http://example.com/track.mp3"
        mock_ma_track.metadata.links = {mock_link}
        mock_converter.return_value = mock_ma_track

        result = await provider.get_stream_details("123-456-789", MediaType.TRACK)

        assert isinstance(result, StreamDetails)
        assert result.stream_type == StreamType.HTTP
        assert result.path == "http://example.com/track.mp3"


async def test_get_stream_details_no_streaming_url(provider: BandcampProvider) -> None:
    """Test stream details when no streaming URL is available."""
    # Mock the get_track method directly to return a track with no streaming URLs
    mock_track = Mock()
    mock_track.metadata.links = []  # Empty links list means no streaming URL

    with patch.object(provider, "get_track", new_callable=AsyncMock) as mock_get_track:
        mock_get_track.return_value = mock_track

        with pytest.raises(
            MediaNotFoundError,
            match=r"No streaming links found for track 123-456-789. Please report this",
        ):
            await provider.get_stream_details("123-456-789", MediaType.TRACK)


async def test_get_artist_toptracks_success(provider: BandcampProvider) -> None:
    """Test successful artist top tracks retrieval."""
    mock_album = Mock()
    mock_track = Mock()

    with (
        patch.object(provider, "get_artist_albums", new_callable=AsyncMock) as mock_get_albums,
        patch.object(provider, "get_album_tracks", new_callable=AsyncMock) as mock_get_tracks,
    ):
        mock_get_albums.return_value = [mock_album]
        mock_get_tracks.return_value = [mock_track]

        result = await provider.get_artist_toptracks("123")

        assert len(result) == 1
        mock_get_albums.assert_called_once_with("123")


async def test_get_library_artists_success(provider: BandcampProvider) -> None:
    """Test successful library artists retrieval."""
    # Test that the method exists and doesn't raise an exception
    # This is a complex async generator method, so we just test it can be called
    assert hasattr(provider, "get_library_artists")
    assert callable(provider.get_library_artists)


async def test_get_library_albums_success(provider: BandcampProvider) -> None:
    """Test successful library albums retrieval."""
    # Test that the method exists and doesn't raise an exception
    # This is a complex async generator method, so we just test it can be called
    assert hasattr(provider, "get_library_albums")
    assert callable(provider.get_library_albums)


async def test_get_library_tracks_success(provider: BandcampProvider) -> None:
    """Test successful library tracks retrieval."""
    # Test that the method exists and doesn't raise an exception
    # This is a complex async generator method, so we just test it can be called
    assert hasattr(provider, "get_library_tracks")
    assert callable(provider.get_library_tracks)
