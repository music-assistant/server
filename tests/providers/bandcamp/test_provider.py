"""Test Bandcamp Provider integration."""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, Mock, patch

import pytest
from bandcamp_async_api import (
    BandcampAPIError,
    BandcampMustBeLoggedInError,
    BandcampNotFoundError,
    BandcampRateLimitError,
    SearchResultAlbum,
    SearchResultArtist,
    SearchResultTrack,
)
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    RetriesExhausted,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.providers.bandcamp import DEFAULT_TOP_TRACKS_LIMIT, BandcampProvider, split_id


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
    """Test that Bandcamp is a streaming provider."""
    assert provider.is_streaming_provider is True


async def test_search_with_identity(provider: BandcampProvider) -> None:
    """Test search functionality with identity token."""
    mock_search_results = [
        Mock(spec=SearchResultTrack),
        Mock(spec=SearchResultAlbum),
        Mock(spec=SearchResultArtist),
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
        mock_track_converter.assert_called_once()
        mock_album_converter.assert_called_once()
        mock_artist_converter.assert_called_once()
        assert len(results.tracks) == 1
        assert len(results.albums) == 1
        assert len(results.artists) == 1


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
        pytest.raises(MediaNotFoundError, match=r"Artist 123 not found on Bandcamp"),
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


async def test_get_track_standalone(provider: BandcampProvider) -> None:
    """Test get_track for a standalone track (album_id=0) uses get_track API path."""
    mock_album_obj = Mock()
    mock_album_obj.id = 456
    mock_album_obj.title = "Standalone Album"
    mock_album_obj.art_url = "http://example.com/art.jpg"

    mock_api_track = Mock()
    mock_api_track.album = mock_album_obj

    with (
        patch.object(provider._client, "get_track", new_callable=AsyncMock) as mock_get_track,
        patch.object(provider._converters, "track_from_api") as mock_converter,
    ):
        mock_get_track.return_value = mock_api_track
        mock_converter.return_value = Mock()

        result = await provider.get_track("123-0-789")

        mock_get_track.assert_called_once_with(123, 789)
        mock_converter.assert_called_once_with(
            track=mock_api_track,
            album_id=456,
            album_name="Standalone Album",
            album_image_url="http://example.com/art.jpg",
        )
        assert result is not None


async def test_get_track_standalone_no_album(provider: BandcampProvider) -> None:
    """Test get_track for a standalone track where api_track.album is None."""
    mock_api_track = Mock()
    mock_api_track.album = None

    with (
        patch.object(provider._client, "get_track", new_callable=AsyncMock) as mock_get_track,
        patch.object(provider._converters, "track_from_api") as mock_converter,
    ):
        mock_get_track.return_value = mock_api_track
        mock_converter.return_value = Mock()

        result = await provider.get_track("123-0-789")

        mock_get_track.assert_called_once_with(123, 789)
        mock_converter.assert_called_once_with(
            track=mock_api_track,
            album_id=None,
            album_name="",
            album_image_url="",
        )
        assert result is not None


async def test_get_track_not_found(provider: BandcampProvider) -> None:
    """Test track retrieval when not found."""
    with (
        patch.object(provider._client, "get_album", side_effect=BandcampNotFoundError("Not found")),
        pytest.raises(MediaNotFoundError, match=r"Track 123-456-789 not found on Bandcamp"),
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
    """Test stream details fetches fresh URL and audio format from API."""
    mock_api_track = Mock()
    mock_api_track.id = 789
    mock_api_track.streaming_url = {"mp3-320": "http://example.com/track.mp3"}
    mock_api_album = Mock()
    mock_api_album.tracks = [mock_api_track]

    with patch.object(provider._client, "get_album", new_callable=AsyncMock) as mock_get_album:
        mock_get_album.return_value = mock_api_album

        result = await provider.get_stream_details("123-456-789", MediaType.TRACK)

        mock_get_album.assert_called_once_with(123, 456)
        assert isinstance(result, StreamDetails)
        assert result.item_id == "123-456-789"
        assert result.media_type == MediaType.TRACK
        assert result.stream_type == StreamType.HTTP
        assert result.path == "http://example.com/track.mp3"
        assert result.audio_format.content_type == ContentType.MP3
        assert result.audio_format.bit_rate == 320


async def test_get_stream_details_vbr(provider: BandcampProvider) -> None:
    """Test stream details with VBR mp3-v0 format."""
    mock_api_track = Mock()
    mock_api_track.id = 789
    mock_api_track.streaming_url = {"mp3-v0": "http://example.com/track-v0.mp3"}
    mock_api_album = Mock()
    mock_api_album.tracks = [mock_api_track]

    with patch.object(provider._client, "get_album", new_callable=AsyncMock) as mock_get_album:
        mock_get_album.return_value = mock_api_album

        result = await provider.get_stream_details("123-456-789", MediaType.TRACK)

        assert result.path == "http://example.com/track-v0.mp3"
        assert result.audio_format.content_type == ContentType.MP3
        assert result.audio_format.bit_rate is None


async def test_get_stream_details_no_streaming_url(provider: BandcampProvider) -> None:
    """Test stream details when API track has no streaming URL."""
    mock_api_track = Mock()
    mock_api_track.id = 789
    mock_api_track.streaming_url = {}
    mock_api_album = Mock()
    mock_api_album.tracks = [mock_api_track]

    with patch.object(provider._client, "get_album", new_callable=AsyncMock) as mock_get_album:
        mock_get_album.return_value = mock_api_album

        with pytest.raises(MediaNotFoundError, match=r"No streaming URL found"):
            await provider.get_stream_details("123-456-789", MediaType.TRACK)


async def test_get_stream_details_none_streaming_url(provider: BandcampProvider) -> None:
    """Test stream details when API track has streaming_url=None."""
    mock_api_track = Mock()
    mock_api_track.id = 789
    mock_api_track.streaming_url = None
    mock_api_album = Mock()
    mock_api_album.tracks = [mock_api_track]

    with patch.object(provider._client, "get_album", new_callable=AsyncMock) as mock_get_album:
        mock_get_album.return_value = mock_api_album

        with pytest.raises(MediaNotFoundError, match=r"No streaming URL found"):
            await provider.get_stream_details("123-456-789", MediaType.TRACK)


async def test_get_stream_details_bypasses_cache(provider: BandcampProvider) -> None:
    """Test that get_stream_details calls API directly, not cached get_track."""
    mock_api_track = Mock()
    mock_api_track.id = 789
    mock_api_track.streaming_url = {"mp3-128": "http://example.com/track.mp3"}
    mock_api_album = Mock()
    mock_api_album.tracks = [mock_api_track]

    with (
        patch.object(provider._client, "get_album", new_callable=AsyncMock) as mock_get_album,
        patch.object(provider, "get_track", new_callable=AsyncMock) as mock_get_track,
    ):
        mock_get_album.return_value = mock_api_album

        result = await provider.get_stream_details("123-456-789", MediaType.TRACK)

        mock_get_album.assert_called_once()
        mock_get_track.assert_not_called()
        assert result.path == "http://example.com/track.mp3"
        assert result.audio_format.content_type == ContentType.MP3


async def test_fetch_api_track_album_path(provider: BandcampProvider) -> None:
    """Test _fetch_api_track with 3-part ID routes through get_album."""
    mock_api_track = Mock()
    mock_api_track.id = 789
    mock_api_album = Mock()
    mock_api_album.tracks = [mock_api_track]

    with patch.object(provider._client, "get_album", new_callable=AsyncMock) as mock_get_album:
        mock_get_album.return_value = mock_api_album

        api_track, api_album = await provider._fetch_api_track("123-456-789")

        mock_get_album.assert_called_once_with(123, 456)
        assert api_track is mock_api_track
        assert api_album is mock_api_album


async def test_fetch_api_track_standalone_path(provider: BandcampProvider) -> None:
    """Test _fetch_api_track with album_id=0 routes through get_track."""
    mock_api_track = Mock()

    with patch.object(provider._client, "get_track", new_callable=AsyncMock) as mock_get_track:
        mock_get_track.return_value = mock_api_track

        api_track, api_album = await provider._fetch_api_track("123-0-789")

        mock_get_track.assert_called_once_with(123, 789)
        assert api_track is mock_api_track
        assert api_album is None


async def test_fetch_api_track_not_in_album(provider: BandcampProvider) -> None:
    """Test _fetch_api_track raises when track ID not found in album tracks."""
    mock_other_track = Mock()
    mock_other_track.id = 999
    mock_api_album = Mock()
    mock_api_album.tracks = [mock_other_track]

    with patch.object(provider._client, "get_album", new_callable=AsyncMock) as mock_get_album:
        mock_get_album.return_value = mock_api_album

        with pytest.raises(MediaNotFoundError, match=r"not found in album"):
            await provider._fetch_api_track("123-456-789")


async def test_fetch_api_track_not_found_error(provider: BandcampProvider) -> None:
    """Test _fetch_api_track converts BandcampNotFoundError."""
    with (
        patch.object(
            provider._client,
            "get_album",
            side_effect=BandcampNotFoundError("Not found"),
        ),
        pytest.raises(MediaNotFoundError, match=r"not found on Bandcamp"),
    ):
        await provider._fetch_api_track("123-456-789")


async def test_fetch_api_track_rate_limit_error(provider: BandcampProvider) -> None:
    """Test _fetch_api_track converts BandcampRateLimitError.

    Since @throttle_with_retries is on _fetch_api_track, persistent rate
    limiting exhausts retries and raises RetriesExhausted.
    """
    rate_error = BandcampRateLimitError("Rate limited")
    rate_error.retry_after = 3

    with (
        patch.object(
            provider._client,
            "get_album",
            side_effect=rate_error,
        ) as mock_get_album,
        patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        pytest.raises(RetriesExhausted),
    ):
        await provider._fetch_api_track("123-456-789")

    assert mock_get_album.call_count == provider.throttler.retry_attempts
    # At least retry_attempts - 1 sleeps from backoff; may be higher if the
    # class-level Throttler also called asyncio.sleep due to accumulated entries.
    assert mock_sleep.call_count >= provider.throttler.retry_attempts - 1


async def test_fetch_api_track_generic_api_error(provider: BandcampProvider) -> None:
    """Test _fetch_api_track converts generic BandcampAPIError to MediaNotFoundError."""
    with (
        patch.object(
            provider._client,
            "get_album",
            side_effect=BandcampAPIError("Something went wrong"),
        ),
        pytest.raises(MediaNotFoundError, match=r"Failed to get track 123-456-789"),
    ):
        await provider._fetch_api_track("123-456-789")


def test_split_id_three_parts() -> None:
    """Test split_id with a 3-part compound ID."""
    assert split_id("123-456-789") == (123, 456, 789)


def test_split_id_two_parts() -> None:
    """Test split_id with a 2-part compound ID."""
    assert split_id("123-456") == (123, 456, 0)


def test_split_id_one_part() -> None:
    """Test split_id with a single ID."""
    assert split_id("123") == (123, 0, 0)


async def test_fetch_api_track_two_part_id(provider: BandcampProvider) -> None:
    """Test _fetch_api_track with 2-part ID routes through get_track."""
    # split_id("123-789") returns (123, 789, 0); since track_id=0,
    # the method swaps to album_id=0, track_id=789 and uses get_track.
    mock_api_track = Mock()

    with patch.object(provider._client, "get_track", new_callable=AsyncMock) as mock_get_track:
        mock_get_track.return_value = mock_api_track

        api_track, api_album = await provider._fetch_api_track("123-789")

        mock_get_track.assert_called_once_with(123, 789)
        assert api_track is mock_api_track
        assert api_album is None


async def test_get_stream_details_standalone_track(provider: BandcampProvider) -> None:
    """Test stream details for a standalone track (album_id=0)."""
    mock_api_track = Mock()
    mock_api_track.streaming_url = {"mp3-128": "http://example.com/standalone.mp3"}

    with patch.object(provider._client, "get_track", new_callable=AsyncMock) as mock_get_track:
        mock_get_track.return_value = mock_api_track

        result = await provider.get_stream_details("123-0-789", MediaType.TRACK)

        mock_get_track.assert_called_once_with(123, 789)
        assert isinstance(result, StreamDetails)
        assert result.path == "http://example.com/standalone.mp3"
        assert result.audio_format.content_type == ContentType.MP3
        assert result.audio_format.bit_rate == 128


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
    mock_collection = Mock()
    mock_collection.items = [
        Mock(item_type="band", item_id=100, band_id=100),
        Mock(item_type="album", item_id=200, band_id=300),
    ]

    with (
        patch.object(
            provider._client, "get_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
        patch.object(provider, "get_artist", new_callable=AsyncMock) as mock_get_artist,
    ):
        mock_get_collection.return_value = mock_collection
        mock_get_artist.return_value = Mock()

        artists = [artist async for artist in provider.get_library_artists()]

        assert len(artists) == 2
        assert mock_get_artist.call_count == 2


async def test_get_library_artists_no_identity(provider: BandcampProvider) -> None:
    """Test that library artists returns nothing without identity."""
    provider._client.identity = None
    artists = [artist async for artist in provider.get_library_artists()]
    assert len(artists) == 0


async def test_get_library_albums_success(provider: BandcampProvider) -> None:
    """Test successful library albums retrieval."""
    mock_collection = Mock()
    mock_collection.items = [
        Mock(item_type="album", item_id=456, band_id=123),
    ]

    with (
        patch.object(
            provider._client, "get_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
        patch.object(provider, "get_album", new_callable=AsyncMock) as mock_get_album,
    ):
        mock_get_collection.return_value = mock_collection
        mock_get_album.return_value = Mock()

        albums = [album async for album in provider.get_library_albums()]

        assert len(albums) == 1
        mock_get_album.assert_called_once_with("123-456")


async def test_get_library_tracks_success(provider: BandcampProvider) -> None:
    """Test successful library tracks retrieval."""
    mock_track = Mock()

    with (
        patch.object(provider, "get_library_albums") as mock_get_albums,
        patch.object(provider, "get_album_tracks", new_callable=AsyncMock) as mock_get_tracks,
    ):
        # Make get_library_albums an async generator
        async def mock_albums_gen() -> AsyncGenerator[Mock, None]:
            yield Mock(item_id="123-456")

        mock_get_albums.return_value = mock_albums_gen()
        mock_get_tracks.return_value = [mock_track]

        tracks = [track async for track in provider.get_library_tracks()]

        assert len(tracks) == 1
        mock_get_tracks.assert_called_once_with("123-456")


def test_split_id_malformed_non_numeric() -> None:
    """Test split_id raises InvalidDataError on non-numeric input."""
    with pytest.raises(InvalidDataError, match=r"Malformed Bandcamp ID"):
        split_id("abc-def")


def test_split_id_malformed_empty() -> None:
    """Test split_id raises InvalidDataError on empty string."""
    with pytest.raises(InvalidDataError, match=r"Malformed Bandcamp ID"):
        split_id("")


async def test_fetch_api_track_login_error(provider: BandcampProvider) -> None:
    """Test _fetch_api_track converts BandcampMustBeLoggedInError to LoginFailed."""
    with (
        patch.object(
            provider._client,
            "get_album",
            side_effect=BandcampMustBeLoggedInError("Must be logged in"),
        ),
        pytest.raises(LoginFailed, match=r"login is invalid or expired"),
    ):
        await provider._fetch_api_track("123-456-789")
